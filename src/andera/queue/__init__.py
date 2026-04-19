"""Queue package — TaskQueue Protocol implementations + a backend factory.

The factory lets the orchestrator pick an implementation based on
`profile.queue.backend` without hardcoding either import at the call
site. That's how the same `andera run` command can fan out samples
through SQLite on a laptop or Redis across 50 worker pods — no code
change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sqlite_queue import SqliteQueue

__all__ = ["SqliteQueue", "make_queue"]


def make_queue(
    *,
    backend: str,
    run_id: str,
    sqlite_path: str | Path | None = None,
    redis_url: str | None = None,
    redis_prefix: str | None = None,
    max_attempts: int = 3,
) -> Any:
    """Construct a TaskQueue for the given run based on backend selection.

    SQLite:  one DB file per run (`data/<run_id>.queue.db` by default).
    Redis:   one prefix per run (`andera:queue:<run_id>`), shared instance.
    """
    backend = (backend or "sqlite").lower()

    if backend == "sqlite":
        path = sqlite_path or (Path("data") / f"{run_id}.queue.db")
        return SqliteQueue(path, max_attempts=max_attempts)

    if backend == "redis":
        from .redis_queue import RedisQueue
        url = redis_url or "redis://localhost:6379/0"
        prefix = redis_prefix or f"andera:queue:{run_id}"
        return RedisQueue(url, prefix=prefix, max_attempts=max_attempts)

    raise ValueError(f"unknown queue backend: {backend!r}")
