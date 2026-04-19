"""Auth detection helpers.

`host_of(url)` — stable host key used to look up sealed storage_state.
`looks_logged_out(url)` — cheap URL-pattern check called after the first
goto; if the final URL looks like a login wall, the sample fails fast
with a clear "run: andera login <host>" message rather than wasting
LLM calls on a re-auth loop.
"""

from __future__ import annotations

from urllib.parse import urlparse

_LOGIN_PATH_TOKENS = (
    "/login", "/signin", "/sign_in", "/sign-in",
    "/auth", "/sso", "/oauth", "/sessions/new",
)


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        h = urlparse(url).hostname
    except Exception:
        return None
    return h.lower() if h else None


def looks_logged_out(url: str) -> bool:
    """True when the URL looks like a login/auth wall."""
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    path = (p.path or "").lower()
    return any(tok in path for tok in _LOGIN_PATH_TOKENS)
