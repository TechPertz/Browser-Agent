from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

EventKind = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "sample.started",
    "sample.completed",
    "sample.failed",
    "sample.retry",
    "tool.called",
    "tool.result",
    "node.entered",
    "node.exited",
    "audit.appended",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    event_id: str
    kind: EventKind
    run_id: str | None = None
    sample_id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    this_hash: str | None = None
