"""LocalPlaywrightSession — `BrowserSession` Protocol against local Chromium.

Each session owns one Playwright browser context + page. The session is
the unit of per-sample isolation: one sample, one context, no cookie
bleed. Screenshots go through the injected ArtifactStore so every
evidence file is content-addressed.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from andera.contracts import Artifact, ArtifactStore

from .grounding import build_snapshot
from .set_of_mark import clear_marks, mark_and_screenshot, marks_to_list


class LocalPlaywrightSession:
    """One tab, one agent sample.

    Construct via `LocalPlaywrightSession.create(...)` (async). Always
    `await session.close()` when done (or use a pool that does it).
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        playwright_ctx: Any,
        sample_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._browser = browser
        self._context = context
        self._page = page
        self._pw = playwright_ctx
        self._sample_id = sample_id
        self._run_id = run_id

    @classmethod
    async def create(
        cls,
        *,
        artifacts: ArtifactStore,
        headless: bool = False,
        viewport: dict[str, int] | None = None,
        sample_id: str | None = None,
        run_id: str | None = None,
        storage_state: str | dict[str, Any] | None = None,
    ) -> "LocalPlaywrightSession":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs: dict[str, Any] = {}
        if viewport is not None:
            ctx_kwargs["viewport"] = viewport
        if storage_state is not None:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        return cls(
            artifacts=artifacts,
            browser=browser,
            context=context,
            page=page,
            playwright_ctx=pw,
            sample_id=sample_id,
            run_id=run_id,
        )

    # --- BrowserSession Protocol ---

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector_or_text: str) -> None:
        """Prefer CSS/XPath; fall back to text match via get_by_text."""
        try:
            await self._page.click(selector_or_text, timeout=5000)
        except Exception:
            await self._page.get_by_text(selector_or_text, exact=False).first.click()

    async def type(self, selector: str, value: str) -> None:
        await self._page.fill(selector, value)

    async def screenshot(self, name: str) -> Artifact:
        data = await self._page.screenshot(full_page=True)
        final_name = name if name.endswith(".png") else f"{name}.png"
        return await self._artifacts.put(
            data,
            final_name,
            mime="image/png",
            sample_id=self._sample_id,
            run_id=self._run_id,
        )

    async def extract(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Phase 1 scaffold: returns the page title + URL.

        Real extraction (LLM-driven over DOM + a11y tree) lands in Phase
        2 where the extractor role is wired in. Keeping a minimal
        implementation here lets the smoke test exercise the full pipe.
        """
        return {
            "url": self._page.url,
            "title": await self._page.title(),
            "_schema_keys": list(schema.get("properties", {}).keys()),
        }

    async def snapshot(self) -> dict[str, Any]:
        """Rich snapshot: url + title + inner text + a11y tree + interactives."""
        return await build_snapshot(self._page)

    async def mark_and_screenshot(self, name: str) -> tuple[Artifact, list[dict[str, Any]]]:
        """Set-of-Mark overlay + screenshot. Returns (artifact, marks list)."""
        png, marks = await mark_and_screenshot(self._page)
        final_name = name if name.endswith(".png") else f"{name}.png"
        art = await self._artifacts.put(
            png, final_name, mime="image/png",
            sample_id=self._sample_id, run_id=self._run_id,
        )
        self._last_marks = marks  # type: ignore[attr-defined]
        return art, marks_to_list(marks)

    async def click_mark(self, mark_id: int) -> None:
        """Click the center of a previously-marked element."""
        marks = getattr(self, "_last_marks", None) or {}
        m = marks.get(mark_id)
        if m is None:
            raise ValueError(f"no such mark: {mark_id} (did you call mark_and_screenshot first?)")
        cx = m.x + m.w // 2
        cy = m.y + m.h // 2
        await self._page.mouse.click(cx, cy)
        # Best-effort clear so successive screenshots don't contain stale overlays.
        try:
            await clear_marks(self._page)
        except Exception:
            pass

    async def close(self) -> None:
        await self._context.close()
        await self._browser.close()
        await self._pw.stop()
