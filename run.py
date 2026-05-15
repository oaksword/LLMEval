#!/usr/bin/env python3
"""LLMEval — Lightweight agentic-capability test suite for LLMs.

Usage:
    python run.py --api-key sk-... --base-url https://api.openai.com/v1 --models gpt-4o
    python run.py --models gpt-4o,deepseek-chat --tasks filesystem,injection --repeat 3
    python run.py --list-tasks
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

from llmeval.config import load_config
from llmeval.client import ModelClient
from llmeval.runner import run_task
from llmeval.reporter import print_header, print_results, save_results


def load_tasks(task_dir: str, globs: list[str]) -> list[dict]:
    """Load all tasks from YAML files in task_dir, optionally filtered by globs."""
    base = Path(task_dir)
    if not base.is_dir():
        print(f"Error: task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    all_tasks: list[dict] = []
    for yaml_file in sorted(base.glob("*.yaml")):
        # filename stem matches a glob?
        stem = yaml_file.stem
        if globs:
            if not any(g in stem for g in globs):
                continue
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
            if data and "tasks" in data:
                all_tasks.extend(data["tasks"])

    return all_tasks


def main(argv: list[str] | None = None) -> None:
    config = load_config(argv)

    # --- list-tasks mode ---
    if config.list_tasks:
        tasks = load_tasks(config.task_dir, [])
        print(f"\nAvailable tasks ({len(tasks)}):\n")
        for t in tasks:
            print(f"  {t['id']:40s}  [{t.get('category', '-')}]  {t.get('description', '')}")
        print()
        return

    # --- Validate ---
    if not config.models:
        print("Error: no models specified. Use --models or --model.", file=sys.stderr)
        print("Example: python run.py --models gpt-4o", file=sys.stderr)
        sys.exit(1)

    tasks = load_tasks(config.task_dir, config.task_globs)
    if not tasks:
        print("Error: no tasks found. Check --tasks filter or tasks/ directory.", file=sys.stderr)
        sys.exit(1)

    # --- Run ---
    print_header(config, len(tasks))
    all_results = []
    total_started = time.time()

    for model_spec in config.models:
        provider = config.get_provider(model_spec)
        client = ModelClient(
            provider=provider,
            model_id=model_spec.model_id,
            reasoning_effort=model_spec.reasoning_effort,
            use_cache=config.use_cache,
            pricing_input_per_1m=model_spec.pricing_input_per_1m,
            pricing_cached_input_per_1m=model_spec.pricing_cached_input_per_1m,
            pricing_output_per_1m=model_spec.pricing_output_per_1m,
            pricing_cache_hit_per_1m=model_spec.pricing_cache_hit_per_1m,
            pricing_cache_miss_per_1m=model_spec.pricing_cache_miss_per_1m,
            timeout=config.api_timeout_s,
        )
        label = f"{model_spec.provider_name}/{model_spec.model_id}" if model_spec.provider_name != "default" else model_spec.model_id
        print(f"  Evaluating {label} ...")

        for task in tasks:
            for rep in range(config.repeat):
                if config.verbose or config.repeat > 1:
                    rep_tag = f" [run {rep+1}/{config.repeat}]" if config.repeat > 1 else ""
                    print(f"    {task['id']}{rep_tag} ...", end=" ", flush=True)

                result = run_task(
                    client,
                    task,
                    default_max_steps=config.max_steps,
                    bash_timeout_s=config.bash_timeout_s,
                    repeat_index=rep,
                )
                all_results.append(result)

                if config.verbose or config.repeat > 1:
                    status = "✓" if result.passed else "✗"
                    cost_str = f"${result.total_cost_usd:.6f}" if result.total_cost_usd is not None else "$?.????"
                    print(f"{status}  {result.total_steps} steps  {result.total_latency_s:.2f}s  "
                          f"{result.total_tokens} tok  {cost_str}")

    total_latency = round(time.time() - total_started, 1)

    # --- Report ---
    print_results(all_results, config)
    save_results(all_results, config, total_latency)

    # --- Exit code ---
    failed = sum(1 for r in all_results if not r.passed)
    if failed > 0:
        print(f"  {failed} task(s) failed.\n")
        sys.exit(1)
    else:
        print(f"  All {len(all_results)} task(s) passed.\n")


if __name__ == "__main__":
    main()
