import pytest
from playwright.async_api import async_playwright

from andera.browser.stealth import (
    apply_stealth,
    random_user_agent,
    random_viewport,
)


def test_random_user_agent_is_chrome():
    for _ in range(5):
        ua = random_user_agent()
        assert "Chrome/" in ua
        assert "Mozilla/5.0" in ua


def test_random_viewport_sensible():
    v = random_viewport()
    assert v["width"] >= 1280
    assert v["height"] >= 720


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        await apply_stealth(ctx)
        p = await ctx.new_page()
        yield p
        await ctx.close()
        await b.close()


async def test_webdriver_hidden(page):
    await page.goto("about:blank")
    val = await page.evaluate("navigator.webdriver")
    assert val is False


async def test_plugins_non_empty(page):
    await page.goto("about:blank")
    n = await page.evaluate("navigator.plugins.length")
    assert n and n > 0


async def test_languages_populated(page):
    await page.goto("about:blank")
    langs = await page.evaluate("navigator.languages")
    assert isinstance(langs, list)
    assert len(langs) >= 1
