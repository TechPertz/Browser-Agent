import asyncio

import pytest

from andera.browser import BrowserPool
from andera.storage import FilesystemArtifactStore


def test_rejects_bad_concurrency(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        BrowserPool(artifacts=store, concurrency=0)


def test_exposes_concurrency(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    pool = BrowserPool(artifacts=store, concurrency=3)
    assert pool.concurrency == 3


async def test_semaphore_bounds_in_flight(monkeypatch, tmp_path):
    """Fake out LocalPlaywrightSession.create to avoid launching chromium.

    Verifies: with concurrency=2, at most 2 sessions can be in-flight
    simultaneously no matter how many coroutines acquire.
    """

    class _FakeSession:
        async def close(self): ...

    async def fake_create(**kwargs):
        return _FakeSession()

    from andera.browser import pool as pool_mod

    monkeypatch.setattr(pool_mod.LocalPlaywrightSession, "create", staticmethod(fake_create))

    store = FilesystemArtifactStore(tmp_path)
    pool = BrowserPool(artifacts=store, concurrency=2)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, peak
        async with pool.acquire():
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(6)])
    assert peak <= 2
