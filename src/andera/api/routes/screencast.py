"""WS /api/screencast?sample_id=<id>. Streams base64 JPEG frames to the UI.

Frames are published on the global EventBus by Screencaster; this route
just filters by sample_id and forwards the raw base64 payload so the
`<img src="data:image/jpeg;base64,...">` swap on the client is cheap.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..ws import get_bus

router = APIRouter()


@router.websocket("/api/screencast")
async def screencast_ws(ws: WebSocket) -> None:
    await ws.accept()
    sample_id = ws.query_params.get("sample_id")
    if not sample_id:
        await ws.close(code=1008, reason="sample_id required")
        return

    # Subscribe to ALL events (screencast frames carry sample_id in payload
    # but may not have run_id), and filter here.
    bus = get_bus()
    q = bus.subscribe(None, maxsize=64)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # keepalive so idle browsers don't trip proxies
                continue
            if event.get("kind") != "screencast.frame":
                continue
            if event.get("sample_id") != sample_id:
                continue
            data = event.get("data")
            if data:
                await ws.send_text(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q, None)
        try:
            await ws.close()
        except Exception:
            pass
