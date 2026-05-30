"""Core evaluation loop: run a single task against a model."""

from __future__ import annotations

import json
import re
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
    category: str
    model_id: str
    provider_name: str
    passed: bool
    final_answer: str
    expected: str
    scorer_detail: str
    total_steps: int
    tool_calls: int
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
    category = str(task.get("category", "uncategorized"))
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

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "You are working in a fresh sandbox directory. Use the available tools to inspect or modify files as needed.\n\nTask:\n" + instruction,
            },
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
                # Give a retry without echoing the invalid assistant text back into
                # the conversation. Some providers may flag malformed/raw output on
                # replay, and echoing it can also reinforce multi-object JSON errors.
                messages.append({
                    "role": "user",
                    "content": (
                        "ERROR: Your previous response was not valid JSON and was discarded. "
                        "Reply with exactly one valid JSON object matching the required schema. "
                        "Do not include markdown, comments, or multiple JSON objects."
                    ),
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
            passed, scorer_detail = scorer_fn(
                answer=final_answer,
                sandbox=sandbox,
                expected_state=expected_state,
                expected=expected if expected != "" else None,
                answer_scorer=task.get("answer_scorer", "exact_match"),
                expected_absent=task.get("expected_absent", []),
                exact_snapshot=bool(task.get("exact_snapshot", False)),
            )
        elif scorer_name == "regex_match":
            passed, scorer_detail = scorer_fn(answer=final_answer, pattern=expected)
        elif scorer_name == "must_not_contain":
            passed, scorer_detail = scorer_fn(answer=final_answer, patterns=task.get("forbidden_patterns", expected))
        else:
            passed, scorer_detail = scorer_fn(answer=final_answer, expected=expected)

        # Optional generic gates for agentic/tool-use and safety checks.
        tool_calls = [s for s in trajectory if s.tool]
        gate_failures = []

        min_tool_calls = task.get("min_tool_calls")
        if min_tool_calls is not None and len(tool_calls) < int(min_tool_calls):
            gate_failures.append(f"min_tool_calls: expected >= {min_tool_calls}, got {len(tool_calls)}")

        required_tools = task.get("required_tools", [])
        used_tools = {s.tool for s in tool_calls}
        for required_tool in required_tools:
            if required_tool not in used_tools:
                gate_failures.append(f"required_tools: missing {required_tool}")

        required_commands = task.get("required_commands", [])
        used_commands = []
        for s in tool_calls:
            if s.tool == "run":
                command_text = str(s.tool_args.get("command", "")).strip()
                if command_text:
                    used_commands.append(command_text.split(maxsplit=1)[0])
        for required_command in required_commands:
            if required_command not in used_commands:
                gate_failures.append(f"required_commands: missing {required_command}")

        forbidden_patterns = task.get("must_not_contain", [])
        if isinstance(forbidden_patterns, str):
            forbidden_patterns = [forbidden_patterns]
        for pattern in forbidden_patterns:
            if re.search(pattern, final_answer, re.IGNORECASE | re.DOTALL):
                gate_failures.append(f"must_not_contain: forbidden pattern matched: {pattern!r}")

        if gate_failures:
            passed = False
            scorer_detail = scorer_detail + "; gates: " + "; ".join(gate_failures)

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
        category=category,
        model_id=client.model_id,
        provider_name=client.provider.name,
        passed=passed,
        final_answer=final_answer,
        expected=expected,
        scorer_detail=scorer_detail,
        total_steps=len(trajectory),
        tool_calls=sum(1 for s in trajectory if s.tool),
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
