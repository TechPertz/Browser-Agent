"""GET /api/connections. Lists hosts that have sealed storage_state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from andera.credentials import SealedStateStore

router = APIRouter()


@router.get("/api/connections")
async def list_connections() -> dict[str, Any]:
    store = SealedStateStore()
    return {"hosts": store.list_hosts()}
