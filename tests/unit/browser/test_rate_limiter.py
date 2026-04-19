import asyncio
import time

import pytest

from andera.browser.rate_limiter import HostRateLimiter, host_of


def test_host_of_parses_url_and_lowercases():
    assert host_of("https://GitHub.com/foo/bar") == "github.com"
    assert host_of("http://example.org:8080/x") == "example.org"
    assert host_of("just-a-host") == ""


def test_construction_validates():
    with pytest.raises(ValueError):
        HostRateLimiter(rps=0)
    with pytest.raises(ValueError):
        HostRateLimiter(rps=1, burst=0)


async def test_first_requests_within_burst_are_instant():
    """Burst=4 means the first 4 acquires return without blocking."""
    lim = HostRateLimiter(rps=2.0, burst=4)
    start = time.monotonic()
    for _ in range(4):
        await lim.acquire("https://a.com/x")
    elapsed = time.monotonic() - start
    assert elapsed < 0.05  # effectively instant


async def test_rate_enforced_after_burst():
    """With rps=4, 8 sequential acquires should take ~(8-burst)/rps seconds."""
    lim = HostRateLimiter(rps=4.0, burst=2)
    start = time.monotonic()
    for _ in range(8):
        await lim.acquire("https://one.example/")
    elapsed = time.monotonic() - start
    # 2 instant (burst) + 6 at 4rps = 6/4 = 1.5s floor
    assert elapsed >= 1.4
    # Some tolerance for scheduler jitter
    assert elapsed < 2.5


async def test_different_hosts_do_not_interfere():
    """Two hosts at rps=1 each should both drain their buckets in parallel."""
    lim = HostRateLimiter(rps=1.0, burst=1)
    await lim.acquire("https://a.example/")  # burn A's single token
    await lim.acquire("https://b.example/")  # B still has 1 free
    start = time.monotonic()
    await lim.acquire("https://b.example/")  # B waits ~1s
    elapsed_b = time.monotonic() - start
    assert 0.9 <= elapsed_b <= 1.5


async def test_concurrent_workers_share_bucket():
    """10 workers, rps=5, burst=5: should complete in ~1s, not 0s."""
    lim = HostRateLimiter(rps=5.0, burst=5)
    start = time.monotonic()
    async def w():
        await lim.acquire("https://shared.example/")
    await asyncio.gather(*[w() for _ in range(10)])
    elapsed = time.monotonic() - start
    # 5 instant + 5 at 5rps = 1s floor
    assert elapsed >= 0.9
