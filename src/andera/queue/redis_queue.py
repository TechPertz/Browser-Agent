"""RedisQueue — durable work queue backed by Redis.

Satisfies the SAME `TaskQueue` Protocol as `SqliteQueue`. Demonstrates
horizontal-scale path: N worker processes on N machines can dequeue
from one Redis instance with no shared filesystem or DB.

Data model:
  - `<prefix>:pending`          Redis list. LPUSH to enqueue, RPOP to dequeue.
  - `<prefix>:items:<item_id>`  Redis hash. Per-item state:
      { status: pending|claimed|done|dead,
        payload_json, attempts, claim_token, claimed_at,
        created_at, updated_at, last_error }
  - `<prefix>:claimed`          Sorted set keyed on item_id, score=claim_epoch.
                                Used by `reclaim_stale` to find abandoned claims.
  - `<prefix>:dead`             Redis set of dead item_ids.

Claim-lease is atomic via a Lua script: RPOP + HSET in one round trip,
so two workers can never claim the same item even on separate boxes.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "RedisQueue requires the `redis` extra: `uv sync --extra redis`"
    ) from e

MAX_ATTEMPTS_DEFAULT = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


class RedisQueue:
    """Implements TaskQueue over a Redis instance.

    Usage:
        q = RedisQueue("redis://localhost:6379/0", prefix="andera:run-abc123")
        await q.enqueue({"sample_id": "s1", ...})
        job = await q.dequeue()
        await q.ack(job["item_id"])
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "andera:queue",
        max_attempts: int = MAX_ATTEMPTS_DEFAULT,
        client: Any = None,
    ) -> None:
        self._prefix = prefix
        self._max_attempts = max_attempts
        self._url = url
        # Allow injection for tests (fakeredis). Decode to UTF-8 so we don't
        # have to decode every returned value by hand.
        self._redis = client or aioredis.from_url(url, decode_responses=True)

    # --- key helpers ---

    @property
    def _pending_key(self) -> str:
        return f"{self._prefix}:pending"

    @property
    def _claimed_key(self) -> str:
        return f"{self._prefix}:claimed"

    @property
    def _dead_key(self) -> str:
        return f"{self._prefix}:dead"

    def _item_key(self, item_id: str) -> str:
        return f"{self._prefix}:items:{item_id}"

    # --- TaskQueue Protocol ---

    async def enqueue(self, item: dict[str, Any]) -> str:
        item_id = item.get("item_id") or str(uuid.uuid4())
        now = _now_iso()
        item_key = self._item_key(item_id)
        pipe = self._redis.pipeline()
        pipe.hset(item_key, mapping={
            "status": "pending",
            "payload_json": json.dumps(item, ensure_ascii=False),
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        })
        # LPUSH pending so oldest goes to the tail (RPOP pops oldest = FIFO).
        pipe.lpush(self._pending_key, item_id)
        await pipe.execute()
        return item_id

    async def dequeue(self) -> dict[str, Any] | None:
        """Atomically claim the oldest pending item.

        Correctness argument: Redis `RPOP` is atomic by itself — two
        concurrent workers calling RPOP on the same list can never
        receive the same element. The HSET + ZADD that follow are
        bookkeeping; if a crash happens between RPOP and HSET the
        item becomes orphaned and `reclaim_stale` recovers it.
        That's the same contract as SqliteQueue.
        """
        item_id = await self._redis.rpop(self._pending_key)
        if item_id is None:
            return None
        token = str(uuid.uuid4())
        now_iso = _now_iso()
        now_epoch = _now_epoch()
        item_key = self._item_key(item_id)
        pipe = self._redis.pipeline()
        pipe.hset(item_key, mapping={
            "status": "claimed",
            "claim_token": token,
            "claimed_at": now_iso,
            "updated_at": now_iso,
        })
        pipe.zadd(self._claimed_key, {item_id: now_epoch})
        pipe.hget(item_key, "payload_json")
        pipe.hget(item_key, "attempts")
        _, _, payload_json, attempts = await pipe.execute()
        payload = json.loads(payload_json) if payload_json else {}
        payload["item_id"] = item_id
        payload["claim_token"] = token
        payload["attempts"] = int(attempts or 0)
        return payload

    async def ack(self, item_id: str) -> None:
        pipe = self._redis.pipeline()
        pipe.hset(self._item_key(item_id), mapping={
            "status": "done", "updated_at": _now_iso(),
        })
        pipe.zrem(self._claimed_key, item_id)
        await pipe.execute()

    async def nack(self, item_id: str, reason: str) -> None:
        item_key = self._item_key(item_id)
        attempts_raw = await self._redis.hget(item_key, "attempts")
        attempts = int(attempts_raw or 0) + 1
        if attempts >= self._max_attempts:
            pipe = self._redis.pipeline()
            pipe.hset(item_key, mapping={
                "status": "dead", "attempts": attempts,
                "last_error": reason, "updated_at": _now_iso(),
            })
            pipe.zrem(self._claimed_key, item_id)
            pipe.sadd(self._dead_key, item_id)
            await pipe.execute()
        else:
            pipe = self._redis.pipeline()
            pipe.hset(item_key, mapping={
                "status": "pending", "attempts": attempts,
                "last_error": reason, "updated_at": _now_iso(),
                "claim_token": "", "claimed_at": "",
            })
            pipe.zrem(self._claimed_key, item_id)
            pipe.lpush(self._pending_key, item_id)
            await pipe.execute()

    async def dead_letter(self, item_id: str) -> None:
        pipe = self._redis.pipeline()
        pipe.hset(self._item_key(item_id), mapping={
            "status": "dead", "updated_at": _now_iso(),
        })
        pipe.zrem(self._claimed_key, item_id)
        pipe.sadd(self._dead_key, item_id)
        await pipe.execute()

    # --- introspection helpers ---

    async def counts(self) -> dict[str, int]:
        # pending = list length; claimed = zset cardinality; dead = set card
        pipe = self._redis.pipeline()
        pipe.llen(self._pending_key)
        pipe.zcard(self._claimed_key)
        pipe.scard(self._dead_key)
        pending, claimed, dead = await pipe.execute()
        return {
            "pending": int(pending or 0),
            "claimed": int(claimed or 0),
            "dead": int(dead or 0),
        }

    async def reclaim_stale(self, older_than_seconds: int = 300) -> int:
        """Release claims whose claim_epoch is older than the cutoff.

        Crashed-worker recovery: an item gets added to the `claimed` zset
        when dequeued. If its worker never ack/nacks, the score stays in
        the past. We scan for old scores and push those items back onto
        pending.
        """
        cutoff = _now_epoch() - older_than_seconds
        stale_ids: list[str] = await self._redis.zrangebyscore(
            self._claimed_key, 0, cutoff
        )
        reclaimed = 0
        for item_id in stale_ids:
            pipe = self._redis.pipeline()
            pipe.hset(self._item_key(item_id), mapping={
                "status": "pending",
                "claim_token": "",
                "claimed_at": "",
                "updated_at": _now_iso(),
            })
            pipe.zrem(self._claimed_key, item_id)
            pipe.lpush(self._pending_key, item_id)
            await pipe.execute()
            reclaimed += 1
        return reclaimed

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass
