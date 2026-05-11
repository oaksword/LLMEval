"""Core evaluation loop: run a single task against a model."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from llmeval.client import ModelClient, CallMetrics
from llmeval.sandbox import Sandbox
from llmeval.tools import execute_tool, JSON_SCHEMA, TOOL_SCHEMA_DESCRIPTION
from llmeval.scorers import SCORERS


SYSTEM_PROMPT = f"""You are an evaluation agent. Complete the user's task using the available tools.

{TOOL_SCHEMA_DESCRIPTION}

{JSON_SCHEMA}

Important:
- Think step by step in the "thought" field.
- Use at most one tool per turn. When you have the answer, set "final_answer" and leave "tool" null.
- Never invent tool results; wait for the actual TOOL_RESULT.
- Answer concisely."""


@dataclass
class StepRecord:
    """Record of one step in the agent trajectory."""
    step: int
    thought: str = ""
    tool: str | None = None
    tool_args: dict = field(default_factory=dict)
    tool_result: dict = field(default_factory=dict)
    final_answer: str | None = None
    metrics: CallMetrics = field(default_factory=CallMetrics)
    elapsed_s: float = 0.0
    parse_error: str | None = None


@dataclass
class TaskResult:
    """Complete result for one task run."""
    task_id: str
    model_id: str
    passed: bool
    final_answer: str
    expected: str
    scorer_detail: str
    total_steps: int
    total_latency_s: float
    total_tokens: int
    total_cost_usd: float | None  # None = unknown
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_reasoning: int = 0
    trajectory: list[dict] = field(default_factory=list)
    repeat_index: int = 0
    error: str | None = None


def _accumulated_metrics(steps: list[StepRecord]) -> CallMetrics:
    """Sum metrics from all steps."""
    m = CallMetrics()
    known_cost = True
    for s in steps:
        m.prompt_tokens += s.metrics.prompt_tokens
        m.completion_tokens += s.metrics.completion_tokens
        m.total_tokens += s.metrics.total_tokens
        m.cache_read_tokens += s.metrics.cache_read_tokens
        m.cache_write_tokens += s.metrics.cache_write_tokens
        m.reasoning_tokens += s.metrics.reasoning_tokens
        if s.metrics.cost_usd is None:
            known_cost = False
        elif known_cost:
            m.cost_usd = (m.cost_usd or 0) + s.metrics.cost_usd
    if not known_cost:
        m.cost_usd = None
    return m


def run_task(
    client: ModelClient,
    task: dict,
    *,
    default_max_steps: int = 12,
    bash_timeout_s: int = 30,
    repeat_index: int = 0,
) -> TaskResult:
    """Evaluate one model on one task.

    Args:
        client: ModelClient instance.
        task: Task dict from YAML (id, instruction, expected, scorer, setup, max_steps).
        default_max_steps: Fallback max_steps if not in task.
        bash_timeout_s: Per-step timeout for bash tool calls.
        repeat_index: Which repeat this is (0-based).
    """
    task_id = task["id"]
    instruction = task["instruction"]
    expected = str(task.get("expected", ""))
    scorer_name = task.get("scorer", "exact_match")
    max_steps = task.get("max_steps", default_max_steps)
    setup = task.get("setup", {})

    sandbox = Sandbox.create()
    trajectory: list[StepRecord] = []
    final_answer = ""
    error = None
    started = time.time()

    try:
        # Set up task files
        files = setup.get("files", [])
        if files:
            sandbox.setup_files(files)

        # Initial CWD context
        cwd_info = _describe_cwd(sandbox)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Sandbox directory contents:\n{cwd_info}\n\nTask:\n{instruction}"},
        ]

        for step_idx in range(max_steps):
            step_start = time.time()
            # Skip temperature in thinking mode (DeepSeek ignores it)
            chat_kwargs = {"temperature": 0.0} if not client.reasoning_effort else {}
            result = client.chat(messages, **chat_kwargs)
            step_elapsed = round(time.time() - step_start, 3)

            parsed = result.parsed
            if parsed is None:
                # JSON parse error
                rec = StepRecord(
                    step=step_idx,
                    parse_error=f"Invalid JSON: {result.content[:200]}",
                    metrics=result.metrics,
                    elapsed_s=step_elapsed,
                )
                trajectory.append(rec)
                # give the model one retry with error feedback
                messages.append(_assistant_msg(result))
                messages.append({
                    "role": "user",
                    "content": "ERROR: Your response was not valid JSON. Please reply with valid JSON only.",
                })
                continue

            thought = parsed.get("thought", "")
            tool = parsed.get("tool")
            tool_args = parsed.get("tool_args", {}) or {}
            final = parsed.get("final_answer")

            rec = StepRecord(
                step=step_idx,
                thought=thought,
                metrics=result.metrics,
                elapsed_s=step_elapsed,
            )

            # ---- FINAL ANSWER ----
            if final is not None:
                rec.final_answer = str(final)
                final_answer = str(final)
                trajectory.append(rec)
                break

            # ---- TOOL CALL ----
            if tool:
                tool_result = execute_tool(tool, tool_args, sandbox, bash_timeout_s)
                rec.tool = tool
                rec.tool_args = tool_args
                rec.tool_result = tool_result
                trajectory.append(rec)

                # Feed tool result back
                messages.append(_assistant_msg(result, parsed))
                messages.append({
                    "role": "user",
                    "content": "TOOL_RESULT:\n" + json.dumps(tool_result, ensure_ascii=False),
                })
                # Cap message history to prevent unbounded cost growth.
                # Keep system + first user + last 20 messages.
                if len(messages) > 22:
                    messages = messages[:2] + messages[-20:]
            else:
                # No tool and no final_answer — malformed
                rec.parse_error = "Response had neither tool nor final_answer"
                trajectory.append(rec)
                messages.append(_assistant_msg(result, parsed))
                messages.append({
                    "role": "user",
                    "content": "ERROR: You must specify either a tool or a final_answer.",
                })

        # --- scoring ---
        scorer_fn = SCORERS.get(scorer_name, SCORERS["exact_match"])
        if scorer_name == "state_check":
            expected_state = task.get("expected_state", {})
            passed, scorer_detail = scorer_fn(answer=final_answer, sandbox=sandbox,
                                              expected_state=expected_state)
        elif scorer_name == "regex_match":
            passed, scorer_detail = scorer_fn(answer=final_answer, pattern=expected)
        else:
            passed, scorer_detail = scorer_fn(answer=final_answer, expected=expected)

    except Exception as exc:
        error = str(exc)
        passed = False
        scorer_detail = f"exception: {error}"

    finally:
        sandbox.cleanup()

    total_latency = round(time.time() - started, 3)
    metrics = _accumulated_metrics(trajectory)

    return TaskResult(
        task_id=task_id,
        model_id=client.model_id,
        passed=passed,
        final_answer=final_answer,
        expected=expected,
        scorer_detail=scorer_detail,
        total_steps=len(trajectory),
        total_latency_s=total_latency,
        total_tokens=metrics.total_tokens,
        total_cost_usd=round(metrics.cost_usd, 6) if metrics.cost_usd is not None else None,
        total_cache_read=metrics.cache_read_tokens,
        total_cache_write=metrics.cache_write_tokens,
        total_reasoning=metrics.reasoning_tokens,
        trajectory=[_step_to_dict(s) for s in trajectory],
        repeat_index=repeat_index,
        error=error,
    )


def _assistant_msg(result, parsed: dict | None = None) -> dict:
    """Build an assistant message dict.

    Includes reasoning_content when present (required by DeepSeek for
    multi-turn conversations after tool calls — see thinking_mode docs).
    """
    content = json.dumps(parsed, ensure_ascii=False) if parsed is not None else result.content
    msg: dict = {"role": "assistant", "content": content}
    if result.reasoning_content:
        msg["reasoning_content"] = result.reasoning_content
    return msg


def _describe_cwd(sandbox: Sandbox) -> str:
    """Produce a simple ls-like description of the sandbox root."""
    entries = []
    for child in sorted(sandbox.root.iterdir()):
        etype = "dir" if child.is_dir() else "file"
        size = child.stat().st_size if child.is_file() else 0
        entries.append(f"  {child.name}/" if child.is_dir() else f"  {child.name} ({size} bytes)")
    return "\n".join(entries) if entries else "  (empty)"


def _step_to_dict(s: StepRecord) -> dict:
    return {
        "step": s.step,
        "thought": s.thought,
        "tool": s.tool,
        "tool_args": s.tool_args,
        "tool_result": s.tool_result,
        "final_answer": s.final_answer,
        "parse_error": s.parse_error,
        "metrics": {
            "prompt_tokens": s.metrics.prompt_tokens,
            "completion_tokens": s.metrics.completion_tokens,
            "total_tokens": s.metrics.total_tokens,
            "cost_usd": s.metrics.cost_usd,
            "cache_read_tokens": s.metrics.cache_read_tokens,
            "cache_write_tokens": s.metrics.cache_write_tokens,
            "reasoning_tokens": s.metrics.reasoning_tokens,
            "cache_status": s.metrics.cache_status,
        },
        "elapsed_s": s.elapsed_s,
    }
