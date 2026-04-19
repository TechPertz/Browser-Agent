"""In-process pub/sub for run events.

Subscribers register an asyncio.Queue; publishers call publish(event).
Dead queues (slow consumer, ~full) are evicted rather than blocking
the writer. This is a single-process bus; swap to Redis pub/sub by
making EventBus a Protocol when we deploy multi-worker.
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        # run_id -> set of subscriber queues. None key = "all events".
        self._subs: dict[str | None, set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, run_id: str | None = None, maxsize: int = 256) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue, run_id: str | None = None) -> None:
        subs = self._subs.get(run_id)
        if subs is not None:
            subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        """Non-blocking. Slow consumers miss events, they do not block us."""
        run_id = event.get("run_id")
        targets = set()
        if run_id is not None:
            targets.update(self._subs.get(run_id, set()))
        targets.update(self._subs.get(None, set()))
        for q in list(targets):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Evict slow subscriber rather than drop current event.
                # Caller receives a disconnect on next iteration.
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


# Process-global bus. Tests can create their own via EventBus().
_bus = EventBus()


def get_bus() -> EventBus:
    return _bus
