"""Bounded browser session pool.

Two-layer lifecycle:
  - ONE Playwright + ONE Chromium browser process launched ONCE per
    pool in `setup()`. Persistent across all samples.
  - One BrowserContext + Page opened PER sample in `acquire()`, which
    is cheap (~5ms). Full per-sample isolation (cookies, storage,
    network are all context-scoped in Chromium).

Old behavior: launched a fresh Chromium per sample. That's 300-800ms
of startup per sample × N samples — the single biggest wasted
wall-clock time in a run. New behavior is a ~100× reduction per
sample at no isolation cost.

`browser.concurrency` still caps in-flight contexts via a semaphore.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from playwright.async_api import Browser, async_playwright

from andera.contracts import ArtifactStore, BrowserSession

from .local import LocalPlaywrightSession


class BrowserPool:
    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        concurrency: int,
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        stealth: bool = False,
        rate_limiter: Any = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._artifacts = artifacts
        self._headless = headless
        self._viewport = viewport
        self._stealth = stealth
        self._rate_limiter = rate_limiter
        self._sem = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency

        # Persistent process-level resources
        self._pw: Any = None
        self._browser: Browser | None = None
        self._setup_lock = asyncio.Lock()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    async def setup(self) -> None:
        """Launch Playwright + Chromium ONCE. Idempotent."""
        async with self._setup_lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)

    async def teardown(self) -> None:
        """Close the persistent browser. Safe to call multiple times."""
        async with self._setup_lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    @asynccontextmanager
    async def acquire(
        self,
        *,
        sample_id: str | None = None,
        run_id: str | None = None,
        storage_state: str | dict[str, Any] | None = None,
    ):
        """Yield a BrowserSession bounded by the concurrency semaphore.

        Lazy-starts the persistent browser on first acquire so callers
        that never use the pool don't pay the launch cost.
        """
        await self._sem.acquire()
        session: BrowserSession | None = None
        try:
            if self._browser is None:
                await self.setup()
            assert self._browser is not None
            session = await LocalPlaywrightSession.from_browser(
                browser=self._browser,
                playwright_ctx=self._pw,
                artifacts=self._artifacts,
                viewport=self._viewport,
                sample_id=sample_id,
                run_id=run_id,
                storage_state=storage_state,
                stealth=self._stealth,
                rate_limiter=self._rate_limiter,
            )
            yield session
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:  # noqa: BLE001 — best-effort close
                    pass
            self._sem.release()
