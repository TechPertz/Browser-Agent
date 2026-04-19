"""Agent-facing browser tools — typed Pydantic I/O over a BrowserSession.

Every public method returns a ToolResult so the orchestrator's audit
log can record the call, args, outcome, and elapsed time uniformly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from andera.contracts import BrowserSession, ToolResult

from ._runner import invoke


class GotoArgs(BaseModel):
    url: str


class ClickArgs(BaseModel):
    selector_or_text: str


class TypeArgs(BaseModel):
    selector: str
    value: str


class ScreenshotArgs(BaseModel):
    name: str
    # viewport = what the user sees (small, default). full = whole scrollable
    # document (big, evidence-grade). Planner picks based on task wording.
    mode: str = "viewport"
    # Optional per-item subfolder under the run root. Use when the task
    # says "save each item's evidence under a folder named X" — planner
    # derives the slug from input_data and passes it here. The
    # content-addressed blob is still written (audit / dedup), AND a
    # hardlink is placed at runs/<run_id>/<folder>/<name>.png.
    folder: str | None = None


class ScrollArgs(BaseModel):
    # 'up' | 'down' | 'top' | 'bottom' | stringified int px
    amount: str = "down"


class ScrollToArgs(BaseModel):
    # visible text (preferred) OR css/xpath selector
    target: str


class ExtractArgs(BaseModel):
    json_schema: dict[str, Any]


class BrowserTools:
    """Bind a set of agent-facing browser tools to one BrowserSession."""

    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    async def goto(self, args: GotoArgs) -> ToolResult:
        async def run():
            await self._session.goto(args.url)
            return {"url": args.url}

        return await invoke("browser.goto", args.model_dump(), run)

    async def click(self, args: ClickArgs) -> ToolResult:
        async def run():
            await self._session.click(args.selector_or_text)
            return {"selector_or_text": args.selector_or_text}

        return await invoke("browser.click", args.model_dump(), run)

    async def type(self, args: TypeArgs) -> ToolResult:
        async def run():
            await self._session.type(args.selector, args.value)
            return {"selector": args.selector, "chars": len(args.value)}

        return await invoke("browser.type", args.model_dump(mode="json"), run)

    async def screenshot(self, args: ScreenshotArgs) -> ToolResult:
        async def run():
            full = (args.mode == "full")
            artifact = await self._session.screenshot(
                args.name, full_page=full, folder=args.folder,
            )
            return {
                "artifact": artifact.model_dump(mode="json"),
                "mode": args.mode,
                "folder": args.folder,
            }

        return await invoke("browser.screenshot", args.model_dump(), run)

    async def scroll(self, args: ScrollArgs) -> ToolResult:
        async def run():
            return await self._session.scroll(args.amount)

        return await invoke("browser.scroll", args.model_dump(), run)

    async def scroll_to(self, args: ScrollToArgs) -> ToolResult:
        async def run():
            return await self._session.scroll_to(args.target)

        return await invoke("browser.scroll_to", args.model_dump(), run)

    async def screenshot_all(self, args: ScreenshotArgs) -> ToolResult:
        """Walk the page top→bottom and capture viewport chunks. The
        `mode` field on ScreenshotArgs is ignored — all chunks are
        viewport-sized by construction. Returns artifacts[] + chunk count."""
        async def run():
            arts = await self._session.screenshot_chunks(
                args.name, folder=args.folder,
            )
            return {
                "artifacts": [a.model_dump(mode="json") for a in arts],
                "chunks": len(arts),
                "folder": args.folder,
            }

        return await invoke("browser.screenshot_all", args.model_dump(), run)

    async def extract(self, args: ExtractArgs) -> ToolResult:
        async def run():
            return await self._session.extract(args.json_schema)

        return await invoke("browser.extract", args.model_dump(), run)

    async def snapshot(self) -> ToolResult:
        async def run():
            return await self._session.snapshot()

        return await invoke("browser.snapshot", {}, run)
