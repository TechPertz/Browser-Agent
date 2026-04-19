"""Lightweight stealth — patches out the most obvious bot signals.

Not a replacement for `playwright-stealth` if it's available, but
keeps us dependency-free and correct for the biggest tells:

  - navigator.webdriver = false
  - navigator.plugins length > 0
  - navigator.languages populated
  - window.chrome defined
  - permissions.query returns 'granted' for notifications

If `playwright-stealth` is installed, we use it instead — it patches
more surfaces. Either way callers get the same `apply_stealth(context)`
entry point.
"""

from __future__ import annotations

import random
from typing import Any

_USER_AGENTS = [
    # Rotating fleet of recent desktop Chrome UAs on macOS/Windows.
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 960},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
]

_STEALTH_JS = r"""
// Hide webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => false});

// Pretend plugins exist
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5]
});

// Languages
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en']
});

// window.chrome
window.chrome = window.chrome || { runtime: {}, loadTimes: function () {} };

// Notifications permission
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters);
}

// WebGL vendor spoof
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, p);
};
"""


def random_user_agent(seed: int | None = None) -> str:
    rng = random.Random(seed) if seed is not None else random
    return rng.choice(_USER_AGENTS)


def random_viewport(seed: int | None = None) -> dict[str, int]:
    rng = random.Random(seed) if seed is not None else random
    return dict(rng.choice(_VIEWPORTS))


async def apply_stealth(context: Any) -> None:
    """Inject the stealth init script into a Playwright browser context.

    Try `playwright-stealth` first if installed; fall back to our own
    inline script. No network, no side effects besides context state.
    """
    try:
        from playwright_stealth import stealth_async  # type: ignore[import-not-found]
        # playwright-stealth attaches per-page; apply to all pages.
        pages = getattr(context, "pages", None) or []
        for p in pages:
            await stealth_async(p)
        # Install a page-creation hook for future pages.
        context.on("page", lambda p: stealth_async(p))
        return
    except ImportError:
        pass
    await context.add_init_script(_STEALTH_JS)
