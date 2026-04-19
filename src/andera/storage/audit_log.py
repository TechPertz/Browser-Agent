"""Append-only hash-chained audit log.

Each row stores `prev_hash` + `this_hash`. Tampering with any row
invalidates every row after it, so `verify_chain()` is a cheap
integrity check auditors can re-run at any time.

Schema lives in storage/schema.sql:audit_log. This module only
writes + verifies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENESIS_HASH = "0" * 64


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(prev_hash: str, event_id: str, kind: str, timestamp: str, payload: dict[str, Any]) -> str:
    blob = "\x1f".join([prev_hash, event_id, kind, timestamp, _canonical(payload)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditLog:
    """Thin writer/verifier over the audit_log table.

    Safe under concurrent writers: each append runs in an IMMEDIATE
    transaction so the prev_hash we read matches the this_hash we write
    (no lost-update race).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    event_id     TEXT PRIMARY KEY,
                    kind         TEXT NOT NULL,
                    run_id       TEXT,
                    sample_id    TEXT,
                    timestamp    TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash    TEXT,
                    this_hash    TEXT NOT NULL,
                    rowid_hint   INTEGER
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp)")

    def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        sample_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Append one row; returns the new this_hash."""
        event_id = event_id or str(uuid.uuid4())
        ts = _utcnow()
        payload = payload or {}
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT this_hash FROM audit_log ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            prev = row["this_hash"] if row else GENESIS_HASH
            this_hash = _hash(prev, event_id, kind, ts, payload)
            c.execute(
                """INSERT INTO audit_log
                    (event_id, kind, run_id, sample_id, timestamp, payload_json, prev_hash, this_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, kind, run_id, sample_id, ts, _canonical(payload), prev, this_hash),
            )
            c.execute("COMMIT")
        return this_hash

    def root_hash(self, run_id: str | None = None) -> str:
        """Latest this_hash (optionally scoped to a run)."""
        with self._conn() as c:
            if run_id is None:
                row = c.execute(
                    "SELECT this_hash FROM audit_log ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT this_hash FROM audit_log WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
        return row["this_hash"] if row else GENESIS_HASH

    def verify_chain(self) -> bool:
        """Walk the entire log; recompute every this_hash; confirm integrity."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT event_id, kind, run_id, sample_id, timestamp, payload_json, prev_hash, this_hash "
                "FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
        expected_prev = GENESIS_HASH
        for r in rows:
            if r["prev_hash"] != expected_prev:
                return False
            payload = json.loads(r["payload_json"])
            recomputed = _hash(r["prev_hash"], r["event_id"], r["kind"], r["timestamp"], payload)
            if recomputed != r["this_hash"]:
                return False
            expected_prev = r["this_hash"]
        return True

    def rows_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT event_id, kind, run_id, sample_id, timestamp, payload_json, prev_hash, this_hash "
                "FROM audit_log WHERE run_id=? ORDER BY rowid ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]
