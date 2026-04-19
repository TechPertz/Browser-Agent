"""Coverage for the new scroll / viewport-vs-full / screenshot_all actions.

These are the evidence-capture tools the planner picks between. We
verify the args get plumbed correctly, not Playwright mechanics.
"""
from andera.contracts import Artifact
from andera.tools.browser import (
    BrowserTools,
    ScreenshotArgs,
    ScrollArgs,
    ScrollToArgs,
)


class _Session:
    def __init__(self):
        self.calls = []

    async def goto(self, url): ...
    async def click(self, s): ...
    async def type(self, s, v): ...
    async def extract(self, s): return {}
    async def snapshot(self): return {"url": "x", "title": "t"}
    async def close(self): ...

    async def screenshot(self, name, *, full_page=True, folder=None):
        self.calls.append(("screenshot", name, full_page, folder))
        return Artifact(
            sha256="a" * 64, name=f"{name}.png", mime="image/png",
            size=4, path=f"/tmp/{name}.png",
        )

    async def scroll(self, amount):
        self.calls.append(("scroll", amount))
        return {"y": 900, "page_height": 3000, "viewport_height": 900,
                "at_bottom": False}

    async def scroll_to(self, target):
        self.calls.append(("scroll_to", target))
        return {"found": True, "y": 1200, "target": target}

    async def screenshot_chunks(self, name, *, folder=None):
        self.calls.append(("screenshot_chunks", name, folder))
        return [
            Artifact(
                sha256=("b" * 63) + str(i), name=f"{name}_chunk{i:02d}.png",
                mime="image/png", size=4, path=f"/tmp/{name}_{i}.png",
            )
            for i in range(3)
        ]


async def test_screenshot_default_is_viewport():
    s = _Session()
    out = await BrowserTools(s).screenshot(ScreenshotArgs(name="x"))
    assert out.status == "ok"
    # mode defaults to viewport → full_page=False reaches the session.
    # folder defaults to None.
    assert s.calls == [("screenshot", "x", False, None)]
    assert out.data["mode"] == "viewport"


async def test_screenshot_full_mode():
    s = _Session()
    out = await BrowserTools(s).screenshot(ScreenshotArgs(name="x", mode="full"))
    assert out.status == "ok"
    assert s.calls == [("screenshot", "x", True, None)]


async def test_screenshot_with_folder():
    s = _Session()
    out = await BrowserTools(s).screenshot(
        ScreenshotArgs(name="pr_01", folder="facebook-react"),
    )
    assert out.status == "ok"
    assert out.data["folder"] == "facebook-react"
    # folder is passed through to the session.
    assert s.calls == [("screenshot", "pr_01", False, "facebook-react")]


async def test_scroll_down_default():
    s = _Session()
    out = await BrowserTools(s).scroll(ScrollArgs())
    assert out.status == "ok"
    assert out.data["at_bottom"] is False
    assert s.calls == [("scroll", "down")]


async def test_scroll_to_text():
    s = _Session()
    out = await BrowserTools(s).scroll_to(ScrollToArgs(target="Sign in"))
    assert out.status == "ok"
    assert out.data["found"] is True
    assert s.calls == [("scroll_to", "Sign in")]


async def test_screenshot_all_returns_multiple_artifacts():
    s = _Session()
    out = await BrowserTools(s).screenshot_all(ScreenshotArgs(name="walk"))
    assert out.status == "ok"
    assert out.data["chunks"] == 3
    assert len(out.data["artifacts"]) == 3
    assert s.calls == [("screenshot_chunks", "walk", None)]
