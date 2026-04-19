"""SQLite connection factory + schema initializer.

WAL mode for concurrent readers. Foreign keys on. Row factory set to
`sqlite3.Row` so callers can access columns by name.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def init_db(db_path: str | Path = "./data/state.db") -> Path:
    """Apply schema idempotently. Safe to call on an existing DB."""
    p = Path(db_path)
    _ensure_parent(p)
    conn = sqlite3.connect(p)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    return p


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    p = Path(db_path)
    _ensure_parent(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
    finally:
        conn.close()
