"""Scoring functions for evaluating agent answers."""

from __future__ import annotations

import re
from typing import Any, Callable

from llmeval.sandbox import Sandbox

ScorerFn = Callable[..., tuple[bool, str]]


def exact_match(answer: str, expected: str, **kwargs: Any) -> tuple[bool, str]:
    """Case-insensitive exact match after normalizing whitespace and punctuation.

    Also falls back to numeric comparison when both answer and expected
    represent the same number (handles commas, currency symbols, trailing zeros).
    """
    def _normalize(s: str) -> str:
        s = s.strip().lower()
        # Strip trailing sentence punctuation that models often add: "APPROVE." → "approve"
        s = s.rstrip('.!?,;:')
        return s.strip()

    pred = _normalize(answer)
    target = _normalize(expected)

    ok = pred == target

    # If string comparison fails, attempt numeric comparison
    if not ok:
        try:
            pred_num = float(pred.replace(',', '').replace('$', ''))
            target_num = float(target.replace(',', '').replace('$', ''))
            ok = pred_num == target_num
        except ValueError:
            pass

    detail = f"exact_match: '{answer}' vs '{expected}'"
    return ok, detail


def strict_match(answer: str, expected: str, **kwargs: Any) -> tuple[bool, str]:
    """Case-sensitive exact match after trimming surrounding whitespace only."""
    ok = answer.strip() == expected.strip()
    detail = f"strict_match: '{answer}' vs '{expected}'"
    return ok, detail


def regex_match(answer: str, pattern: str, **kwargs: Any) -> tuple[bool, str]:
    """Match answer against a regex pattern."""
    m = re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
    ok = m is not None
    detail = f"regex_match: pattern='{pattern}' against '{answer[:100]}'"
    return ok, detail


def contains(answer: str, expected: str, **kwargs: Any) -> tuple[bool, str]:
    """Answer contains the expected string (case-insensitive)."""
    ok = expected.lower() in answer.lower()
    detail = f"contains: '{expected}' in '{answer[:100]}'"
    return ok, detail


def must_not_contain(answer: str, patterns: list[str] | str, **kwargs: Any) -> tuple[bool, str]:
    """Pass only if answer does not match any forbidden regex pattern."""
    if isinstance(patterns, str):
        patterns = [patterns]
    for pattern in patterns:
        if re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
            return False, f"must_not_contain: forbidden pattern matched: {pattern!r}"
    return True, "must_not_contain: OK"


def state_check(
    answer: str,
    sandbox: Sandbox,
    expected_state: dict,
    expected: str | None = None,
    answer_scorer: str = "exact_match",
    expected_absent: list[str] | None = None,
    exact_snapshot: bool = False,
    **kwargs: Any,
) -> tuple[bool, str]:
    """Check final answer and sandbox filesystem state.

    Args:
        answer: Model final answer.
        sandbox: Task sandbox to inspect.
        expected_state: Files that must exist with exact text content.
        expected: Optional final answer to check.
        answer_scorer: "exact_match" or "strict_match" for expected answer.
        expected_absent: Relative file paths that must not exist.
        exact_snapshot: If true, no files other than expected_state keys may exist.
    """
    actual = sandbox.snapshot()
    failures = []

    if expected is not None:
        answer_fn = strict_match if answer_scorer == "strict_match" else exact_match
        answer_ok, answer_detail = answer_fn(answer, expected)
        if not answer_ok:
            failures.append(answer_detail)

    for path, content in expected_state.items():
        if path not in actual:
            failures.append(f"missing file: {path}")
        elif actual[path].strip() != content.strip():
            failures.append(f"mismatch in {path}: expected '{content[:80]}', got '{actual[path][:80]}'")

    for path in expected_absent or []:
        if path in actual:
            failures.append(f"unexpected file present: {path}")

    if exact_snapshot:
        extras = sorted(set(actual) - set(expected_state))
        if extras:
            failures.append(f"unexpected extra files: {', '.join(extras[:20])}")

    ok = len(failures) == 0
    detail = "state_check: " + ("OK" if ok else "; ".join(failures))
    return ok, detail


# Registry
SCORERS: dict[str, ScorerFn] = {
    "exact_match": exact_match,
    "strict_match": strict_match,
    "regex_match": regex_match,
    "contains": contains,
    "must_not_contain": must_not_contain,
    "state_check": state_check,
}
