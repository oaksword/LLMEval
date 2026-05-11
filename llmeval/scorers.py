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


def state_check(answer: str, sandbox: Sandbox, expected_state: dict, **kwargs: Any) -> tuple[bool, str]:
    """Check sandbox filesystem state against expected.

    expected_state: {"relative/path": "expected content", ...}
    """
    actual = sandbox.snapshot()
    failures = []
    for path, content in expected_state.items():
        if path not in actual:
            failures.append(f"missing file: {path}")
        elif actual[path].strip() != content.strip():
            failures.append(f"mismatch in {path}: expected '{content[:80]}', got '{actual[path][:80]}'")
    ok = len(failures) == 0
    detail = "state_check: " + ("OK" if ok else "; ".join(failures))
    return ok, detail


# Registry
SCORERS: dict[str, ScorerFn] = {
    "exact_match": exact_match,
    "regex_match": regex_match,
    "contains": contains,
    "state_check": state_check,
}
