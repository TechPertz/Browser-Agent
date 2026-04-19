"""Multi-worker integration test — proves horizontal scale.

Simulates two `andera worker` processes sharing one Redis queue. Each
picks up a disjoint subset of enqueued samples with no duplicates.
The aggregate `samples.jsonl` contains every sample exactly once.
"""

import contextlib
import json

import fakeredis.aioredis
import pytest

from andera.config import load_profile


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

    async def setup(self): ...
    async def teardown(self): ...

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
    p.queue.backend = "redis"
    return p


async def test_two_workers_share_one_queue(monkeypatch, profile, tmp_path):
    """Enqueue 10 items; two WorkerNode processes drain them; every
    item lands in samples.jsonl exactly once, disjoint across workers."""
    monkeypatch.chdir(tmp_path)

    # Write the run config the worker expects.
    run_id = "multi-w-1"
    (tmp_path / "runs" / run_id).mkdir(parents=True)
    (tmp_path / "runs" / run_id / ".run_config.json").write_text(json.dumps({
        "run_id": run_id,
        "task": {
            "task_id": "t", "task_name": "x", "prompt": "do thing",
            "extract_schema": {"type": "object", "properties": {"i": {"type": "integer"}}},
        },
    }))

    # Shared fake Redis across both workers and the coordinator.
    shared_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    from andera.queue.redis_queue import RedisQueue
    from andera.queue import make_queue as real_make_queue

    def fake_make_queue(**kwargs):
        """Everyone using the factory gets the same fake-Redis-backed queue."""
        if kwargs.get("backend") == "redis":
            return RedisQueue(
                "redis://fake",
                prefix=kwargs.get("redis_prefix") or f"andera:queue:{kwargs['run_id']}",
                max_attempts=kwargs.get("max_attempts", 3),
                client=shared_redis,
            )
        return real_make_queue(**kwargs)

    import andera.worker as worker_mod
    import andera.orchestrator.runner as runner_mod
    monkeypatch.setattr(worker_mod, "make_queue", fake_make_queue)
    # The agent work is stubbed so we don't need browsers or LLMs.
    async def fake_run_sample(*, deps, initial_state, **kw):
        # Small delay lets the two workers interleave; otherwise the first
        # to spin up grabs all 10 before the second gets its first turn.
        import asyncio
        await asyncio.sleep(0.02)
        return {
            "status": "done", "verdict": "pass",
            "extracted": {"i": int(initial_state["input_data"]["i"])},
            "evidence": [],
        }
    monkeypatch.setattr(worker_mod, "run_sample", fake_run_sample)
    monkeypatch.setattr(worker_mod, "BrowserPool", _FakePool)

    # Coordinator pre-fills the queue directly (skipping the runner).
    coord_queue = RedisQueue(
        "redis://fake", prefix=f"andera:queue:{run_id}", client=shared_redis,
    )
    for i in range(10):
        await coord_queue.enqueue({
            "sample_id": f"{run_id}-s{i:02d}", "row_index": i,
            "input_data": {"i": i}, "start_url": f"https://x/{i}",
        })

    # Spin up two workers; let them race to drain the queue.
    from andera.worker import WorkerNode
    w1 = WorkerNode(profile=profile, run_id=run_id,
                    task=json.loads((tmp_path / "runs" / run_id / ".run_config.json").read_text())["task"],
                    worker_id="wA")
    w2 = WorkerNode(profile=profile, run_id=run_id,
                    task=json.loads((tmp_path / "runs" / run_id / ".run_config.json").read_text())["task"],
                    worker_id="wB")

    import asyncio
    processed_a, processed_b = await asyncio.gather(w1.run(), w2.run())

    # Both workers processed something; total == 10.
    assert processed_a >= 1
    assert processed_b >= 1
    assert processed_a + processed_b == 10

    # Aggregate samples.jsonl has every sample exactly once.
    jsonl = tmp_path / "runs" / run_id / "samples.jsonl"
    lines = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    ids = [r["sample_id"] for r in lines]
    assert len(ids) == 10
    assert len(set(ids)) == 10  # no duplicates across workers

    # Every worker_id tag appears on at least one row.
    workers = {r["worker_id"] for r in lines}
    assert "wA" in workers
    assert "wB" in workers

    # All jobs acked; queue fully drained.
    counts = await coord_queue.counts()
    assert counts["pending"] == 0
    assert counts["claimed"] == 0
