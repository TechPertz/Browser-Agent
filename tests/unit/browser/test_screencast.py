"""Screencaster tests with a fake Playwright Page + CDP session."""

import pytest

from andera.browser.screencast import Screencaster


class _FakeCDP:
    def __init__(self):
        self.sent = []
        self._handler = None

    def on(self, event, handler):
        self._handler = handler

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))

    async def detach(self):
        self.sent.append(("__detached__", {}))

    # test helper
    async def fire_frame(self, data, session_id=1):
        if self._handler:
            await self._handler({"data": data, "sessionId": session_id})


class _FakeContext:
    def __init__(self, cdp):
        self._cdp = cdp

    async def new_cdp_session(self, page):
        return self._cdp


class _FakePage:
    def __init__(self, cdp):
        self.context = _FakeContext(cdp)


@pytest.mark.asyncio
async def test_start_sends_startScreencast_with_jpeg_and_quality():
    cdp = _FakeCDP()
    page = _FakePage(cdp)
    frames = []
    cast = Screencaster(page, sample_id="s1", publish=lambda e: frames.append(e), fps=10, quality=55)
    await cast.start()
    assert any(m == "Page.startScreencast" for m, _ in cdp.sent)
    _, params = next((m, p) for m, p in cdp.sent if m == "Page.startScreencast")
    assert params["format"] == "jpeg"
    assert params["quality"] == 55


@pytest.mark.asyncio
async def test_frame_published_on_bus():
    cdp = _FakeCDP()
    page = _FakePage(cdp)
    frames = []
    cast = Screencaster(page, sample_id="s1", publish=lambda e: frames.append(e))
    await cast.start()
    await cdp.fire_frame("base64-jpeg-data", session_id=42)
    assert len(frames) == 1
    ev = frames[0]
    assert ev["kind"] == "screencast.frame"
    assert ev["sample_id"] == "s1"
    assert ev["data"] == "base64-jpeg-data"
    # Acked the session id so Chromium keeps sending
    assert any(m == "Page.screencastFrameAck" for m, _ in cdp.sent)


@pytest.mark.asyncio
async def test_stop_sends_stopScreencast_and_detaches():
    cdp = _FakeCDP()
    page = _FakePage(cdp)
    cast = Screencaster(page, sample_id="s1", publish=lambda e: None)
    await cast.start()
    await cast.stop()
    methods = [m for m, _ in cdp.sent]
    assert "Page.stopScreencast" in methods
    assert "__detached__" in methods


@pytest.mark.asyncio
async def test_fps_clamped_to_every_nth_frame():
    cdp = _FakeCDP()
    page = _FakePage(cdp)
    cast = Screencaster(page, sample_id="s1", publish=lambda e: None, fps=30)
    await cast.start()
    _, params = next((m, p) for m, p in cdp.sent if m == "Page.startScreencast")
    assert params["everyNthFrame"] == 1
    # fps=5 -> everyNthFrame = 30//5 = 6
    cdp2 = _FakeCDP()
    cast2 = Screencaster(_FakePage(cdp2), sample_id="s2", publish=lambda e: None, fps=5)
    await cast2.start()
    _, p2 = next((m, p) for m, p in cdp2.sent if m == "Page.startScreencast")
    assert p2["everyNthFrame"] == 6
