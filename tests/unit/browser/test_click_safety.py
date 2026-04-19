"""Click-fallback safety: the OLD behavior would substring-match + click
the first element. That's how agents accidentally click 'Submit feedback'
in the footer when the planner said 'Submit'. These tests pin the new
behavior: exact matches only, ambiguity raises, 'not found' raises."""

import pytest
from playwright.async_api import async_playwright

from andera.browser import BrowserPool
from andera.storage import FilesystemArtifactStore


@pytest.fixture
async def session(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        # Build a session wrapper around the live page. We hand-wire
        # the fields LocalPlaywrightSession expects so we skip its
        # own create() flow (which launches a separate browser).
        from andera.browser.local import LocalPlaywrightSession
        s = LocalPlaywrightSession(
            artifacts=store, browser=browser, context=ctx, page=page,
            playwright_ctx=pw,
        )
        yield s
        await ctx.close()
        await browser.close()


async def test_exact_role_match_clicks(session):
    await session._page.set_content("""
        <html><body>
          <button id="form-submit">Submit</button>
          <a href="/feedback">Submit feedback</a>
        </body></html>
    """)
    await session.click("Submit")
    # The role=button matched first; verify the BUTTON received click
    # via a sentinel DOM mutation hook.
    # Easiest: re-run with a known side-effect.


async def test_ambiguous_text_raises(session):
    """Old behavior would click .first — that's the accuracy bug."""
    await session._page.set_content("""
        <html><body>
          <a href="/a">Go</a>
          <a href="/b">Go</a>
          <a href="/c">Go</a>
        </body></html>
    """)
    with pytest.raises(ValueError, match="ambiguous"):
        await session.click("Go")


async def test_missing_text_raises(session):
    await session._page.set_content("<html><body><p>nothing clickable</p></body></html>")
    with pytest.raises(ValueError, match="not found"):
        await session.click("Submit")


async def test_exact_beats_substring(session):
    """'Submit' must click the 'Submit' button, not 'Submit feedback' link."""
    await session._page.set_content("""
        <html><body>
          <button type="button" id="primary">Submit</button>
          <a href="/fb" id="secondary">Submit feedback</a>
        </body></html>
    """)
    # Install a click counter on each element via page.evaluate
    await session._page.evaluate("""
        document.getElementById('primary').addEventListener('click', () => window._hitPrimary = true);
        document.getElementById('secondary').addEventListener('click', () => window._hitSecondary = true);
    """)
    await session.click("Submit")
    hit_primary = await session._page.evaluate("window._hitPrimary === true")
    hit_secondary = await session._page.evaluate("window._hitSecondary === true")
    assert hit_primary is True
    assert hit_secondary is False


async def test_selector_used_strictly(session):
    """If input looks like a selector, don't degrade to text match on failure."""
    await session._page.set_content("<html><body><p>no matching selector</p></body></html>")
    with pytest.raises(Exception):
        await session.click("#nonexistent")
