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
        owns_browser: bool = True,
    ) -> None:
        self._artifacts = artifacts
        self._browser = browser
        self._context = context
        self._page = page
        self._pw = playwright_ctx
        self._sample_id = sample_id
        self._run_id = run_id
        # When false (pool-managed case), close() only closes the context,
        # leaving the shared Browser process alive for the next sample.
        self._owns_browser = owns_browser

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
        stealth: bool = False,
        rate_limiter: Any = None,
    ) -> "LocalPlaywrightSession":
        from .stealth import apply_stealth, random_user_agent, random_viewport

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs: dict[str, Any] = {}
        if stealth:
            # Randomize UA + viewport per sample so fingerprints vary.
            ctx_kwargs["user_agent"] = random_user_agent()
            ctx_kwargs["viewport"] = random_viewport()
        elif viewport is not None:
            ctx_kwargs["viewport"] = viewport
        if storage_state is not None:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        if stealth:
            await apply_stealth(context)
        page = await context.new_page()
        inst = cls(
            artifacts=artifacts,
            browser=browser,
            context=context,
            page=page,
            playwright_ctx=pw,
            sample_id=sample_id,
            run_id=run_id,
        )
        inst._rate_limiter = rate_limiter  # type: ignore[attr-defined]
        return inst

    @classmethod
    async def from_browser(
        cls,
        *,
        browser: Browser,
        playwright_ctx: Any,
        artifacts: ArtifactStore,
        viewport: dict[str, int] | None = None,
        sample_id: str | None = None,
        run_id: str | None = None,
        storage_state: str | dict[str, Any] | None = None,
        stealth: bool = False,
        rate_limiter: Any = None,
    ) -> "LocalPlaywrightSession":
        """Build a session using a shared persistent Browser.

        Opens a fresh context + page (cheap, ~5ms). close() will close
        just the context — the shared browser stays alive for the next
        caller. This is the hot path when a BrowserPool is in use.
        """
        from .stealth import apply_stealth, random_user_agent, random_viewport

        ctx_kwargs: dict[str, Any] = {}
        if stealth:
            ctx_kwargs["user_agent"] = random_user_agent()
            ctx_kwargs["viewport"] = random_viewport()
        elif viewport is not None:
            ctx_kwargs["viewport"] = viewport
        if storage_state is not None:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        if stealth:
            await apply_stealth(context)
        page = await context.new_page()
        inst = cls(
            artifacts=artifacts,
            browser=browser,
            context=context,
            page=page,
            playwright_ctx=playwright_ctx,
            sample_id=sample_id,
            run_id=run_id,
            owns_browser=False,
        )
        inst._rate_limiter = rate_limiter  # type: ignore[attr-defined]
        return inst

    # --- BrowserSession Protocol ---

    async def goto(self, url: str) -> None:
        limiter = getattr(self, "_rate_limiter", None)
        if limiter is not None:
            await limiter.acquire(url)
        await self._page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector_or_text: str) -> None:
        """Prefer real selectors; fall back carefully to text match.

        Accuracy rule: a failed selector must not degrade into "click
        anything that contains this substring." That's how agents end
        up clicking 'Submit feedback' in the footer when the planner
        asked for the form's submit button.

        Strategy:
          1. If the input looks like a selector (CSS/XPath), use it
             strictly. Raise on failure rather than retrying as text.
          2. Otherwise, try get_by_role('button'/'link'/'menuitem'/'tab',
             name=text, exact=True).
          3. Fall back to get_by_text(text, exact=True) — but ONLY if
             it matches exactly ONE element. Ambiguous text never
             clicks; let the agent reflect and pick a different step.
        """
        s = selector_or_text.strip()
        looks_like_selector = (
            s.startswith(("#", ".", "/", "[", ":"))
            or ">" in s or "[" in s or "//" in s
        )
        if looks_like_selector:
            await self._page.click(s, timeout=5000)
            return

        for role in ("button", "link", "menuitem", "tab"):
            loc = self._page.get_by_role(role, name=s, exact=True)
            if await loc.count() == 1:
                await loc.click(timeout=5000)
                return

        loc = self._page.get_by_text(s, exact=True)
        n = await loc.count()
        if n == 1:
            await loc.click(timeout=5000)
            return
        if n == 0:
            raise ValueError(f"click target not found: {s!r}")
        raise ValueError(
            f"click target ambiguous: {n} elements match {s!r} exactly; "
            "planner must emit a more specific target"
        )

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
        try:
            await self._context.close()
        except Exception:
            pass
        # Only tear down the browser/playwright when this session owns them.
        # Pool-managed sessions leave the shared Browser alive for reuse.
        if self._owns_browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            try:
                await self._pw.stop()
            except Exception:
                pass
