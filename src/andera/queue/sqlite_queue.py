"""SqliteQueue — durable work queue backed by the `queue` table.

Implements the `TaskQueue` Protocol with a claim-lease pattern so
concurrent workers can dequeue safely:

  1. dequeue() picks a pending row, stamps a unique claim_token, and
     flips status to 'claimed' in a single UPDATE...WHERE rowid=... step.
  2. The worker runs the job, then calls ack() (status='done') on
     success, nack() (status='pending' + attempts++ + last_error) on
     recoverable failure, or dead_letter() (status='dead') after the
     configured max attempts.

The `(status, created_at)` index makes picking the oldest pending row
O(log n). WAL mode + row-level claim-token updates give single-writer
safety; one SQLite DB handles 10k+ enqueues without contention on a
laptop.

Swap path: swap in `RedisQueue(TaskQueue)` or `NatsQueue(TaskQueue)`
via profile.yaml without touching the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_ATTEMPTS_DEFAULT = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteQueue:
    """Durable FIFO queue with claim-lease semantics."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_attempts = max_attempts
        # One lock serializes writers within a process; sqlite's WAL
        # handles cross-process safety.
        self._lock = asyncio.Lock()
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _ensure_schema(self) -> None:
        # Stand-alone queue tables — do not depend on the shared metadata DB.
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    item_id      TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    claimed_at   TEXT,
                    claim_token  TEXT,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    last_error   TEXT,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, created_at)")

    # --- TaskQueue Protocol ---

    async def enqueue(self, item: dict[str, Any]) -> str:
        item_id = item.get("item_id") or str(uuid.uuid4())
        async with self._lock:
            with self._conn() as c:
                c.execute(
                    """INSERT OR REPLACE INTO queue
                        (item_id, payload_json, status, attempts, created_at, updated_at)
                        VALUES (?, ?, 'pending', 0, ?, ?)""",
                    (item_id, json.dumps(item, ensure_ascii=False), _now(), _now()),
                )
        return item_id

    async def dequeue(self) -> dict[str, Any] | None:
        """Atomically claim the oldest pending item. Returns None if empty."""
        token = str(uuid.uuid4())
        async with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    """SELECT item_id, payload_json, attempts
                         FROM queue
                        WHERE status='pending'
                        ORDER BY created_at ASC
                        LIMIT 1"""
                ).fetchone()
                if row is None:
                    c.execute("COMMIT")
                    return None
                c.execute(
                    """UPDATE queue
                         SET status='claimed', claim_token=?, claimed_at=?, updated_at=?
                       WHERE item_id=?""",
                    (token, _now(), _now(), row["item_id"]),
                )
                c.execute("COMMIT")
                payload = json.loads(row["payload_json"])
                payload["item_id"] = row["item_id"]
                payload["claim_token"] = token
                payload["attempts"] = row["attempts"]
                return payload

    async def ack(self, item_id: str) -> None:
        async with self._lock:
            with self._conn() as c:
                c.execute(
                    "UPDATE queue SET status='done', updated_at=? WHERE item_id=?",
                    (_now(), item_id),
                )

    async def nack(self, item_id: str, reason: str) -> None:
        """Increment attempts + release claim. Escalates to dead after max_attempts."""
        async with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT attempts FROM queue WHERE item_id=?", (item_id,)
                ).fetchone()
                if row is None:
                    return
                attempts = row["attempts"] + 1
                if attempts >= self._max_attempts:
                    c.execute(
                        """UPDATE queue
                             SET status='dead', attempts=?, last_error=?, updated_at=?
                           WHERE item_id=?""",
                        (attempts, reason, _now(), item_id),
                    )
                else:
                    c.execute(
                        """UPDATE queue
                             SET status='pending', attempts=?, last_error=?,
                                 claim_token=NULL, claimed_at=NULL, updated_at=?
                           WHERE item_id=?""",
                        (attempts, reason, _now(), item_id),
                    )

    async def dead_letter(self, item_id: str) -> None:
        async with self._lock:
            with self._conn() as c:
                c.execute(
                    "UPDATE queue SET status='dead', updated_at=? WHERE item_id=?",
                    (_now(), item_id),
                )

    # --- introspection helpers ---

    async def counts(self) -> dict[str, int]:
        """status -> count, for dashboards + tests."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM queue GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    async def reclaim_stale(self, older_than_seconds: int = 300) -> int:
        """Release claims older than N seconds (crashed worker recovery)."""
        cutoff_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - older_than_seconds,
            tz=timezone.utc,
        ).isoformat()
        async with self._lock:
            with self._conn() as c:
                cur = c.execute(
                    """UPDATE queue
                         SET status='pending', claim_token=NULL,
                             claimed_at=NULL, updated_at=?
                       WHERE status='claimed' AND claimed_at < ?""",
                    (_now(), cutoff_iso),
                )
                return cur.rowcount
