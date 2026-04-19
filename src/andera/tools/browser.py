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
            artifact = await self._session.screenshot(args.name)
            return {"artifact": artifact.model_dump(mode="json")}

        return await invoke("browser.screenshot", args.model_dump(), run)

    async def extract(self, args: ExtractArgs) -> ToolResult:
        async def run():
            return await self._session.extract(args.json_schema)

        return await invoke("browser.extract", args.model_dump(), run)

    async def snapshot(self) -> ToolResult:
        async def run():
            return await self._session.snapshot()

        return await invoke("browser.snapshot", {}, run)
