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
    outline_labels = [o["label"] for o in snap["outline"]]
    assert any("Big Heading" in l for l in outline_labels)
    roles = [i["role"] for i in snap["interactive"]]
    assert "button" in roles
    assert "a" in roles
    assert "input" in roles
    # Each interactive now carries in_viewport
    assert all("in_viewport" in i for i in snap["interactive"])
    # page_state present
    assert snap["page_state"]["ready_state"] in ("complete", "interactive", "loading")
    assert snap["page_state"]["modal_open"] is False


async def test_snapshot_detects_open_modal(page):
    await page.set_content("""
        <html><body>
          <div role="dialog" aria-modal="true" aria-label="Sign in">
            <h2>Please sign in</h2>
            <button>OK</button>
          </div>
        </body></html>
    """)
    snap = await build_snapshot(page)
    assert snap["page_state"]["modal_open"] is True
    assert any("Sign in" in l for l in snap["page_state"]["modal_labels"])


async def test_snapshot_captures_scroll_position(page):
    await page.set_content(
        "<html><body style='margin:0'>"
        + "<div style='height:3000px'>tall</div>"
        + "</body></html>"
    )
    await page.evaluate("window.scrollTo(0, 800)")
    snap = await build_snapshot(page)
    assert snap["page_state"]["scroll_y"] >= 500


async def test_snapshot_marks_offscreen_interactives(page):
    await page.set_content(
        "<html><body style='margin:0'>"
        + "<button>Top</button>"
        + "<div style='height:2000px'></div>"
        + "<button>Bottom</button>"
        + "</body></html>"
    )
    snap = await build_snapshot(page)
    buttons = [i for i in snap["interactive"] if i["role"] == "button"]
    assert len(buttons) == 2
    by_name = {b["name"]: b for b in buttons}
    assert by_name["Top"]["in_viewport"] is True
    assert by_name["Bottom"]["in_viewport"] is False


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
