from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Artifact(BaseModel):
    """Content-addressed evidence artifact (screenshot, download, DOM snapshot, etc)."""

    sha256: str = Field(min_length=64, max_length=64)
    name: str
    mime: str
    size: int = Field(ge=0)
    path: str
    sample_id: str | None = None
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
