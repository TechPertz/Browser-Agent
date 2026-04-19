"""Grounding tests — real Playwright Page against in-memory HTML."""

import pytest
from playwright.async_api import async_playwright

from andera.browser.grounding import build_snapshot


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        p = await ctx.new_page()
        yield p
        await ctx.close()
        await browser.close()


async def test_snapshot_has_rich_fields(page):
    await page.set_content("""
        <html>
          <head><title>Hello</title></head>
          <body>
            <h1>Big Heading</h1>
            <p>Some body text.</p>
            <button aria-label="Submit form">Go</button>
            <a href="/x">Link</a>
            <input placeholder="email" />
          </body>
        </html>
    """)
    snap = await build_snapshot(page)
    assert snap["title"] == "Hello"
    assert "Big Heading" in snap["inner_text"]
    # outline captures the h1
    outline_labels = [o["label"] for o in snap["outline"]]
    assert any("Big Heading" in l for l in outline_labels)
    # interactive elements captured
    roles = [i["role"] for i in snap["interactive"]]
    assert "button" in roles
    assert "a" in roles
    assert "input" in roles


async def test_snapshot_truncates_long_text(page):
    big = "x" * 20000
    await page.set_content(f"<html><body>{big}</body></html>")
    snap = await build_snapshot(page)
    assert len(snap["inner_text"]) <= 6000
    assert snap["inner_text_truncated"] is True


async def test_interactive_names_extracted(page):
    await page.set_content("""
        <html><body>
          <button>Click me</button>
          <button aria-label="Accessible name">X</button>
        </body></html>
    """)
    snap = await build_snapshot(page)
    names = [i["name"] for i in snap["interactive"]]
    assert any("Click me" in n for n in names)
    assert any("Accessible name" in n for n in names)
