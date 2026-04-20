"""Coverage for the Serper-backed search tool."""
from unittest.mock import patch

import httpx
import pytest

from andera.tools.browser import BrowserTools, SearchArgs


class _NoopSession:
    async def goto(self, url): ...
    async def click(self, s): ...
    async def type(self, s, v): ...
    async def screenshot(self, n, *, full_page=True, folder=None): ...
    async def scroll(self, amount): ...
    async def scroll_to(self, target): ...
    async def screenshot_chunks(self, n, *, folder=None): ...
    async def visit_each_link(self, **kw): ...
    async def extract(self, s): ...
    async def snapshot(self): return {"url": "x", "title": "t"}
    async def close(self): ...


def _mock_serper_response(organic):
    # httpx.Response.raise_for_status() needs a request set.
    req = httpx.Request("POST", "https://google.serper.dev/search")
    return httpx.Response(200, json={"organic": organic}, request=req)


async def test_search_requires_api_key(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    tools = BrowserTools(_NoopSession())
    out = await tools.search(SearchArgs(query="test"))
    assert out.status == "error"
    assert "SERPER_API_KEY" in (out.error or "")


async def test_search_returns_organic_results(monkeypatch):
    """Happy path: Serper returns N hits, tool projects to {title, url, snippet}."""
    monkeypatch.setenv("SERPER_API_KEY", "fake-key")
    sent_payloads = []

    async def fake_post(self, url, headers=None, json=None, **_):
        sent_payloads.append({"url": url, "headers": headers, "json": json})
        return _mock_serper_response([
            {"title": "Zeyap - LinkedIn", "link": "https://linkedin.com/in/zeyap", "snippet": "Engineer at Meta"},
            {"title": "Zeyap GitHub", "link": "https://github.com/zeyap", "snippet": "..."},
        ])

    with patch.object(httpx.AsyncClient, "post", fake_post):
        tools = BrowserTools(_NoopSession())
        out = await tools.search(SearchArgs(query="zeyap site:linkedin.com", limit=2))

    assert out.status == "ok"
    assert out.data["count"] == 2
    assert out.data["results"][0]["url"] == "https://linkedin.com/in/zeyap"
    assert out.data["results"][0]["snippet"] == "Engineer at Meta"
    # Confirm we actually hit Serper with the right auth + payload.
    assert sent_payloads[0]["url"] == "https://google.serper.dev/search"
    assert sent_payloads[0]["headers"]["X-API-KEY"] == "fake-key"
    assert sent_payloads[0]["json"] == {"q": "zeyap site:linkedin.com", "num": 2}


async def test_search_empty_organic(monkeypatch):
    """No results -> count=0 + empty list, still status=ok."""
    monkeypatch.setenv("SERPER_API_KEY", "fake-key")

    async def fake_post(self, *_, **__):
        return _mock_serper_response([])

    with patch.object(httpx.AsyncClient, "post", fake_post):
        out = await BrowserTools(_NoopSession()).search(SearchArgs(query="zzz"))

    assert out.status == "ok"
    assert out.data["count"] == 0
    assert out.data["results"] == []
