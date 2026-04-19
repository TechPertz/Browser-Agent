"""Orchestrator tests — stub run_sample so we can verify fan-out + aggregation
without launching Chromium or hitting any LLM."""

import csv
import json

import pytest

from andera.config import load_profile
from andera.orchestrator.runner import RunWorkflow


@pytest.fixture
def profile():
    p = load_profile()
    # Keep concurrency low for tests
    p.browser.concurrency = 2
    p.browser.headless = True
    return p


def _task():
    return {
        "task_id": "test",
        "task_name": "stub",
        "prompt": "do the thing",
        "extract_schema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    }


async def test_run_all_pass(monkeypatch, profile, tmp_path):
    """All 5 samples pass -> aggregate CSV has 5 rows + header."""
    # Patch run_sample globally (the runner imports it at module load).
    from andera.orchestrator import runner as runner_mod

    async def fake_run_sample(*, deps, initial_state, checkpoint_db, thread_id, **kw):
        return {
            "status": "done",
            "verdict": "pass",
            "verdict_reason": "mock ok",
            "extracted": {"x": int(initial_state["input_data"]["idx"])},
            "evidence": [{"sha256": "a" * 64, "name": "s.png"}],
        }

    # Patch the browser pool to a no-op fake — no real chromium.
    class FakeSession:
        async def goto(self, url): ...
        async def click(self, s): ...
        async def type(self, s, v): ...
        async def screenshot(self, n): ...
        async def extract(self, s): ...
        async def snapshot(self): ...
        async def close(self): ...

    class FakePool:
        def __init__(self, *a, **kw):
            pass
        def acquire(self, **kw):
            import contextlib
            @contextlib.asynccontextmanager
            async def _ctx():
                yield FakeSession()
            return _ctx()

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", FakePool)
    monkeypatch.chdir(tmp_path)

    rows = [{"idx": i, "url": f"https://x/{i}"} for i in range(5)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t1")
    result = await wf.execute()

    assert result.total == 5
    assert result.passed == 5
    assert result.failed == 0
    assert result.aggregate_csv and result.aggregate_csv.exists()

    with result.aggregate_csv.open() as f:
        r = list(csv.reader(f))
    assert r[0] == ["sample_id", "row_index", "verdict", "x"]
    assert len(r) == 6  # header + 5

    manifest = json.loads(result.manifest.read_text())
    assert manifest["totals"]["passed"] == 5
    assert len(manifest["samples"]) == 5
    assert manifest["audit_root_hash"]
    assert manifest["manifest_hash"]


async def test_run_mixed_verdicts(monkeypatch, profile, tmp_path):
    """Half pass, half fail (nack twice -> dead -> verdict=fail counted)."""
    from andera.orchestrator import runner as runner_mod

    async def fake_run_sample(*, deps, initial_state, **kw):
        idx = int(initial_state["input_data"]["idx"])
        if idx % 2 == 0:
            return {"status": "done", "verdict": "pass", "extracted": {"x": idx}, "evidence": []}
        return {"status": "failed", "verdict": "fail", "extracted": {}, "evidence": [], "error": "mock fail"}

    class FakeSession:
        async def goto(self, url): ...
        async def click(self, s): ...
        async def type(self, s, v): ...
        async def screenshot(self, n): ...
        async def extract(self, s): ...
        async def snapshot(self): ...
        async def close(self): ...

    class FakePool:
        def __init__(self, *a, **kw): pass
        def acquire(self, **kw):
            import contextlib
            @contextlib.asynccontextmanager
            async def _ctx():
                yield FakeSession()
            return _ctx()

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", FakePool)
    monkeypatch.chdir(tmp_path)

    rows = [{"idx": i} for i in range(4)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t2")
    result = await wf.execute()

    # Pass the even ones. Failed samples may have been retried (nack)
    # but ultimately end in result set.
    assert result.passed == 2
    assert result.total >= 2  # at minimum the 2 passes


async def test_max_samples_caps_work(monkeypatch, profile, tmp_path):
    from andera.orchestrator import runner as runner_mod

    async def fake_run_sample(*, deps, initial_state, **kw):
        return {"status": "done", "verdict": "pass", "extracted": {"x": 1}, "evidence": []}

    class FakeSession:
        async def goto(self, url): ...
        async def click(self, s): ...
        async def type(self, s, v): ...
        async def screenshot(self, n): ...
        async def extract(self, s): ...
        async def snapshot(self): ...
        async def close(self): ...

    class FakePool:
        def __init__(self, *a, **kw): pass
        def acquire(self, **kw):
            import contextlib
            @contextlib.asynccontextmanager
            async def _ctx():
                yield FakeSession()
            return _ctx()

    monkeypatch.setattr(runner_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(runner_mod, "BrowserPool", FakePool)
    monkeypatch.chdir(tmp_path)

    rows = [{"idx": i} for i in range(100)]
    wf = RunWorkflow(profile=profile, task=_task(), input_rows=rows, run_id="t3", max_samples=3)
    result = await wf.execute()
    assert result.total == 3
