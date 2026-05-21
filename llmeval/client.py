"""OpenAI-compatible model client with token/cost tracking and provider support.

Verified against real API responses (2026-05-09):
  - OpenRouter returns usage.cost via model_dump()
  - OpenRouter response caching: X-OpenRouter-Cache request header,
    X-OpenRouter-Cache-Status response header (HIT/MISS)
  - DeepSeek returns prompt_cache_hit/miss_tokens, reasoning_content
  - DeepSeek reasoning_effort must go via extra_body
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

from llmeval.config import Provider

logger = logging.getLogger(__name__)


@dataclass
class CallMetrics:
    """Token usage + cost for a single API call."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None          # None = unknown
    cache_read_tokens: int = 0                # prompt_cache_hit_tokens (DeepSeek) / cached_tokens
    cache_write_tokens: int = 0               # prompt_cache_miss_tokens (DeepSeek)
    reasoning_tokens: int = 0                 # completion_tokens_details.reasoning_tokens
    cache_status: str = ""                    # "HIT" / "MISS" / "" (OpenRouter response cache)


@dataclass
class ClientResult:
    """Result of a chat completion call."""
    raw_json: dict
    content: str
    parsed: dict | None
    reasoning_content: str | None = None      # DeepSeek thinking mode
    metrics: CallMetrics = field(default_factory=CallMetrics)
    finish_reason: str = ""


