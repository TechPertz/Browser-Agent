"""Tool call / result envelopes.

Every agent-to-tool interaction goes through these types so the audit
log can record exactly what was called, with what inputs, and what
came back. Pydantic validates at the seam.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolCall(BaseModel):
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str
    timestamp: datetime = Field(default_factory=_utcnow)


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    status: Literal["ok", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)
