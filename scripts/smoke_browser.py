"""Phase 1 acceptance test.

Drives real Chromium to https://example.com, screenshots it, writes the
content-addressed artifact, and prints the sha256 + resolved path.

Run: `uv run python scripts/smoke_browser.py`
Exit code 0 on success.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from andera.browser import BrowserPool
from andera.storage import FilesystemArtifactStore


async def main() -> int:
    root = Path("runs/smoke")
    store = FilesystemArtifactStore(root)
    pool = BrowserPool(
        artifacts=store,
        concurrency=1,
        headless=True,
        viewport={"width": 1280, "height": 800},
    )
    async with pool.acquire(sample_id="smoke-1", run_id="smoke") as session:
        await session.goto("https://example.com")
        snap = await session.snapshot()
        print(f"title: {snap['title']!r}")
        print(f"url:   {snap['url']}")
        artifact = await session.screenshot("step_00_home.png")

    p = Path(artifact.path)
    assert p.exists(), f"artifact file missing: {p}"
    print(f"sha256: {artifact.sha256}")
    print(f"size:   {artifact.size} bytes")
    print(f"path:   {p}")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
