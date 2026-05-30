"""Report generation: aggregate results, console output, JSON export."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llmeval.config import Config
from llmeval.runner import TaskResult


def _green(s: str) -> str: return f"\033[32m{s}\033[0m"
def _red(s: str) -> str: return f"\033[31m{s}\033[0m"
def _bold(s: str) -> str: return f"\033[1m{s}\033[0m"
def _dim(s: str) -> str: return f"\033[2m{s}\033[0m"


def _fmt_cost(val: float | None) -> str:
    """Format cost, showing 'N/A' when unknown."""
    if val is None:
        return "     N/A"
    return f"${val:.4f}"


def _model_label(r: TaskResult) -> str:
    """Display label for a result; prefixes provider so the same model run via
    different providers (e.g. aihubmix/ vs deepseek/) shows as separate rows."""
    if r.provider_name and r.provider_name != "default":
        return f"{r.provider_name}/{r.model_id}"
    return r.model_id


def print_header(config: Config, num_tasks: int) -> None:
    """Print the evaluation run header."""
    model_desc = ", ".join(
        f"{m.provider_name}/{m.model_id}" if m.provider_name != "default" else m.model_id
        for m in config.models
    ) if config.models else "(none)"

    print()
    print(_bold("=" * 72))
    print(_bold("  LLMEval — Agentic Capability Test Suite"))
    print(_bold("=" * 72))
    print(f"  Models:    {model_desc}")
    print(f"  Tasks:     {num_tasks} loaded")
    print(f"  Repeat:    {config.repeat}x per task")
    print(f"  Started:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(_bold("=" * 72))
    print()


def print_results(results: list[TaskResult], config: Config) -> None:
    """Print per-task and per-model summary to console."""

    # --- Per-task pass/fail ---
    if config.verbose:
        print(_bold("\n--- Per-Task Results ---"))
        for r in results:
            status = _green("✓ PASS") if r.passed else _red("✗ FAIL")
            repeat_tag = f" [run {r.repeat_index + 1}]" if config.repeat > 1 else ""
            cost_str = _fmt_cost(r.total_cost_usd)
            print(f"  {status}  {_model_label(r):30s}  {r.task_id:35s}  "
                  f"{r.total_steps:2d} steps/{r.tool_calls:2d} tools  {r.total_latency_s:7.2f}s  "
                  f"{r.total_tokens:5d} tok  {cost_str}"
                  f"{repeat_tag}")
            if not r.passed:
                print(f"          expected: '{r.expected[:80]}'")
                print(f"          got:      '{r.final_answer[:80]}'")
                if r.scorer_detail:
                    print(f"          scorer:   {r.scorer_detail}")
                if r.error:
                    print(f"          error:    {r.error}")

    # --- Per-model summary ---
    by_model: dict[str, list[TaskResult]] = defaultdict(list)
    for r in results:
        by_model[_model_label(r)].append(r)

    print(_bold("\n--- Model Summary ---"))
    print(f"  {'Model':30s}  {'Pass':>5s}  {'Total':>5s}  {'Rate':>7s}  "
          f"{'Avg Steps':>9s}  {'Avg Lat':>7s}  {'Tokens':>7s}  {'Cost':>8s}")
    print(f"  {'-'*30}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*8}")

    for model_id, res_list in by_model.items():
        passed = sum(1 for r in res_list if r.passed)
        total = len(res_list)
        rate = f"{passed/total*100:.1f}%" if total > 0 else "N/A"
        avg_steps = sum(r.total_steps for r in res_list) / total if total > 0 else 0
        avg_lat = sum(r.total_latency_s for r in res_list) / total if total > 0 else 0
        total_tok = sum(r.total_tokens for r in res_list)
        total_cache_read = sum(r.total_cache_read for r in res_list)
        total_cache_write = sum(r.total_cache_write for r in res_list)
        total_reasoning = sum(r.total_reasoning for r in res_list)
        # Sum known costs
        known_costs = [r.total_cost_usd for r in res_list if r.total_cost_usd is not None]
        if known_costs:
            total_cost = f"${sum(known_costs):.4f}"
        else:
            total_cost = "N/A"
        print(f"  {model_id:30s}  {passed:5d}  {total:5d}  {rate:>7s}  "
              f"{avg_steps:8.1f}  {avg_lat:6.2f}s  {total_tok:7d}  {total_cost:>8s}")
        # Show breakdown if cache or reasoning tokens present
        extras = []
        if total_cache_read:
            extras.append(f"cache_read={total_cache_read}")
        if total_cache_write:
            extras.append(f"cache_write={total_cache_write}")
        if total_reasoning:
            extras.append(f"reasoning={total_reasoning}")
        # Show OpenRouter cache hit count
        cache_hits = sum(1 for r in res_list if r.trajectory and any(
            s.get("metrics", {}).get("cache_status") == "HIT" for s in r.trajectory
        ))
        if cache_hits:
            extras.append(f"cache={cache_hits} HIT")
        if extras:
            print(f"  {'':30s}  {'':5s}  {'':5s}  {'':7s}  {'':9s}  {'':7s}  {'':7s}  "
                  f"({', '.join(extras)})")

    # --- Per-category summary ---
    by_category: dict[tuple[str, str], list[TaskResult]] = defaultdict(list)
    for r in results:
        by_category[(_model_label(r), r.category)].append(r)

    if by_category:
        print(_bold("\n--- Category Summary ---"))
        print(f"  {'Model':30s}  {'Category':18s}  {'Pass':>5s}  {'Total':>5s}  {'Rate':>7s}")
        print(f"  {'-'*30}  {'-'*18}  {'-'*5}  {'-'*5}  {'-'*7}")
        for (model_id, category), res_list in sorted(by_category.items()):
            passed = sum(1 for r in res_list if r.passed)
            total = len(res_list)
            rate = f"{passed/total*100:.1f}%" if total > 0 else "N/A"
            print(f"  {model_id:30s}  {category:18s}  {passed:5d}  {total:5d}  {rate:>7s}")

    print()


def save_results(results: list[TaskResult], config: Config, total_latency_s: float) -> Path:
    """Save full results to a JSON file."""
    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    uniq = uuid.uuid4().hex[:6]
    models_slug = "_".join(m.model_id.replace("/", "-") for m in config.models[:3]) or "unknown"
    fname = f"eval_{ts}_{uniq}_{models_slug}.json"
    path = out_dir / fname

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "models": [
                {
                    "id": m.model_id,
                    "provider": m.provider_name,
                    "reasoning_effort": m.reasoning_effort,
                    "pricing_input": m.pricing_input_per_1m,
                    "pricing_cached_input": m.pricing_cached_input_per_1m,
                    "pricing_output": m.pricing_output_per_1m,
                    "base_url": config.get_provider(m).base_url,
                }
                for m in config.models
            ],
            "repeat": config.repeat,
            "max_steps": config.max_steps,
            "bash_timeout_s": config.bash_timeout_s,
            "api_timeout_s": config.api_timeout_s,
        },
        "total_latency_s": total_latency_s,
        "num_results": len(results),
        "results": [_result_to_dict(r) for r in results],
    }

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(_dim(f"  Full results saved to: {path}"))
    return path


def _result_to_dict(r: TaskResult) -> dict:
    return {
        "task_id": r.task_id,
        "category": r.category,
        "model_id": r.model_id,
        "provider_name": r.provider_name,
        "passed": r.passed,
        "final_answer": r.final_answer,
        "expected": r.expected,
        "scorer_detail": r.scorer_detail,
        "total_steps": r.total_steps,
        "tool_calls": r.tool_calls,
        "total_latency_s": r.total_latency_s,
        "total_tokens": r.total_tokens,
        "total_cost_usd": r.total_cost_usd,
        "total_cache_read": r.total_cache_read,
        "total_cache_write": r.total_cache_write,
        "total_reasoning": r.total_reasoning,
        "repeat_index": r.repeat_index,
        "error": r.error,
        "trajectory": r.trajectory,
    }
