"""RedisQueue parity tests — the whole point of the Protocol is that
SqliteQueue and RedisQueue behave identically. Uses fakeredis so CI
doesn't need a real Redis server.
"""

import asyncio

import pytest

from andera.contracts import TaskQueue


@pytest.fixture
async def q():
    # fakeredis.aioredis ships a full async-compatible client that
    # implements eval/evalsha so the Lua claim script works.
    import fakeredis.aioredis
    from andera.queue.redis_queue import RedisQueue

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    q = RedisQueue("redis://fake", prefix="t", max_attempts=3, client=client)
    yield q
    await q.close()


async def test_implements_task_queue_protocol(q):
    assert isinstance(q, TaskQueue)


async def test_enqueue_dequeue_roundtrip(q):
    item_id = await q.enqueue({"sample_id": "s1", "row_index": 0})
    job = await q.dequeue()
    assert job is not None
    assert job["sample_id"] == "s1"
    assert job["item_id"] == item_id
    assert "claim_token" in job


async def test_dequeue_empty_returns_none(q):
    assert await q.dequeue() is None


async def test_ack_marks_done(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()
    await q.ack(iid)
    counts = await q.counts()
    assert counts["pending"] == 0
    assert counts["claimed"] == 0


async def test_nack_requeues_pending(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()
    await q.nack(iid, "transient")
    counts = await q.counts()
    assert counts["pending"] == 1
    assert counts["claimed"] == 0


async def test_nack_dead_letters_after_max_attempts(q):
    iid = await q.enqueue({"x": 1})
    for _ in range(3):
        item = await q.dequeue()
        assert item is not None
        await q.nack(iid, "boom")
    counts = await q.counts()
    assert counts["dead"] == 1
    assert counts["pending"] == 0


async def test_dequeue_claim_exclusive(q):
    """Parallel dequeues must never return the same item id — the
    atomic Lua claim script is the mechanism that makes horizontal
    scale safe across worker processes."""
    for i in range(20):
        await q.enqueue({"i": i})

    async def worker():
        out = []
        for _ in range(20):
            item = await q.dequeue()
            if item is None:
                break
            out.append(item["item_id"])
        return out

    a, b = await asyncio.gather(worker(), worker())
    all_ids = a + b
    assert len(all_ids) == 20
    assert len(set(all_ids)) == 20  # no duplicate claims


async def test_fifo_ordering(q):
    for i in range(5):
        await q.enqueue({"order": i})
    got = []
    while True:
        item = await q.dequeue()
        if item is None:
            break
        got.append(item["order"])
    assert got == [0, 1, 2, 3, 4]


async def test_reclaim_stale(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()  # claim it
    n = await q.reclaim_stale(older_than_seconds=0)
    assert n == 1
    counts = await q.counts()
    assert counts["pending"] == 1


async def test_factory_routes_to_redis(monkeypatch, tmp_path):
    """make_queue(backend='redis', ...) returns a RedisQueue."""
    from andera.queue import make_queue
    import fakeredis.aioredis

    # make_queue calls aioredis.from_url internally; patch it to use fake.
    from andera.queue import redis_queue as rq_mod
    monkeypatch.setattr(
        rq_mod.aioredis, "from_url",
        lambda url, **kw: fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    q = make_queue(backend="redis", run_id="r1", redis_url="redis://fake")
    assert isinstance(q, rq_mod.RedisQueue)
    # still satisfies the Protocol
    assert isinstance(q, TaskQueue)


async def test_factory_routes_to_sqlite(tmp_path):
    from andera.queue import make_queue, SqliteQueue
    q = make_queue(backend="sqlite", run_id="r1",
                   sqlite_path=tmp_path / "q.db")
    assert isinstance(q, SqliteQueue)


async def test_factory_rejects_unknown_backend():
    from andera.queue import make_queue
    with pytest.raises(ValueError):
        make_queue(backend="kafka", run_id="r1")
