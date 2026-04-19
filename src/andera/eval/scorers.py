"""Deterministic scorers for eval cases.

Each scorer takes (predicted, expected, extra_ctx) and returns a float
in [0, 1]. Composite score is a weighted mean.
"""

from __future__ import annotations

from typing import Any


def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip().lower()
    return v


def field_match(predicted: dict[str, Any], expected: dict[str, Any]) -> float:
    """Fraction of expected fields that equal predicted (case-insensitive)."""
    if not expected:
        return 1.0
    hits = 0
    for k, want in expected.items():
        got = predicted.get(k)
        if _norm(got) == _norm(want):
            hits += 1
    return hits / len(expected)


def screenshot_exists(evidence_count: int, minimum: int = 1) -> float:
    return 1.0 if evidence_count >= minimum else 0.0


def judge_pass(verdict: str | None) -> float:
    return 1.0 if verdict == "pass" else 0.0


def overall_score(
    predicted: dict[str, Any],
    expected: dict[str, Any],
    *,
    evidence_count: int = 0,
    verdict: str | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    w = weights or {"fields": 0.6, "evidence": 0.15, "judge": 0.25}
    scores = {
        "fields": field_match(predicted, expected),
        "evidence": screenshot_exists(evidence_count),
        "judge": judge_pass(verdict),
    }
    total = sum(scores[k] * w.get(k, 0.0) for k in scores)
    return {**scores, "total": total}
