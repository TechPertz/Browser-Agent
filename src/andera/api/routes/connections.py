"""Connections: sealed-storage_state sign-in UX.

Flow:
  /ui/connections/new (GET)                 -> form (host + login URL)
  /ui/connections/new (POST)                -> spawn headed Chromium on THIS
                                               machine, hold in-memory, redirect
                                               to status page
  /ui/connections/session/{id} (GET)        -> "Sign in over there, click below
                                               when done" + live URL preview
  /ui/connections/session/{id}/save (POST)  -> capture storage_state, seal,
                                               close browser, redirect to list
  /ui/connections/session/{id}/cancel (POST)-> close browser, drop session

Session state lives in-memory only — a server restart drops any in-flight
login session. The user just retries. Each session auto-closes after 15 min
so dangling Chromium windows can't accumulate.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from andera.credentials import SealedStateStore, host_of

router = APIRouter()

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


# Max time we keep a login session alive before forcing cleanup.
_SESSION_TTL_SECONDS = 15 * 60


class _LoginSession:
    __slots__ = ("session_id", "host", "login_url", "browser", "context", "page", "pw", "created_at")

    def __init__(
        self, *, session_id: str, host: str, login_url: str,
        browser: Any, context: Any, page: Any, pw: Any,
    ) -> None:
        self.session_id = session_id
        self.host = host
        self.login_url = login_url
        self.browser = browser
        self.context = context
        self.page = page
        self.pw = pw
        self.created_at = time.time()


# In-memory registry: session_id -> _LoginSession. Module-level because the
# handlers need to share it across requests.
_SESSIONS: dict[str, _LoginSession] = {}


async def _close_session(sess: _LoginSession) -> None:
    for action in (sess.browser.close(), sess.pw.stop()):
        try:
            await action
        except Exception:
            pass
    _SESSIONS.pop(sess.session_id, None)


async def _reap_stale() -> None:
    """Best-effort GC: close any session older than the TTL."""
    now = time.time()
    stale = [s for s in _SESSIONS.values() if now - s.created_at > _SESSION_TTL_SECONDS]
    for s in stale:
        await _close_session(s)


@router.get("/api/connections")
async def list_connections() -> dict[str, Any]:
    store = SealedStateStore()
    return {"hosts": store.list_hosts()}


@router.post("/api/connections/{host}/delete")
async def delete_connection(host: str) -> dict[str, Any]:
    store = SealedStateStore()
    removed = store.delete(host)
    return {"host": host, "removed": removed}


@router.get("/ui/connections/new", response_class=HTMLResponse)
async def connection_new_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "connection_new.html", {})


@router.post("/ui/connections/new")
async def connection_new_submit(
    request: Request,
    host: str = Form(...),
    login_url: str = Form(...),
) -> Any:
    """Launch a headed Chromium on the server machine for the user to sign in.

    The browser window appears on the same desktop as the uvicorn process.
    We hand the user back a status page; they hit the 'I'm signed in' button
    once the target site shows them as authenticated.
    """
    # Normalize host — accept either a bare hostname or a URL.
    resolved = host_of(host) or host.strip().lower()
    if not resolved:
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": "host is required (e.g. linkedin.com)"},
            status_code=400,
        )
    url = login_url.strip()
    if not url:
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": "login URL is required"},
            status_code=400,
        )

    await _reap_stale()

    # Spawn headed Playwright. The window pops on the user's desktop in
    # local-dev (uvicorn runs on the same box as the browser they're using).
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        try:
            await pw.stop()
        except Exception:
            pass
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": f"failed to launch browser: {e}",
             "host": host, "login_url": login_url},
            status_code=500,
        )

    session_id = uuid.uuid4().hex[:12]
    sess = _LoginSession(
        session_id=session_id, host=resolved, login_url=url,
        browser=browser, context=context, page=page, pw=pw,
    )
    _SESSIONS[session_id] = sess
    return RedirectResponse(
        f"/ui/connections/session/{session_id}", status_code=303,
    )


@router.get("/ui/connections/session/{session_id}", response_class=HTMLResponse)
async def connection_session_page(
    request: Request, session_id: str,
) -> HTMLResponse:
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": "login session not found (may have timed out)"},
            status_code=404,
        )
    # Surface the current URL so the user can see the login progressing.
    current_url = ""
    try:
        current_url = sess.page.url
    except Exception:
        pass
    return templates.TemplateResponse(
        request, "connection_session.html",
        {
            "session_id": session_id,
            "host": sess.host,
            "login_url": sess.login_url,
            "current_url": current_url,
        },
    )


@router.get("/api/connections/session/{session_id}/url")
async def connection_session_url(session_id: str) -> dict[str, Any]:
    """Poll target: return the current URL of the login browser so the UI
    can show the user they've moved past the login page."""
    sess = _SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    try:
        return {"url": sess.page.url, "host": sess.host}
    except Exception:
        return {"url": "", "host": sess.host}


@router.post("/ui/connections/session/{session_id}/save")
async def connection_session_save(
    request: Request, session_id: str,
) -> Any:
    sess = _SESSIONS.get(session_id)
    if sess is None:
        return RedirectResponse("/ui/connections", status_code=303)
    try:
        state = await sess.context.storage_state()
    except Exception as e:
        await _close_session(sess)
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": f"failed to capture storage_state: {e}"},
            status_code=500,
        )
    try:
        SealedStateStore().save(sess.host, state)
    except Exception as e:
        await _close_session(sess)
        return templates.TemplateResponse(
            request, "connection_new.html",
            {"error": (
                f"failed to seal state: {e} — is ANDERA_MASTER_KEY set in .env?"
            )},
            status_code=500,
        )
    await _close_session(sess)
    return RedirectResponse("/ui/connections", status_code=303)


@router.post("/ui/connections/session/{session_id}/cancel")
async def connection_session_cancel(session_id: str) -> Any:
    sess = _SESSIONS.get(session_id)
    if sess is not None:
        await _close_session(sess)
    return RedirectResponse("/ui/connections", status_code=303)


@router.post("/ui/connections/{host}/delete-form")
async def connection_delete_form(host: str) -> Any:
    """Form-friendly delete (POST from the list page)."""
    SealedStateStore().delete(host)
    return RedirectResponse("/ui/connections", status_code=303)
