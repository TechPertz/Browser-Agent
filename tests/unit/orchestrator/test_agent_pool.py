"""Two `andera agent` instances sharing one global Redis queue both
process samples, without needing a run_id argument or .run_config.json.
Each job carries its task + run_id inline (set by RunWorkflow._enqueue_all
under `queue.distributed=true`).

Fakeredis — no real Redis, no real Chromium, no real LLM."""

import asyncio
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
    def __init__(self, *a, **kw): pass
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
    p.queue.distributed = True
    return p


async def test_global_queue_two_agents_share_work(monkeypatch, profile, tmp_path):
    """Two `andera agent` pools pull from one global Redis queue; all 10
    samples processed, each exactly once, across both agents."""
    monkeypatch.chdir(tmp_path)

    shared = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # Factory hook: every make_queue(backend="redis") call returns a
    # RedisQueue sharing this fakeredis instance.
    from andera.queue.redis_queue import RedisQueue
    from andera.queue import make_queue as real_make_queue, GLOBAL_QUEUE_PREFIX

    def fake_make_queue(**kwargs):
        if kwargs.get("backend") == "redis":
            prefix = GLOBAL_QUEUE_PREFIX if kwargs.get("global_queue") else (
                kwargs.get("redis_prefix") or f"andera:queue:{kwargs['run_id']}"
            )
            return RedisQueue("redis://fake", prefix=prefix,
                              max_attempts=kwargs.get("max_attempts", 3),
                              client=shared)
        return real_make_queue(**kwargs)

    import andera.worker as worker_mod
    monkeypatch.setattr(worker_mod, "make_queue", fake_make_queue)
    monkeypatch.setattr(worker_mod, "BrowserPool", _FakePool)

    async def fake_run_sample(*, deps, initial_state, **kw):
        # Tiny sleep so two agents actually interleave instead of one
        # grabbing everything.
        await asyncio.sleep(0.02)
        return {
            "status": "done", "verdict": "pass",
            "extracted": {"i": int(initial_state["input_data"]["i"])},
            "evidence": [],
        }
    monkeypatch.setattr(worker_mod, "run_sample", fake_run_sample)

    run_id = "run-glob-1"
    task = {
        "task_id": "t", "task_name": "x", "prompt": "do thing",
        "extract_schema": {"type": "object", "properties": {"i": {"type": "integer"}}},
    }

    # Pre-fill the GLOBAL queue with 10 items, each carrying run_id+task inline.
    q = fake_make_queue(backend="redis", global_queue=True)
    for i in range(10):
        await q.enqueue({
            "sample_id": f"{run_id}-s{i:02d}",
            "row_index": i,
            "input_data": {"i": i},
            "start_url": f"https://x/{i}",
            "run_id": run_id,
            "task": task,
        })

    # Run two agent pools concurrently with a short idle timeout so the
    # test finishes — the real agents loop forever, but we need termination.
    from andera.worker import run_agent_pool

    async def bounded_agent(agent_id):
        # Cancel the pool once counts hit zero for long enough.
        task = asyncio.create_task(
            run_agent_pool(profile=profile, agent_id=agent_id)
        )
        # Poll until the queue is drained (all done/dead states).
        for _ in range(120):  # ~6s max at 50ms ticks
            counts = await q.counts()
            if counts.get("pending", 0) == 0 and counts.get("claimed", 0) == 0:
                # Give agents a beat to ack/nack their last claim
                await asyncio.sleep(0.15)
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            return None

    a, b = await asyncio.gather(
        bounded_agent("agent-A"),
        bounded_agent("agent-B"),
    )

    # Both agents contributed; all 10 samples in JSONL exactly once.
    jsonl = tmp_path / "runs" / run_id / "samples.jsonl"
    assert jsonl.exists()
    rows = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    assert len(rows) == 10
    ids = {r["sample_id"] for r in rows}
    assert len(ids) == 10  # no duplicates

    # Both workers appear — true parallel distribution.
    workers = {r["worker_id"] for r in rows}
    assert "agent-A" in workers
    assert "agent-B" in workers

    # Queue drained.
    counts = await q.counts()
    assert counts.get("pending", 0) == 0
    assert counts.get("claimed", 0) == 0
