"""Interactive login helper — opens a headed browser so the user can
sign in, then seals Playwright's `storage_state` for future runs.

Usage (via CLI):
    andera login github --url https://github.com/login

The helper waits for the user to finish logging in, detects navigation
to a post-login landing page, and saves the sealed state.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from playwright.async_api import async_playwright

from .storage_state import SealedStateStore


async def interactive_login(
    host: str,
    login_url: str,
    *,
    wait_prompt: Callable[[], None] | None = None,
    store: SealedStateStore | None = None,
) -> str:
    """Launch headed Chromium, wait for user to hit ENTER, save sealed state.

    Returns the path the sealed state was written to.
    """
    store = store or SealedStateStore()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(login_url)

        loop = asyncio.get_running_loop()
        if wait_prompt is None:
            def _prompt() -> None:
                input(
                    f"\n[andera login:{host}] Complete the login in the browser window, "
                    "then press ENTER here to save state... "
                )
            await loop.run_in_executor(None, _prompt)
        else:
            wait_prompt()

        state = await context.storage_state()
        await browser.close()

    path = store.save(host, state)
    return str(path)
