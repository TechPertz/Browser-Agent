"""Rich page grounding: what the agent sees beyond a raw DOM dump.

Three channels:
  1. Inner text (body only, truncated) — human-readable content.
  2. Accessibility tree (role + name + value) — semantic structure.
  3. Interactive element list with bounding boxes — where to click.

Works on vanilla pages. Shadow DOM / iframe awareness lives in
`set_of_mark.py` where we need bbox-per-element for overlays.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

# Hard cap on inner-text payload. Larger payloads blow up the navigator
# LLM context and rarely help (most useful content is at the top).
INNER_TEXT_LIMIT = 6000


async def build_snapshot(page: Page) -> dict[str, Any]:
    """Return a rich, LLM-friendly view of the current page."""
    title = await page.title()
    url = page.url

    try:
        inner_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        inner_text = ""
    inner_text = inner_text[:INNER_TEXT_LIMIT]

    try:
        outline = await _collect_outline(page)
    except Exception:
        outline = []

    try:
        interactive = await _collect_interactive(page)
    except Exception:
        interactive = []

    # Page-state extras the verifier + planner benefit from knowing.
    try:
        page_state = await _collect_page_state(page)
    except Exception:
        page_state = {}

    return {
        "url": url,
        "title": title,
        "inner_text": inner_text,
        "inner_text_truncated": len(inner_text) >= INNER_TEXT_LIMIT,
        "outline": outline,          # flat headings + landmarks
        "interactive": interactive,  # clickables with role/name/bbox + in_viewport
        "page_state": page_state,    # scroll + active element + modal_open + ready_state
    }


async def _collect_page_state(page: Page) -> dict[str, Any]:
    """Scroll position, focused element, modal detection, ready state."""
    script = """
    () => {
      const active = document.activeElement;
      const activeInfo = active && active !== document.body ? {
        tag: active.tagName.toLowerCase(),
        role: active.getAttribute('role') || '',
        name: (active.getAttribute('aria-label') ||
               active.getAttribute('placeholder') ||
               (active.innerText || '').trim().slice(0, 60)),
      } : null;
      // Heuristic: a visible dialog/modal is anything with role="dialog",
      // role="alertdialog", or a fixed-position element that covers >40%
      // of the viewport.
      const dialogs = Array.from(document.querySelectorAll(
        '[role=dialog], [role=alertdialog], dialog[open], [aria-modal="true"]'
      )).filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      return {
        scroll_y: Math.round(window.scrollY),
        scroll_max_y: Math.round(document.documentElement.scrollHeight -
                                 window.innerHeight),
        viewport: { w: window.innerWidth, h: window.innerHeight },
        ready_state: document.readyState,
        active: activeInfo,
        modal_open: dialogs.length > 0,
        modal_labels: dialogs.slice(0, 3).map((d) =>
          d.getAttribute('aria-label') ||
          (d.querySelector('h1,h2,h3')?.innerText || '').slice(0, 60)
        ),
      };
    }
    """
    return await page.evaluate(script)


async def _collect_outline(page: Page) -> list[dict[str, Any]]:
    """Headings + landmarks, flat list in document order.

    Playwright Python no longer exposes `page.accessibility`, so we build
    the structural outline via JS. Captures what a screen reader would
    emit for navigation — enough for the LLM to understand page shape.
    """
    script = """
    () => {
      const sel = 'h1,h2,h3,h4,h5,h6,nav,main,header,footer,form,article,section,[role=heading],[role=main],[role=navigation]';
      const out = [];
      const walk = (root) => {
        for (const el of root.querySelectorAll(sel)) {
          const tag = el.tagName.toLowerCase();
          out.push({
            tag,
            level: tag.match(/^h\\d$/) ? Number(tag[1]) : 0,
            role: el.getAttribute('role') || tag,
            label: el.getAttribute('aria-label') || (el.innerText || '').trim().slice(0, 80),
          });
          if (out.length >= 60) return;
        }
        for (const el of root.querySelectorAll('*')) {
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      };
      walk(document);
      return out;
    }
    """
    return await page.evaluate(script)


async def _collect_interactive(page: Page) -> list[dict[str, Any]]:
    """List visible clickable elements with their bounding boxes.

    In-browser JS is the fastest way to walk the DOM; we run a small
    script that returns a compact list. Shadow roots and same-origin
    iframes are walked; cross-origin iframes are skipped (unreachable).
    """
    script = """
    () => {
      const selectors = 'a,button,input,select,textarea,[role=button],[role=link],[role=menuitem],[role=tab],[onclick]';
      const results = [];
      const seen = new Set();

      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
        return true;
      };
      const inViewport = (r) => {
        return r.bottom >= 0 && r.top <= window.innerHeight
          && r.right >= 0 && r.left <= window.innerWidth;
      };

      const accessibleName = (el) => {
        return (
          el.getAttribute('aria-label') ||
          el.getAttribute('alt') ||
          el.getAttribute('title') ||
          (el.innerText || '').trim().slice(0, 80) ||
          el.getAttribute('placeholder') ||
          el.value ||
          ''
        );
      };

      const roleOf = (el) => {
        const r = el.getAttribute('role');
        if (r) return r;
        return el.tagName.toLowerCase();
      };

      const walk = (root) => {
        const nodes = root.querySelectorAll(selectors);
        for (const el of nodes) {
          if (seen.has(el)) continue;
          seen.add(el);
          if (!isVisible(el)) continue;
          const r = el.getBoundingClientRect();
          results.push({
            role: roleOf(el),
            name: accessibleName(el),
            bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
            in_viewport: inViewport(r),
          });
        }
        // walk shadow roots
        const all = root.querySelectorAll('*');
        for (const el of all) {
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      };

      walk(document);

      // walk same-origin iframes (best-effort; cross-origin throws SecurityError)
      for (const iframe of document.querySelectorAll('iframe')) {
        try {
          const doc = iframe.contentDocument;
          if (doc) walk(doc);
        } catch (_) { /* cross-origin, skip */ }
      }

      return results.slice(0, 120);  // cap to keep payload bounded
    }
    """
    return await page.evaluate(script)
