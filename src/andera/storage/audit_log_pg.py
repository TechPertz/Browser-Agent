"""Postgres-backed AuditLog.

Same public interface as the SQLite impl (`append`, `verify_chain`,
`root_hash`, `rows_for_run`) but with:

  - One shared Postgres instance across ALL runs — not one DB file
    per run. Cleaner for multi-container stacks.
  - Per-run hash chain serialized via `pg_advisory_xact_lock(
    hashtext(run_id))`. Multiple agents writing within the same run
    don't race the chain; across different runs writes parallelize.
  - Async-first via `asyncpg` connection pool.

Events without a run_id use a synthetic lock key of 0 (shared global
chain for those). In practice every sample audit event carries a
run_id, so this path is only hit by pre-run bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit_log import GENESIS_HASH, _canonical, _hash

# Idempotent DDL — callers usually run `andera migrate` once, but
# AuditLogPg itself runs it on first connection too for zero-ceremony
# dev. Safe to call multiple times.
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    ordinal      BIGSERIAL PRIMARY KEY,
    event_id     TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    run_id       TEXT,
    sample_id    TEXT,
    timestamp    TIMESTAMPTZ NOT NULL,
    payload_json JSONB NOT NULL,
    prev_hash    TEXT,
    this_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_run_ordinal ON audit_log(run_id, ordinal);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _advisory_key(run_id: str | None) -> int:
    """Lock key for pg_advisory_xact_lock. `hashtext()` returns int32;
    cast to bigint for the single-arg overload."""
    if not run_id:
        return 0
    # Python-side hash matching PG's hashtext-ish semantics — we don't
    # need PG's exact algorithm, just a stable int per run_id. Use a
    # small sha and fold to bigint range.
    h = hashlib.sha256(run_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(h, "big", signed=True)


class AuditLogPg:
    """Postgres-backed hash-chained audit log."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._schema_ensured = False

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        if not self._schema_ensured:
            async with self._pool.acquire() as conn:
                await conn.execute(_PG_SCHEMA)
            self._schema_ensured = True
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = None

    async def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        sample_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or str(uuid.uuid4())
        ts = _utcnow()
        payload = payload or {}
        pool = await self._ensure_pool()
        lock_key = _advisory_key(run_id)
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize writers for this run so prev_hash read + insert
                # is atomic vs other appenders. Different runs get different
                # lock keys and parallelize.
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
                if run_id is None:
                    row = await conn.fetchrow(
                        "SELECT this_hash FROM audit_log "
                        "WHERE run_id IS NULL ORDER BY ordinal DESC LIMIT 1"
                    )
                else:
                    row = await conn.fetchrow(
                        "SELECT this_hash FROM audit_log "
                        "WHERE run_id = $1 ORDER BY ordinal DESC LIMIT 1",
                        run_id,
                    )
                prev = row["this_hash"] if row else GENESIS_HASH
                this_hash = _hash(prev, event_id, kind, ts.isoformat(), payload)
                await conn.execute(
                    """INSERT INTO audit_log
                        (event_id, kind, run_id, sample_id, timestamp,
                         payload_json, prev_hash, this_hash)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)""",
                    event_id, kind, run_id, sample_id, ts,
                    _canonical(payload), prev, this_hash,
                )
        return this_hash

    async def root_hash(self, run_id: str | None = None) -> str:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if run_id is None:
                row = await conn.fetchrow(
                    "SELECT this_hash FROM audit_log ORDER BY ordinal DESC LIMIT 1"
                )
            else:
                row = await conn.fetchrow(
                    "SELECT this_hash FROM audit_log WHERE run_id=$1 "
                    "ORDER BY ordinal DESC LIMIT 1",
                    run_id,
                )
        return row["this_hash"] if row else GENESIS_HASH

    async def verify_chain(self, run_id: str | None = None) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if run_id is None:
                rows = await conn.fetch(
                    "SELECT event_id, kind, run_id, sample_id, timestamp, "
                    "payload_json, prev_hash, this_hash "
                    "FROM audit_log ORDER BY ordinal ASC"
                )
            else:
                rows = await conn.fetch(
                    "SELECT event_id, kind, run_id, sample_id, timestamp, "
                    "payload_json, prev_hash, this_hash "
                    "FROM audit_log WHERE run_id=$1 ORDER BY ordinal ASC",
                    run_id,
                )
        expected_prev = GENESIS_HASH
        for r in rows:
            if r["prev_hash"] != expected_prev:
                return False
            payload = r["payload_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            recomputed = _hash(
                r["prev_hash"], r["event_id"], r["kind"],
                r["timestamp"].isoformat(), payload,
            )
            if recomputed != r["this_hash"]:
                return False
            expected_prev = r["this_hash"]
        return True

    async def rows_for_run(self, run_id: str) -> list[dict[str, Any]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_id, kind, run_id, sample_id, timestamp, "
                "payload_json, prev_hash, this_hash "
                "FROM audit_log WHERE run_id=$1 ORDER BY ordinal ASC",
                run_id,
            )
        return [dict(r) for r in rows]
