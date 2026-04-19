"""WebSocket /api/events?run_id=<id>. Streams audit events as JSON."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..ws import get_bus

router = APIRouter()


@router.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    run_id = ws.query_params.get("run_id") or None
    bus = get_bus()
    q = bus.subscribe(run_id)
    try:
        # initial hello so clients know the stream is live
        await ws.send_text(json.dumps({"kind": "ws.ready", "run_id": run_id}))
        while True:
            # Time-bound wait so we can ping the client periodically and detect disconnects.
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
                await ws.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"kind": "ws.ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        # client died mid-send; just close
        pass
    finally:
        bus.unsubscribe(q, run_id)
        try:
            await ws.close()
        except Exception:
            pass
