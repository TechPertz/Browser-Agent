"""Set-of-Mark tests including shadow DOM walk."""

import pytest
from playwright.async_api import async_playwright

from andera.browser.set_of_mark import mark_and_screenshot, mark_page


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1024, "height": 768})
        p = await ctx.new_page()
        yield p
        await ctx.close()
        await browser.close()


async def test_marks_basic_interactives(page):
    await page.set_content("""
        <html><body>
          <button>A</button>
          <button>B</button>
          <a href="/x">Link</a>
          <input placeholder="q" />
        </body></html>
    """)
    marks = await mark_page(page)
    assert len(marks) == 4
    roles = sorted(m.role for m in marks.values())
    assert roles == ["a", "button", "button", "input"]


async def test_marks_walk_shadow_dom(page):
    await page.set_content("""
        <html><body>
          <div id="host"></div>
          <script>
            const host = document.getElementById('host');
            const root = host.attachShadow({ mode: 'open' });
            root.innerHTML = '<button>Inside Shadow</button>';
          </script>
        </body></html>
    """)
    marks = await mark_page(page)
    in_shadow = [m for m in marks.values() if m.in_shadow]
    assert len(in_shadow) == 1
    assert in_shadow[0].role == "button"


async def test_mark_and_screenshot_returns_png_bytes(page):
    await page.set_content("<html><body><button>Go</button></body></html>")
    png, marks = await mark_and_screenshot(page)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(marks) == 1


async def test_mark_cap_enforced(page):
    html = "<html><body>" + "".join(f"<button>B{i}</button>" for i in range(200)) + "</body></html>"
    await page.set_content(html)
    marks = await mark_page(page)
    assert len(marks) <= 80
