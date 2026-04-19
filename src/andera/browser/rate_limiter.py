"""Per-host token-bucket rate limiter.

Agent workers must not hammer any single target host faster than
`per_host_rps` requests/second. The bucket is shared across workers
in-process; swap to Redis INCR for multi-process.

Design:
  - One `_HostBucket` per host, lazily created.
  - `acquire(host)` sleeps until a token is available, then consumes it.
  - A lock guards bucket state; contention is low because sleeps happen
    outside the lock.

This is separate from LiteLLM's retry: that handles *LLM-provider*
backpressure. Rate-limiter handles *target-site* backpressure.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse


def host_of(url: str) -> str:
    """Canonical hostname (lowercased, no port) for bucketing."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return host


class _HostBucket:
    def __init__(self, rps: float, burst: int) -> None:
        self.rps = max(0.1, float(rps))
        self.capacity = max(1, int(burst))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        dt = now - self.updated
        if dt > 0:
            self.tokens = min(float(self.capacity), self.tokens + dt * self.rps)
            self.updated = now

    def time_until_token(self) -> float:
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        need = 1.0 - self.tokens
        return need / self.rps


class HostRateLimiter:
    """Async rate limiter keyed by hostname."""

    def __init__(self, rps: float = 2.0, burst: int = 4) -> None:
        if rps <= 0:
            raise ValueError("rps must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rps = rps
        self._burst = burst
        self._buckets: dict[str, _HostBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, url_or_host: str) -> None:
        """Block until the bucket for this host admits a request."""
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
        if not host:
            return  # can't bucket an empty host; don't block
        while True:
            async with self._lock:
                bucket = self._buckets.setdefault(host, _HostBucket(self._rps, self._burst))
                wait = bucket.time_until_token()
                if wait <= 0:
                    bucket.tokens -= 1.0
                    return
            # Sleep outside the lock so other hosts aren't blocked.
            await asyncio.sleep(max(wait, 0.005))

    def stats(self) -> dict[str, dict[str, float]]:
        """Diagnostic — per-host tokens + capacity."""
        return {
            h: {"tokens": b.tokens, "capacity": float(b.capacity), "rps": b.rps}
            for h, b in self._buckets.items()
        }
