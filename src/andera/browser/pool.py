"""Bounded browser session pool.

Enforces the `browser.concurrency` cap from profile.yaml so we never
launch more than N chromium contexts at once, no matter how many
samples the orchestrator fans out. Back-pressure falls on the
semaphore, not the OS.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

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
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._artifacts = artifacts
        self._headless = headless
        self._viewport = viewport
        self._sem = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @asynccontextmanager
    async def acquire(
        self,
        *,
        sample_id: str | None = None,
        run_id: str | None = None,
        storage_state: str | dict[str, Any] | None = None,
    ):
        """Yield a BrowserSession bounded by the concurrency semaphore."""
        await self._sem.acquire()
        session: BrowserSession | None = None
        try:
            session = await LocalPlaywrightSession.create(
                artifacts=self._artifacts,
                headless=self._headless,
                viewport=self._viewport,
                sample_id=sample_id,
                run_id=run_id,
                storage_state=storage_state,
            )
            yield session
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:  # noqa: BLE001 — best-effort close
                    pass
            self._sem.release()
