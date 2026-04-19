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

GLOBAL_QUEUE_PREFIX = "andera:queue:global"

__all__ = ["GLOBAL_QUEUE_PREFIX", "SqliteQueue", "make_queue"]


def make_queue(
    *,
    backend: str,
    run_id: str | None = None,
    sqlite_path: str | Path | None = None,
    redis_url: str | None = None,
    redis_prefix: str | None = None,
    max_attempts: int = 3,
    global_queue: bool = False,
) -> Any:
    """Construct a TaskQueue.

    Run-scoped mode (default, `global_queue=False`):
      - SQLite: `data/<run_id>.queue.db`
      - Redis:  prefix `andera:queue:<run_id>`
      One queue per run — appropriate for SQLite or single-run Redis.

    Global mode (`global_queue=True`, Redis only):
      - Prefix `andera:queue:global`
      - ONE queue across ALL runs. Long-lived agent containers pull
        from here without knowing which run they're serving — each
        job carries run_id + task inline. This is the multi-agent
        service shape used by the Docker compose stack.
    """
    backend = (backend or "sqlite").lower()

    if backend == "sqlite":
        if global_queue:
            raise ValueError("global queue requires redis backend")
        if run_id is None:
            raise ValueError("sqlite queue needs a run_id")
        path = sqlite_path or (Path("data") / f"{run_id}.queue.db")
        return SqliteQueue(path, max_attempts=max_attempts)

    if backend == "redis":
        from .redis_queue import RedisQueue
        url = redis_url or "redis://localhost:6379/0"
        if global_queue:
            prefix = GLOBAL_QUEUE_PREFIX
        else:
            if run_id is None:
                raise ValueError("run-scoped redis queue needs a run_id")
            prefix = redis_prefix or f"andera:queue:{run_id}"
        return RedisQueue(url, prefix=prefix, max_attempts=max_attempts)

    raise ValueError(f"unknown queue backend: {backend!r}")
