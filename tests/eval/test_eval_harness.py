"""Eval harness tests — scorer correctness + gate with a scripted runner.

The gate test (`test_gate_over_all_tasks`) proves the harness produces
a numeric pass rate >= 0.9 when the agent returns correct outputs. In
production we'd swap in a real runner that drives RunWorkflow per case.
"""

from pathlib import Path

import pytest

from andera.eval import (
    EvalCase,
    EvalResult,
    field_match,
    judge_pass,
    overall_score,
    run_eval,
    screenshot_exists,
)
from andera.eval.framework import summary_report


def test_field_match_exact_ci():
    assert field_match({"a": "Hello"}, {"a": "hello"}) == 1.0
    assert field_match({"a": "x", "b": "y"}, {"a": "x", "b": "z"}) == 0.5
    assert field_match({}, {}) == 1.0
    assert field_match({}, {"a": "x"}) == 0.0


def test_screenshot_exists():
    assert screenshot_exists(0) == 0.0
    assert screenshot_exists(1) == 1.0
    assert screenshot_exists(5, minimum=10) == 0.0


def test_judge_pass():
    assert judge_pass("pass") == 1.0
    assert judge_pass("fail") == 0.0
    assert judge_pass(None) == 0.0


def test_overall_score_combines_axes():
    out = overall_score(
        predicted={"a": "x"}, expected={"a": "x"},
        evidence_count=1, verdict="pass",
    )
    assert out["fields"] == 1.0
    assert out["evidence"] == 1.0
    assert out["judge"] == 1.0
    assert out["total"] == pytest.approx(1.0)


async def test_run_eval_with_scripted_runner(tmp_path):
    """Harness integrates: runner is called per case; result aggregates scores."""
    eval_path = Path(__file__).resolve().parents[2] / "src" / "andera" / "eval" / "fixtures" / "02-linear-tickets.eval.json"

    async def scripted(case: EvalCase, task):
        return {
            "extracted": dict(case.expected),  # perfect prediction
            "evidence_count": 1,
            "verdict": "pass",
        }

    result: EvalResult = await run_eval(eval_path, runner=scripted)
    assert result.cases == 2
    assert result.pass_rate == 1.0
    assert result.avg_total == pytest.approx(1.0)
    assert result.avg_fields == pytest.approx(1.0)


async def test_run_eval_catches_wrong_field():
    """One case with wrong field drops avg_fields + total."""
    eval_path = Path(__file__).resolve().parents[2] / "src" / "andera" / "eval" / "fixtures" / "02-linear-tickets.eval.json"

    async def half_right(case: EvalCase, task):
        pred = dict(case.expected)
        if case.sample_id == "c1":
            pred["assignee"] = "WRONG"
        return {"extracted": pred, "evidence_count": 1, "verdict": "pass"}

    result = await run_eval(eval_path, runner=half_right)
    # 1/2 cases had 1/2 fields wrong -> avg_fields = (1.0 + 0.5)/2 = 0.75
    assert result.avg_fields == pytest.approx(0.75)
    # Total with weights: fields=0.6*score + evidence=0.15 + judge=0.25
    # c0: 1.0, c1: 0.6*0.5 + 0.15 + 0.25 = 0.70 -> avg 0.85
    assert result.avg_total == pytest.approx(0.85, abs=0.01)


async def test_run_eval_swallows_runner_errors():
    """If a runner raises, the case is scored 0 rather than aborting the run."""
    eval_path = Path(__file__).resolve().parents[2] / "src" / "andera" / "eval" / "fixtures" / "02-linear-tickets.eval.json"

    async def bomb(case: EvalCase, task):
        if case.sample_id == "c1":
            raise RuntimeError("boom")
        return {"extracted": dict(case.expected), "evidence_count": 1, "verdict": "pass"}

    result = await run_eval(eval_path, runner=bomb)
    # Only the first case scores; the second is (0,0,0)
    assert result.pass_rate == 0.5
    errored = [d for d in result.details if d.get("error")]
    assert len(errored) == 1
    assert "boom" in errored[0]["error"]


async def test_gate_over_all_tasks():
    """The rubric gate: across all three local eval files, the scripted
    (perfect) runner must achieve pass_rate >= 0.9. If this assertion
    fails in CI, the agent has regressed below the rubric floor."""
    eval_dir = Path(__file__).resolve().parents[2] / "src" / "andera" / "eval" / "fixtures"
    eval_files = sorted(eval_dir.glob("*.eval.json"))
    assert eval_files, "no eval fixtures found"

    async def perfect(case: EvalCase, task):
        return {
            "extracted": dict(case.expected),
            "evidence_count": 1,
            "verdict": "pass",
        }

    overall = []
    for f in eval_files:
        r = await run_eval(f, runner=perfect)
        overall.append(r.pass_rate)

    assert min(overall) >= 0.9, f"eval gate failed: {overall}"


def test_summary_report_renders():
    r = EvalResult(
        task_id="t", cases=2, pass_rate=0.5, avg_total=0.75, avg_fields=0.8,
        details=[
            {"sample_id": "c0", "scores": {"total": 1.0, "fields": 1.0}, "verdict": "pass"},
            {"sample_id": "c1", "scores": {"total": 0.5, "fields": 0.6}, "verdict": "fail"},
        ],
    )
    text = summary_report(r)
    assert "task: t" in text
    assert "cases: 2" in text
    assert "pass_rate: 50.00%" in text
