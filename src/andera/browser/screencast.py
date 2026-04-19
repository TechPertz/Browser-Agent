"""CDP screencast — live JPEG frames from a running Playwright page.

Playwright Python doesn't surface `Page.startScreencast` in its
high-level API, but we can drive it over the Chrome DevTools Protocol
directly. Each frame comes back as a base64-encoded JPEG which we
publish on the EventBus keyed by sample_id.

Subscribers (WebSocket at /api/screencast?sample_id=...) receive a
stream of base64 strings to stuff into `<img src="data:image/jpeg;base64,..." />`.

Frames are lossy by design: at ~10 FPS on a laptop the signal-to-noise
is fine for a reviewer watching the agent drive the browser. If an
overloaded UI can't keep up, frames drop (EventBus evicts slow subs).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from playwright.async_api import Page


class Screencaster:
    """Bound to one Page; start() begins streaming frames to `publish`."""

    def __init__(
        self,
        page: Page,
        *,
        sample_id: str,
        publish: Callable[[dict[str, Any]], None],
        fps: int = 10,
        quality: int = 60,
        max_width: int = 1024,
    ) -> None:
        self._page = page
        self._sample_id = sample_id
        self._publish = publish
        self._fps = max(1, min(fps, 30))
        self._quality = max(10, min(quality, 100))
        self._max_width = max_width
        self._cdp: Any = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        # Each tab needs its own CDP session in Chromium.
        self._cdp = await self._page.context.new_cdp_session(self._page)

        async def _on_frame(params: dict[str, Any]) -> None:
            try:
                data = params.get("data")
                session_id = params.get("sessionId")
                if data:
                    self._publish({
                        "kind": "screencast.frame",
                        "sample_id": self._sample_id,
                        "data": data,  # already base64
                    })
                if session_id is not None:
                    try:
                        await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                    except Exception:
                        pass
            except Exception:
                pass

        self._cdp.on("Page.screencastFrame", _on_frame)
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": self._quality,
            "maxWidth": self._max_width,
            "everyNthFrame": max(1, 30 // self._fps),
        })
        self._running = True

    async def stop(self) -> None:
        if not self._running or self._cdp is None:
            return
        try:
            await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await self._cdp.detach()
        except Exception:
            pass
        self._running = False


async def run_with_screencast(
    session,
    *,
    sample_id: str,
    publish: Callable[[dict[str, Any]], None],
    coro_factory: Callable[[], Any],
    fps: int = 10,
) -> Any:
    """Run `coro_factory()` with screencast active on the session's page."""
    page = getattr(session, "_page", None)
    if page is None:
        # Session doesn't expose a Page (e.g., non-local backend); run bare.
        return await coro_factory()
    cast = Screencaster(page, sample_id=sample_id, publish=publish, fps=fps)
    await cast.start()
    try:
        return await coro_factory()
    finally:
        await cast.stop()
