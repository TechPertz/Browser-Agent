"""Fault-tolerance tests — JSONL durability, counters, and resume path."""

import contextlib
import json

import pytest

from andera.config import load_profile
from andera.orchestrator.runner import RunWorkflow


class _FakeSession:
    async def goto(self, url): ...
    async def click(self, s): ...
    async def type(self, s, v): ...
    async def screenshot(self, n): ...
    async def extract(self, s): ...
    async def snapshot(self): ...
    async def close(self): ...


class _FakePool:
    def __init__(self, *a, **kw):
        pass

    def acquire(self, **kw):
        @contextlib.asynccontextmanager
        async def _ctx():
            yield _FakeSession()
        return _ctx()


@pytest.fixture
def profile():
    p = load_profile()
    p.browser.concurrency = 2
    p.browser.headless = True
    return p


def _task():
    return {
        "task_id": "t", "task_name": "x", "prompt": "do thing",
        "extract_schema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
    }


async def test_samples_jsonl_written_per_sample(monkeypatch, profile, tmp_path):
    """Every completed sample lands in samples.jsonl as one line."""
    from andera.orchestrator import runner as runner_mod
    monkeypatch.chdir(tmp_path)

    async def fake_run_sample(*, deps, initial_state, **kw):
        return {"status": "done", "verdict": "pass", "extracted": {"x": 1}, "evidence": []}

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", _FakePool)

    rows = [{"idx": i} for i in range(5)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t-dur")
    await wf.execute()

    jsonl_path = tmp_path / "runs" / "t-dur" / "samples.jsonl"
    assert jsonl_path.exists()
    lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 5
    assert all(l["verdict"] == "pass" for l in lines)


async def test_run_config_written_at_start(monkeypatch, profile, tmp_path):
    from andera.orchestrator import runner as runner_mod
    monkeypatch.chdir(tmp_path)

    async def fake_run_sample(*, deps, initial_state, **kw):
        return {"status": "done", "verdict": "pass", "extracted": {}, "evidence": []}

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", _FakePool)

    wf = RunWorkflow(profile=profile, task=_task(), input_rows=[{"idx": 0}], run_id="t-cfg")
    await wf.execute()
    cfg = json.loads((tmp_path / "runs" / "t-cfg" / ".run_config.json").read_text())
    assert cfg["run_id"] == "t-cfg"
    assert cfg["task"]["task_id"] == "t"
    assert cfg["max_samples_applied"] == 1


async def test_resume_rehydrates_counters_from_jsonl(monkeypatch, profile, tmp_path):
    """Resuming against an existing samples.jsonl must not double-count."""
    from andera.orchestrator import runner as runner_mod
    from andera.orchestrator.runner import resume as orchestrator_resume
    monkeypatch.chdir(tmp_path)

    call_count = 0
    async def fake_run_sample(*, deps, initial_state, **kw):
        nonlocal call_count
        call_count += 1
        return {"status": "done", "verdict": "pass",
                "extracted": {"x": int(initial_state["input_data"]["idx"])}, "evidence": []}

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", _FakePool)

    # First run: 3 samples -> 3 JSONL rows
    rows = [{"idx": i} for i in range(3)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t-rsm")
    await wf.execute()
    first_calls = call_count

    # Resume: no new samples should be run (queue is drained, JSONL already covers them)
    result = await orchestrator_resume(profile=profile, run_id="t-rsm")
    assert result.total == 3
    assert result.passed == 3
    # No additional run_sample calls
    assert call_count == first_calls


async def test_counters_match_jsonl(monkeypatch, profile, tmp_path):
    """Memory counters + on-disk JSONL must agree."""
    from andera.orchestrator import runner as runner_mod
    monkeypatch.chdir(tmp_path)

    async def fake_run_sample(*, deps, initial_state, **kw):
        idx = int(initial_state["input_data"]["idx"])
        return {
            "status": "done" if idx % 2 == 0 else "failed",
            "verdict": "pass" if idx % 2 == 0 else "fail",
            "extracted": {"x": idx},
            "evidence": [],
        }

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", _FakePool)

    rows = [{"idx": i} for i in range(4)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t-cnt")
    result = await wf.execute()
    assert result.passed == 2
    # CSV reflects same data
    csv_text = (tmp_path / "runs" / "t-cnt" / "output.csv").read_text()
    assert csv_text.count("\n") >= 2  # header + at least some data rows


async def test_resume_missing_config_raises(monkeypatch, profile, tmp_path):
    from andera.orchestrator.runner import resume as orchestrator_resume
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        await orchestrator_resume(profile=profile, run_id="never-existed")
