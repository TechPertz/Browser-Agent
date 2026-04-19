from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SampleStatus = Literal["pending", "running", "completed", "failed", "dead_lettered"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sample(BaseModel):
    sample_id: str
    run_id: str
    row_index: int = Field(ge=0)
    input_data: dict[str, Any]
    status: SampleStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    error: str | None = None
    extracted: dict[str, Any] | None = None
    evidence_dir: str | None = None
