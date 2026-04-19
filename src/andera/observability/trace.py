"""Always-on local trace sink — one JSONL line per meaningful event.

Runs regardless of whether Langfuse is enabled. This is the offline
fallback: logs end up under `data/traces/<date>.jsonl` and can be
inspected with `jq` or replayed into Langfuse later.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlTraceSink:
    def __init__(self, root: str | Path = "data/traces") -> None:
        self.root = Path(root)
        # Best-effort init; tests/cwd changes may require a lazy remake.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _path_for_today(self) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Re-ensure the dir each write so cwd changes (common in tests)
        # don't crash the sink.
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{date}.jsonl"

    def write(self, event: dict[str, Any]) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        try:
            with self._path_for_today().open("a") as f:
                f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        except OSError:
            # Telemetry failure must never crash the run.
            pass


_sink: JsonlTraceSink | None = None


def get_trace_sink() -> JsonlTraceSink:
    global _sink
    if _sink is None:
        _sink = JsonlTraceSink()
    return _sink
