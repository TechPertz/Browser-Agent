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


class VisitEachLinkArgs(BaseModel):
    """Iterate over matching links on the current page, visit + screenshot
    each. Useful for "top N items in a listing" flows — the planner emits
    ONE step, deterministic code handles the loop."""
    # substring that must appear in href (e.g. "/pull/", "/issues/", "/p/")
    url_pattern: str
    limit: int = 10
    # Python format string; `{i}` or `{i:02d}` substituted per iteration.
    # May include a slash to place shots in a subfolder.
    name_template: str = "item_{i:02d}"
    folder: str | None = None


class SearchArgs(BaseModel):
    """Google search via the Serper API — bypasses the entire anti-bot
    fight with Google/DDG/Bing landing pages. Returns structured JSON
    (title, url, snippet) for the top N organic results."""
    query: str
    limit: int = 5


class GotoSearchResultArgs(BaseModel):
    """Goto the first result from the most recent search whose URL
    contains `url_filter`. Paired with `search` to do the two-step dance
    (search -> navigate to first matching result) without the planner
    trying to reference future values from a static plan."""
    # Substring that must appear in the result URL. Empty -> no filter,
    # just take the Nth result.
    url_filter: str = ""
    # 0-based index into the filtered list of results.
    index: int = 0


class ExtractArgs(BaseModel):
    json_schema: dict[str, Any]


class AnnotateArgs(BaseModel):
    """Draw numbered overlay boxes on every interactive element and
    capture the resulting screenshot. Used as the setup step for any
    visual_do — the act node calls this, stores the marks list in
    state, then asks the vision LMM to pick a mark_id."""
    name: str = "annotated"


class ClickMarkArgs(BaseModel):
    mark_id: int


class TypeMarkArgs(BaseModel):
    mark_id: int
    value: str


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

    async def search(self, args: SearchArgs) -> ToolResult:
        """Hit Serper (Google-results-as-JSON). Requires SERPER_API_KEY."""
        async def run():
            import os

            import httpx

            key = os.environ.get("SERPER_API_KEY")
            if not key:
                raise RuntimeError(
                    "SERPER_API_KEY is not set in .env — add it or use a "
                    "different search strategy"
                )
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={
                        "X-API-KEY": key,
                        "Content-Type": "application/json",
                    },
                    json={"q": args.query, "num": args.limit},
                )
                resp.raise_for_status()
                data = resp.json()
            organic = data.get("organic") or []
            results = [
                {
                    "title": h.get("title") or "",
                    "url": h.get("link") or "",
                    "snippet": h.get("snippet") or "",
                }
                for h in organic[: args.limit]
            ]
            return {
                "query": args.query,
                "results": results,
                "count": len(results),
            }

        return await invoke("browser.search", args.model_dump(), run)

    async def visit_each_link(self, args: VisitEachLinkArgs) -> ToolResult:
        async def run():
            return await self._session.visit_each_link(
                url_pattern=args.url_pattern,
                limit=args.limit,
                name_template=args.name_template,
                folder=args.folder,
            )

        return await invoke("browser.visit_each_link", args.model_dump(), run)

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

    async def annotate(self, args: AnnotateArgs) -> ToolResult:
        """Set-of-Mark overlay + screenshot. Returns the artifact + marks
        list in ToolResult.data. Caller (act node's visual_do handler)
        feeds the marks into the vision resolver or the descriptor
        matcher to pick which mark to click."""
        async def run():
            art, marks = await self._session.mark_and_screenshot(args.name)
            return {
                "artifact": art.model_dump(mode="json"),
                "marks": marks,
                "count": len(marks),
            }

        return await invoke("browser.annotate", args.model_dump(), run)

    async def click_mark(self, args: ClickMarkArgs) -> ToolResult:
        async def run():
            await self._session.click_mark(args.mark_id)
            return {"mark_id": args.mark_id}

        return await invoke("browser.click_mark", args.model_dump(), run)

    async def type_mark(self, args: TypeMarkArgs) -> ToolResult:
        async def run():
            await self._session.type_mark(args.mark_id, args.value)
            return {"mark_id": args.mark_id, "chars": len(args.value)}

        return await invoke("browser.type_mark", args.model_dump(mode="json"), run)
