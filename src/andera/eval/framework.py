"""Eval harness — runs agent samples against frozen ground truth.

A single eval file is JSON of:
  {
    "task_file": "config/tasks/02-linear-tickets.yaml",
    "cases": [
      {"sample_id": "0", "input": {...}, "expected": {...}},
      ...
    ]
  }

`run_eval` executes each case through the agent (via an injected
runner callable — real agent in prod, scripted in tests), scores each,
and returns an aggregate report. Accuracy gate is applied by the
caller (typically `pytest` asserting total >= 0.9).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from .scorers import overall_score


@dataclass
class EvalCase:
    sample_id: str
    input: dict[str, Any]
    expected: dict[str, Any]


@dataclass
class EvalResult:
    task_id: str
    cases: int
    pass_rate: float          # share of cases with total >= threshold
    avg_total: float          # mean total score
    avg_fields: float
    details: list[dict[str, Any]] = field(default_factory=list)


RunnerFn = Callable[[EvalCase, dict[str, Any]], Awaitable[dict[str, Any]]]
"""Runner(case, task) -> {"extracted": dict, "evidence_count": int, "verdict": str|None}

Production: adapter around RunWorkflow for one sample.
Tests: scripted responses.
"""


def load_eval_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


async def run_eval(
    eval_path: str | Path,
    *,
    runner: RunnerFn,
    pass_threshold: float = 0.8,
    task_override: dict[str, Any] | None = None,
) -> EvalResult:
    data = load_eval_file(eval_path)
    task = task_override or _load_task(data.get("task_file") or "")
    task_id = task.get("task_id") or Path(data.get("task_file", "unknown")).stem

    cases = [EvalCase(**c) for c in data.get("cases", [])]
    details: list[dict[str, Any]] = []

    totals = []
    fields_scores = []
    passes = 0

    for case in cases:
        try:
            out = await runner(case, task)
        except Exception as e:
            out = {"extracted": {}, "evidence_count": 0, "verdict": None,
                   "error": f"{type(e).__name__}: {e}"}
        scored = overall_score(
            predicted=out.get("extracted") or {},
            expected=case.expected,
            evidence_count=out.get("evidence_count", 0),
            verdict=out.get("verdict"),
        )
        details.append({
            "sample_id": case.sample_id,
            "scores": scored,
            "predicted": out.get("extracted") or {},
            "expected": case.expected,
            "verdict": out.get("verdict"),
            "error": out.get("error"),
        })
        totals.append(scored["total"])
        fields_scores.append(scored["fields"])
        if scored["total"] >= pass_threshold:
            passes += 1

    n = max(1, len(cases))
    return EvalResult(
        task_id=task_id,
        cases=len(cases),
        pass_rate=passes / n,
        avg_total=sum(totals) / n,
        avg_fields=sum(fields_scores) / n,
        details=details,
    )


def _load_task(path: str) -> dict[str, Any]:
    import yaml
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text())


def summary_report(result: EvalResult) -> str:
    lines = [
        f"task: {result.task_id}",
        f"cases: {result.cases}",
        f"pass_rate: {result.pass_rate:.2%}",
        f"avg_total:  {result.avg_total:.3f}",
        f"avg_fields: {result.avg_fields:.3f}",
    ]
    for d in result.details:
        lines.append(
            f"  {d['sample_id']}: total={d['scores']['total']:.2f} "
            f"fields={d['scores']['fields']:.2f} "
            f"verdict={d.get('verdict')}"
        )
    return "\n".join(lines)
