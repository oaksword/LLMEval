"""Configuration: .env loading, provider resolution, CLI args, and Config dataclass."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Provider — bundles a base_url + api_key for a named provider
# ---------------------------------------------------------------------------

# Built-in default base URLs (user can override via env)
BUILTIN_BASE_URLS: dict[str, str] = {
    "openai":      "https://api.openai.com/v1",
    "deepseek":    "https://api.deepseek.com",
    "openrouter":  "https://openrouter.ai/api/v1",
}


@dataclass
class Provider:
    """Connection details for one API provider."""
    name: str
    base_url: str
    api_key: str = field(repr=False)


def resolve_provider(name: str, fallback_api_key: str = "dummy",
                     fallback_base_url: str = "https://api.openai.com/v1") -> Provider:
    """Resolve a provider by name from environment variables.

    API key lookup order:
      1. LLMEVAL_<NAME>_API_KEY          (per-provider key)
      2. LLMEVAL_API_KEY                 (global default)
      3. *fallback_api_key*              (CLI arg)

    Base URL lookup order:
      1. LLMEVAL_<NAME>_BASE_URL
      2. Built-in default from BUILTIN_BASE_URLS
      3. LLMEVAL_BASE_URL
      4. *fallback_base_url*
    """
    name_upper = name.upper()
    api_key = (
        os.environ.get(f"LLMEVAL_{name_upper}_API_KEY")
        or os.environ.get("LLMEVAL_API_KEY")
        or fallback_api_key
    )
    base_url = (
        os.environ.get(f"LLMEVAL_{name_upper}_BASE_URL")
        or BUILTIN_BASE_URLS.get(name)
        or os.environ.get("LLMEVAL_BASE_URL")
        or fallback_base_url
    )
    return Provider(name=name, base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------------------
# Built-in pricing for providers that don't return cost in API response
# ---------------------------------------------------------------------------

# Per-model cache-aware pricing: (cache_hit, cache_miss, output) per 1M tokens
# DeepSeek does NOT return cost in API response → calculate from token counts.
# Prices reflect the current discount campaigns as of 2026-05-09.
BUILTIN_PRICING: dict[str, tuple[float, float, float]] = {
    # deepseek-v4-flash: input cache hit $0.0028, miss $0.14, output $0.28
    "deepseek/deepseek-v4-flash":  (0.0028, 0.14, 0.28),
    # deepseek-v4-pro: 75% discount until 2026/05/31 15:59 UTC (prices below are after discount)
    "deepseek/deepseek-v4-pro":   (0.003625, 0.435, 0.87),
    # Legacy alias (deepseek-chat resolves to flash)
    "deepseek/deepseek-chat":     (0.0028, 0.14, 0.28),
}

# Default reasoning_effort per provider (None = no default / don't send)
DEFAULT_REASONING: dict[str, str] = {
    "deepseek": "high",
    "openrouter": "medium",
}


# ---------------------------------------------------------------------------
# ModelSpec — a model to evaluate
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """A model to evaluate, with optional provider and reasoning settings."""

    model_id: str
    provider_name: str = "default"
    reasoning_effort: Optional[str] = None  # "high" or "max" for DeepSeek thinking mode

    # Manual pricing override (optional).  When set, simple input/output pricing is used.
    # When not set, built-in cache-aware pricing may apply for known providers.
    pricing_input_per_1m: Optional[float] = None
    pricing_output_per_1m: Optional[float] = None
    pricing_cache_hit_per_1m: Optional[float] = None
    pricing_cache_miss_per_1m: Optional[float] = None

    @classmethod
    def parse(cls, raw: str) -> "ModelSpec":
        """Parse a model specification string.

        Formats:
          model_id                                    → default provider
          provider/model_id                           → named provider
          provider/model_id:reasoning=high            → with reasoning effort
          provider/model_id:price=2.50,10.00          → manual pricing override
          provider/model_id:reasoning=max:price=1,2   → chained options
        """
        import re
        raw = raw.strip()
        reasoning: Optional[str] = None
        pricing_in: Optional[float] = None
        pricing_out: Optional[float] = None

        # Extract chained :key=value options from the end of the string.
        # e.g. "provider/model:reasoning=max:price=1,2" → core="provider/model"
        options: dict[str, str] = {}
        opt_match = re.match(r'^(.+?)((?::\w+=[^:]+)+)$', raw)
        if opt_match:
            raw = opt_match.group(1)
            opt_str = opt_match.group(2)  # ":reasoning=max:price=1,2"
            for opt in opt_str.split(":")[1:]:  # skip leading empty
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    options[k.strip()] = v.strip()

        reasoning = options.get("reasoning")
        if "price" in options:
            price_parts = options["price"].split(",")
            if len(price_parts) >= 2:
                try:
                    pricing_in = float(price_parts[0])
                    pricing_out = float(price_parts[1])
                except ValueError:
                    pass  # ignore malformed price, fall back to auto-detect

        # Extract provider prefix
        if "/" in raw:
            provider, model_id = raw.split("/", 1)
        else:
            provider, model_id = "default", raw

        return cls(
            model_id=model_id.strip(),
            provider_name=provider.strip(),
            reasoning_effort=reasoning.strip() if reasoning else None,
            pricing_input_per_1m=pricing_in,
            pricing_output_per_1m=pricing_out,
        )


# ---------------------------------------------------------------------------
# Config — the full evaluation run configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Full configuration for an evaluation run."""

    api_key: str = "dummy"
    base_url: str = "https://api.openai.com/v1"
    models: list[ModelSpec] = field(default_factory=list)
    providers: dict[str, Provider] = field(default_factory=dict)
    task_globs: list[str] = field(default_factory=list)
    repeat: int = 1
    max_steps: int = 12
    bash_timeout_s: int = 30
    api_timeout_s: int = 60
    out_dir: str = "results"
    task_dir: str = "tasks"
    verbose: bool = False
    use_cache: bool = False  # disabled by default — measure cold performance
    list_tasks: bool = False

    @property
    def model_ids(self) -> list[str]:
        return [m.model_id for m in self.models]

    def get_provider(self, spec: ModelSpec) -> Provider:
        """Return the resolved Provider for a ModelSpec."""
        return self.providers.get(spec.provider_name,
                                  Provider("default", self.base_url, self.api_key))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(argv: Optional[list[str]] = None) -> Config:
    """Load config from .env and CLI args.  CLI wins over .env."""
    load_dotenv(Path.cwd() / ".env")

    p = argparse.ArgumentParser(
        prog="llmeval",
        description="Lightweight agentic-capability test suite for LLMs",
    )

    # --- API / connection (default provider) ---
    p.add_argument("--api-key", default=os.environ.get("LLMEVAL_API_KEY", "dummy"),
                   help="Default API key (env: LLMEVAL_API_KEY)")
    p.add_argument("--base-url", default=os.environ.get("LLMEVAL_BASE_URL", "https://api.openai.com/v1"),
                   help="Default OpenAI-compatible base URL (env: LLMEVAL_BASE_URL)")

    # --- Models ---
    p.add_argument("--models", default=os.environ.get("LLMEVAL_MODELS", ""),
                   help="Comma-separated model specs. Format: [provider/]model[:reasoning=high|max][:price=in,out]")
    p.add_argument("--model", action="append", dest="model_list",
                   help="Add a single model (repeatable). Overrides --models if given.")

    # --- Tasks ---
    p.add_argument("--tasks", default="",
                   help="Comma-separated task globs / category names (default: all)")
    p.add_argument("--task-dir", default="tasks",
                   help="Directory containing task YAML files")

    # --- Execution ---
    p.add_argument("--repeat", type=int, default=1,
                   help="How many times to repeat each task (default: 1)")
    p.add_argument("--max-steps", type=int, default=12,
                   help="Default max agent steps per task (default: 12)")
    p.add_argument("--bash-timeout", type=int, default=30,
                   help="Per-step timeout for bash tool calls in seconds (default: 30)")
    p.add_argument("--api-timeout", type=int, default=60,
                   help="Per-request timeout for API calls in seconds (default: 60)")

    # --- Caching ---
    p.add_argument("--cache", action="store_true",
                   help="Enable OpenRouter response caching (disabled by default). Cache hits are free.")

    # --- Output ---
    p.add_argument("--out-dir", default="results",
                   help="Output directory for result JSON files")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose output (print per-task details)")

    # --- Discover ---
    p.add_argument("--list-tasks", action="store_true",
                   help="List available tasks and exit")

    args = p.parse_args(argv)

    # --- Resolve model specs (smart split to protect price=X,Y commas) ---
    raw_ids: list[str] = []
    raw_models = ""
    if args.model_list:
        raw_models = ",".join(args.model_list)  # --model args don't have comma issues
    elif args.models:
        raw_models = args.models

    # Protect commas inside price=X,Y before splitting
    import re
    _PRICE_COMMA_PLACEHOLDER = "\x01"
    def _protect_price_commas(s: str) -> str:
        """Replace commas inside price=number,number with placeholder."""
        return re.sub(r'price=([\d.]+),([\d.]+)',
                      lambda m: f'price={m.group(1)}{_PRICE_COMMA_PLACEHOLDER}{m.group(2)}', s)

    raw_models = _protect_price_commas(raw_models)
    raw_ids = [m.strip().replace(_PRICE_COMMA_PLACEHOLDER, ',') for m in raw_models.split(",") if m.strip()]

    models: list[ModelSpec] = [ModelSpec.parse(raw) for raw in raw_ids]

    # --- Apply defaults: reasoning effort + built-in pricing ---
    for spec in models:
        # Default reasoning_effort by provider (unless explicitly set)
        if spec.reasoning_effort is None:
            spec.reasoning_effort = DEFAULT_REASONING.get(spec.provider_name)

        # Apply built-in cache-aware pricing if no manual pricing override
        if spec.pricing_input_per_1m is None and spec.pricing_output_per_1m is None:
            key = f"{spec.provider_name}/{spec.model_id}"
            builtin = BUILTIN_PRICING.get(key)
            if builtin is not None:
                spec.pricing_cache_hit_per_1m = builtin[0]
                spec.pricing_cache_miss_per_1m = builtin[1]
                spec.pricing_output_per_1m = builtin[2]

    # --- Resolve providers ---
    providers: dict[str, Provider] = {}

    # Always register the "default" provider from CLI/env
    providers["default"] = Provider("default", args.base_url, args.api_key)

    # Register named providers referenced by model specs
    for spec in models:
        if spec.provider_name != "default" and spec.provider_name not in providers:
            providers[spec.provider_name] = resolve_provider(
                spec.provider_name,
                fallback_api_key=args.api_key,
                fallback_base_url=args.base_url,
            )

    # --- Task filter ---
    task_globs = [t.strip() for t in args.tasks.split(",") if t.strip()]

    return Config(
        api_key=args.api_key,
        base_url=args.base_url,
        models=models,
        providers=providers,
        task_globs=task_globs,
        repeat=args.repeat,
        max_steps=args.max_steps,
        bash_timeout_s=args.bash_timeout,
        api_timeout_s=args.api_timeout,
        out_dir=args.out_dir,
        task_dir=args.task_dir,
        verbose=args.verbose,
        use_cache=args.cache,
        list_tasks=args.list_tasks,
    )



