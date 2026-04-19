"""Shared tool invocation helper: times, envelopes, and error-wraps a call.

Keeps the call-site for each tool small and ensures every tool invocation
produces a ToolResult with consistent shape.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from andera.contracts import ToolCall, ToolResult


async def invoke(
    tool_name: str,
    args: dict[str, Any],
    fn: Callable[[], Awaitable[dict[str, Any]]],
) -> ToolResult:
    call = ToolCall(tool_name=tool_name, args=args, call_id=str(uuid.uuid4()))
    start = time.perf_counter()
    try:
        data = await fn()
        return ToolResult(
            call_id=call.call_id,
            tool_name=tool_name,
            status="ok",
            data=data,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as e:  # noqa: BLE001 — intentionally broad; tool contract normalizes errors
        return ToolResult(
            call_id=call.call_id,
            tool_name=tool_name,
            status="error",
            error=f"{type(e).__name__}: {e}",
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
