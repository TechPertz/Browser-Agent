import asyncio

import pytest

from andera.contracts import TaskQueue
from andera.queue import SqliteQueue


@pytest.fixture
def q(tmp_path):
    return SqliteQueue(tmp_path / "q.db", max_attempts=3)


async def test_implements_task_queue_protocol(q):
    assert isinstance(q, TaskQueue)


async def test_enqueue_dequeue_roundtrip(q):
    await q.enqueue({"sample_id": "s1"})
    item = await q.dequeue()
    assert item is not None
    assert item["sample_id"] == "s1"
    assert "item_id" in item
    assert "claim_token" in item


async def test_dequeue_empty_returns_none(q):
    assert await q.dequeue() is None


async def test_ack_marks_done(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()
    await q.ack(iid)
    counts = await q.counts()
    assert counts.get("done") == 1
    assert counts.get("pending", 0) == 0


async def test_nack_requeues_pending(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()
    await q.nack(iid, "transient")
    counts = await q.counts()
    assert counts.get("pending") == 1


async def test_nack_dead_letters_after_max_attempts(q):
    iid = await q.enqueue({"x": 1})
    for _ in range(3):
        item = await q.dequeue()
        assert item is not None
        await q.nack(iid, "boom")
    counts = await q.counts()
    assert counts.get("dead") == 1
    assert counts.get("pending", 0) == 0


async def test_dequeue_claim_exclusive(q):
    """Parallel dequeues must never return the same item to two workers."""
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
    assert len(set(all_ids)) == 20  # no duplicates


async def test_reclaim_stale(q):
    iid = await q.enqueue({"x": 1})
    await q.dequeue()  # claim it
    # Immediate reclaim with 0s threshold should release it
    n = await q.reclaim_stale(older_than_seconds=0)
    assert n == 1
    counts = await q.counts()
    assert counts.get("pending") == 1


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