class ModelClient:
    """Thin wrapper around openai.OpenAI with token/cost/cache tracking.

    - Uses provider for base_url + api_key.
    - Extracts cost from API response when available (OpenRouter).
    - Tracks cache and reasoning tokens (DeepSeek, OpenRouter).
    - Sends OpenRouter cache headers (X-OpenRouter-Cache) when enabled via --cache.
    - Supports DeepSeek reasoning_effort via extra_body.
    """

    def __init__(
        self,
        provider: Provider,
        model_id: str,
        *,
        reasoning_effort: Optional[str] = None,
        use_cache: bool = True,
        pricing_input_per_1m: Optional[float] = None,
        pricing_cached_input_per_1m: Optional[float] = None,
        pricing_output_per_1m: Optional[float] = None,
        pricing_cache_hit_per_1m: Optional[float] = None,
        pricing_cache_miss_per_1m: Optional[float] = None,
        timeout: float = 60.0,
        json_mode: bool = True,
    ):
        self.model_id = model_id
        self.provider = provider
        self.reasoning_effort = reasoning_effort
        self._use_cache = use_cache
        self._pricing_in = pricing_input_per_1m
        self._pricing_cached_in = pricing_cached_input_per_1m
        self._pricing_out = pricing_output_per_1m
        self._pricing_hit = pricing_cache_hit_per_1m
        self._pricing_miss = pricing_cache_miss_per_1m
        self._json_mode = json_mode
        self._client = OpenAI(
            base_url=provider.base_url,
            api_key=provider.api_key,
            timeout=timeout,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ClientResult:
        """Send a chat completion request.  Returns parsed JSON when json_mode enabled."""
        kwargs: dict = dict(
            model=self.model_id,
            messages=messages,
            temperature=temperature,
        )
        # Newer OpenAI models require max_completion_tokens; other providers
        # (deepseek, openrouter) still use max_tokens.
        if self.provider.name == "default":
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # DeepSeek thinking mode — must go via extra_body (not a native SDK param)
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        # OpenRouter response caching headers
        if self.provider.name == "openrouter":
            kwargs["extra_headers"] = {
                "X-OpenRouter-Cache": "true" if self._use_cache else "false",
            }

        # Retry loop with exponential backoff for transient failures.
        # Also handles model-specific parameter conflicts:
        #   - temperature=0 rejected by reasoning models → retry without it
        # For 429s, prefer provider rate-limit hints (Retry-After /
        # X-RateLimit-Reset) over blind short backoff.  This matters for
        # OpenRouter free models, which may expose a per-minute reset time.
        max_retries = 4
        for attempt in range(max_retries):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                break
            except Exception as exc:
                # Reasoning models (gpt-5-mini, o-series, etc.) reject temperature != 1
                if ("temperature" in str(exc).lower()
                        and "does not support" in str(exc).lower()
                        and "temperature" in kwargs):
                    del kwargs["temperature"]
                    logger.info("Retrying without temperature (reasoning model)")
                    resp = self._client.chat.completions.create(**kwargs)
                    break
                if attempt < max_retries - 1 and _is_retryable(exc):
                    sleep_s = _retry_delay_s(exc, attempt)
                    logger.warning(
                        "API call attempt %d/%d failed (%s), retrying in %.1fs",
                        attempt + 1, max_retries, exc, sleep_s,
                    )
                    time.sleep(sleep_s)
                else:
                    raise
        choice = resp.choices[0]
        content = choice.message.content or "{}"
        finish = choice.finish_reason or ""

        # --- Cache status from response headers ---
        cache_status = self._cache_status(resp)

        # --- Reasoning content (DeepSeek thinking mode) ---
        reasoning_content: Optional[str] = None
        try:
            msg_dump = choice.message.model_dump() if hasattr(choice.message, 'model_dump') else {}
            reasoning_content = msg_dump.get("reasoning_content")
        except Exception:
            pass

        # --- Token counts ---
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0

        # Guard: warn if API returned more completion tokens than requested.
        # Some providers/proxies may ignore max_tokens; this helps catch regressions.
        if completion_tokens > max_tokens:
            logger.warning(
                "API returned %d completion tokens (cap was %d) — provider may have "
                "ignored max_tokens. Model: %s, finish_reason: %s",
                completion_tokens, max_tokens, self.model_id, finish,
            )

        # Cache tokens — both provider-level and OpenRouter response cache
        cache_read_tokens = 0
        cache_write_tokens = 0
        try:
            usage_dump = usage.model_dump() if usage and hasattr(usage, 'model_dump') else {}
            # Provider prompt caching (Anthropic/OpenAI native: prompt_tokens_details.cached_tokens)
            cache_read_tokens = usage_dump.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            # DeepSeek KV cache: prompt_cache_hit_tokens / prompt_cache_miss_tokens
            if not cache_read_tokens:
                cache_read_tokens = usage_dump.get("prompt_cache_hit_tokens", 0)
            cache_write_tokens = usage_dump.get("prompt_cache_miss_tokens", 0)
        except Exception:
            pass

        # Reasoning tokens (DeepSeek)
        reasoning_tokens = 0
        try:
            ctd = getattr(usage, 'completion_tokens_details', None)
            if ctd and hasattr(ctd, 'reasoning_tokens') and ctd.reasoning_tokens:
                reasoning_tokens = ctd.reasoning_tokens
        except Exception:
            pass

        # --- Cost extraction ---
        cost = self._extract_cost(resp, prompt_tokens, completion_tokens,
                                  cache_read_tokens, cache_write_tokens)

        # --- Parse JSON ---
        parsed = _parse_json_object(content)
        raw_json = parsed or {}

        return ClientResult(
            raw_json=raw_json,
            content=content,
            parsed=parsed,
            reasoning_content=reasoning_content,
            metrics=CallMetrics(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
                cache_status=cache_status,
            ),
            finish_reason=finish,
        )

    # ------------------------------------------------------------------
    # Cache status (OpenRouter response header)
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_status(resp) -> str:
        """Extract X-OpenRouter-Cache-Status from response headers."""
        try:
            http_resp = getattr(resp, '_response', None)
            if http_resp is not None and hasattr(http_resp, 'headers'):
                return http_resp.headers.get('x-openrouter-cache-status', '').upper()
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Cost extraction
    # ------------------------------------------------------------------

    def _extract_cost(self, resp, prompt_tokens: int, completion_tokens: int,
                       cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> Optional[float]:
        """Extract cost from API response, then fall back to pricing config.

        Priority:
          1. API-reported cost (OpenRouter: usage.cost in model_dump)
          2. Cache-aware pricing (DeepSeek: hit/miss split)
          3. Simple pricing: built-in static pricing or manual :price=in,out
          4. None (unknown — displayed as N/A)
        """
        # 1. API-reported cost (OpenRouter returns usage.cost; $0 on cache HIT)
        cost = self._api_cost(resp)
        if cost is not None:
            return round(cost, 8)

        # 2. Cache-aware pricing (DeepSeek built-in or manual override)
        if self._pricing_hit is not None and self._pricing_miss is not None and self._pricing_out is not None:
            input_cost = (cache_read_tokens / 1_000_000) * self._pricing_hit + \
                         (cache_write_tokens / 1_000_000) * self._pricing_miss
            # Unaccounted prompt tokens (should be 0 for DeepSeek, but be safe)
            uncategorized = prompt_tokens - cache_read_tokens - cache_write_tokens
            if uncategorized > 0:
                input_cost += (uncategorized / 1_000_000) * self._pricing_miss
            output_cost = (completion_tokens / 1_000_000) * self._pricing_out
            return round(input_cost + output_cost, 8)

        # 3. Simple pricing (with optional cache split)
        if self._pricing_in is not None and self._pricing_out is not None:
            # Split prompt tokens into cached vs fresh
            cached_read = cache_read_tokens
            fresh_prompt = max(0, prompt_tokens - cached_read)
            # Cached input price: explicit override or 1/10 of input price
            cached_price = self._pricing_cached_in if self._pricing_cached_in is not None else self._pricing_in / 10
            c = (fresh_prompt / 1_000_000) * self._pricing_in + \
                (cached_read / 1_000_000) * cached_price + \
                (completion_tokens / 1_000_000) * self._pricing_out
            return round(c, 8)

        # 4. Unknown
        return None

    @staticmethod
    def _api_cost(resp) -> Optional[float]:
        """Extract cost from model_dump() — confirmed working for OpenRouter."""
        try:
            if hasattr(resp, 'model_dump'):
                dumped = resp.model_dump()
                cost = dumped.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            pass
        return None


def _parse_json_object(content: str) -> dict | None:
    """Parse a model JSON response.

    Prefer strict JSON. If a model emits multiple JSON objects back-to-back
    despite JSON mode, salvage the first complete object so the runner can
    continue instead of burning every step on parse retries.
    """
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    try:
        decoder = json.JSONDecoder()
        obj, end = decoder.raw_decode(content.lstrip())
        if isinstance(obj, dict):
            trailing = content.lstrip()[end:].strip()
            if trailing:
                logger.debug("Parsed first JSON object and ignored trailing content: %r", trailing[:120])
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient API errors that should be retried."""
    import openai
    # Explicit isinstance checks for known openai retryable error types
    if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError,
                        openai.APITimeoutError, openai.InternalServerError)):
        return True
    # Check HTTP status codes as a fallback
    status = getattr(exc, 'status_code', None)
    if status is not None and status in (429, 500, 502, 503, 504):
        return True
    # String-match fallback for errors from other SDK versions or wrappers
    msg = str(exc).lower()
    for keyword in ('timeout', 'connection', 'rate limit', 'server error', 'service unavailable'):
        if keyword in msg:
            return True
    return False


def _retry_delay_s(exc: Exception, attempt: int) -> float:
    """Return a retry delay, respecting provider rate-limit reset hints.

    Priority:
      1. Retry-After header/body value, if present
      2. X-RateLimit-Reset epoch timestamp, if present
      3. Exponential backoff with jitter

    OpenRouter sometimes places rate-limit headers inside the JSON error body
    as error.metadata.headers rather than only on the HTTP response, so this
    function checks both locations.
    """
    retry_after = _header_float(exc, "retry-after")
    if retry_after is not None:
        return max(0.0, retry_after) + random.uniform(0.25, 0.75)

    reset_at = _rate_limit_reset_epoch_s(exc)
    if reset_at is not None:
        delay = reset_at - time.time()
        if delay > 0:
            return delay + random.uniform(0.5, 1.5)

    return (2 ** attempt) + random.uniform(0, 1)


def _header_float(exc: Exception, name: str) -> Optional[float]:
    """Find a numeric header value on the HTTP response or error body."""
    val = _header_value(exc, name)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _rate_limit_reset_epoch_s(exc: Exception) -> Optional[float]:
    """Find X-RateLimit-Reset and normalize seconds/ms epoch to seconds."""
    raw = _header_value(exc, "x-ratelimit-reset")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    # OpenRouter commonly reports epoch milliseconds, e.g. 1778325000000.
    if val > 10_000_000_000:
        val /= 1000.0
    return val


def _header_value(exc: Exception, name: str) -> Optional[str]:
    """Find a header value in common OpenAI SDK exception shapes."""
    wanted = name.lower()

    # 1. Real HTTP response headers, if the SDK exposes them.
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            val = headers.get(name) or headers.get(wanted)
            if val is not None:
                return str(val)
        except Exception:
            pass

    # 2. OpenAI SDK error body.  OpenRouter may put headers at:
    #    body["error"]["metadata"]["headers"]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        val = _nested_header(body, wanted)
        if val is not None:
            return str(val)

    return None


def _nested_header(obj, wanted: str) -> Optional[str]:
    """Recursively search dict/list structures for a header key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == wanted:
                return str(v)
            found = _nested_header(v, wanted)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _nested_header(item, wanted)
            if found is not None:
                return found
    return None
