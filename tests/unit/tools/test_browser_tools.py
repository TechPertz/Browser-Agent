from andera.contracts import Artifact
from andera.tools.browser import (
    BrowserTools,
    ClickArgs,
    ExtractArgs,
    GotoArgs,
    ScreenshotArgs,
    TypeArgs,
)


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def goto(self, url):
        self.calls.append(("goto", url))

    async def click(self, s):
        self.calls.append(("click", s))

    async def type(self, s, v):
        self.calls.append(("type", s, v))

    async def screenshot(self, name):
        self.calls.append(("screenshot", name))
        return Artifact(sha256="a" * 64, name=name, mime="image/png", size=4, path="/tmp/x.png")

    async def extract(self, schema):
        self.calls.append(("extract", schema))
        return {"title": "T"}

    async def snapshot(self):
        self.calls.append(("snapshot",))
        return {"url": "https://x", "title": "T"}


async def test_goto_ok():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.goto(GotoArgs(url="https://example.com"))
    assert out.status == "ok"
    assert out.tool_name == "browser.goto"
    assert out.data == {"url": "https://example.com"}
    assert session.calls == [("goto", "https://example.com")]


async def test_click():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.click(ClickArgs(selector_or_text="Submit"))
    assert out.status == "ok"


async def test_type():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.type(TypeArgs(selector="#q", value="hello"))
    assert out.data["chars"] == 5


async def test_screenshot_returns_artifact():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.screenshot(ScreenshotArgs(name="step.png"))
    assert out.status == "ok"
    assert out.data["artifact"]["sha256"] == "a" * 64


async def test_extract():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.extract(ExtractArgs(json_schema={"properties": {"title": {"type": "string"}}}))
    assert out.data == {"title": "T"}


async def test_snapshot():
    session = _FakeSession()
    tools = BrowserTools(session)
    out = await tools.snapshot()
    assert out.data["title"] == "T"


async def test_error_is_normalized():
    class Broken(_FakeSession):
        async def goto(self, url):
            raise RuntimeError("boom")

    tools = BrowserTools(Broken())
    out = await tools.goto(GotoArgs(url="https://x"))
    assert out.status == "error"
    assert "RuntimeError" in (out.error or "")
    assert out.elapsed_ms >= 0
