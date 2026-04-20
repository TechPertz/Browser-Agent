"""Set-of-Mark overlay: numbered boxes on visible interactive elements.

The navigator LLM sees the annotated screenshot, says "click mark 7",
and we resolve to a coordinate click at the mark's bbox center. This
is the primary unlock for unseen UIs where selectors are brittle.

Walks DOM + shadow roots + same-origin iframes. Cross-origin iframes
are flagged `unreachable` in the returned map so the agent can fall
back to visible-text heuristics if needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from playwright.async_api import Page


@dataclass
class Mark:
    mark_id: int
    role: str
    name: str
    x: int
    y: int
    w: int
    h: int
    # Structural metadata — stable across rows of the same task (e.g.
    # `/pull/\d+` href pattern is identical on every GitHub repo). These
    # feed the descriptor matcher so cached plans replay cross-row
    # without calling vision again.
    href: str = ""
    placeholder: str = ""
    tag: str = ""
    viewport_region: str = ""   # top-left | top-center | ... | header | main | footer
    in_shadow: bool = False
    in_iframe: bool = False


# JS that (a) finds interactive elements across shadow DOM + same-origin
# iframes, (b) assigns each a numeric mark, (c) overlays a colored
# numbered box. Returns the marks list so Python can resolve click
# coordinates without re-querying the DOM.
_OVERLAY_JS = r"""
() => {
  const selectors = 'a,button,input,select,textarea,[role=button],[role=link],[role=menuitem],[role=tab],[onclick]';
  const palette = ['#e11d48', '#2563eb', '#059669', '#d97706', '#7c3aed', '#0891b2', '#be123c', '#15803d'];
  const marks = [];
  const seen = new Set();
  let counter = 0;

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
    return true;
  };

  const accName = (el) => (
    el.getAttribute('aria-label') ||
    el.getAttribute('alt') ||
    el.getAttribute('title') ||
    (el.innerText || '').trim().slice(0, 60) ||
    el.getAttribute('placeholder') ||
    el.value || ''
  );

  const roleOf = (el) => el.getAttribute('role') || el.tagName.toLowerCase();

  // An overlay container that sits on top of the page.
  let layer = document.getElementById('__andera_som__');
  if (layer) layer.remove();
  layer = document.createElement('div');
  layer.id = '__andera_som__';
  Object.assign(layer.style, {
    position: 'fixed', inset: '0', pointerEvents: 'none', zIndex: '2147483647',
  });
  document.body.appendChild(layer);

  const drawMark = (rect, id, color) => {
    const box = document.createElement('div');
    Object.assign(box.style, {
      position: 'absolute',
      left: rect.x + 'px',
      top: rect.y + 'px',
      width: rect.w + 'px',
      height: rect.h + 'px',
      border: `2px solid ${color}`,
      boxSizing: 'border-box',
      background: 'transparent',
    });
    const tag = document.createElement('div');
    tag.textContent = id;
    Object.assign(tag.style, {
      position: 'absolute',
      top: '-1px', left: '-1px',
      background: color, color: 'white',
      font: 'bold 11px sans-serif',
      padding: '1px 4px',
      lineHeight: '1',
    });
    box.appendChild(tag);
    layer.appendChild(box);
  };

  // Landmark ancestors give stable region hints when CSS grid layout
  // fails (unlabeled icon-only buttons in the top-right corner are
  // common — "header button in top-right" generalizes across sites).
  const landmarkOf = (el) => {
    let node = el;
    while (node && node.tagName) {
      const t = node.tagName.toLowerCase();
      if (t === 'header' || t === 'footer' || t === 'nav' || t === 'main' || t === 'aside') {
        return t;
      }
      if (node.getAttribute) {
        const role = node.getAttribute('role');
        if (role === 'banner') return 'header';
        if (role === 'contentinfo') return 'footer';
        if (role === 'navigation') return 'nav';
        if (role === 'main') return 'main';
      }
      node = node.parentElement || (node.getRootNode && node.getRootNode().host);
    }
    return '';
  };

  const regionOf = (r) => {
    // 3x3 viewport grid. Midpoint of the element decides the cell.
    const vw = window.innerWidth || 1280;
    const vh = window.innerHeight || 720;
    const mx = r.x + r.width / 2;
    const my = r.y + r.height / 2;
    const col = mx < vw / 3 ? 'left' : (mx < 2 * vw / 3 ? 'center' : 'right');
    const row = my < vh / 3 ? 'top' : (my < 2 * vh / 3 ? 'middle' : 'bottom');
    return `${row}-${col}`;
  };

  const walk = (root, offsetX = 0, offsetY = 0, inShadow = false, inIframe = false) => {
    const nodes = root.querySelectorAll(selectors);
    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      if (!isVisible(el)) continue;
      const r = el.getBoundingClientRect();
      const landmark = landmarkOf(el);
      const region = landmark || regionOf(r);
      const mark = {
        mark_id: counter,
        role: roleOf(el),
        name: accName(el),
        x: Math.round(r.x + offsetX),
        y: Math.round(r.y + offsetY),
        w: Math.round(r.width),
        h: Math.round(r.height),
        href: el.getAttribute('href') || '',
        placeholder: el.getAttribute('placeholder') || '',
        tag: el.tagName ? el.tagName.toLowerCase() : '',
        viewport_region: region,
        in_shadow: inShadow,
        in_iframe: inIframe,
      };
      marks.push(mark);
      drawMark(mark, counter, palette[counter % palette.length]);
      counter++;
      if (counter >= 80) return;  // overlay saturation cap
    }
    // shadow DOM
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, offsetX, offsetY, true, inIframe);
    }
  };

  walk(document);

  // same-origin iframes (cross-origin throws)
  for (const iframe of document.querySelectorAll('iframe')) {
    try {
      const doc = iframe.contentDocument;
      if (!doc) continue;
      const ir = iframe.getBoundingClientRect();
      walk(doc, ir.x, ir.y, false, true);
    } catch (_) { /* cross-origin, skip */ }
  }

  return marks;
}
"""


_CLEAR_OVERLAY_JS = r"""
() => {
  const el = document.getElementById('__andera_som__');
  if (el) el.remove();
}
"""


async def mark_page(page: Page) -> dict[int, Mark]:
    """Inject the overlay and return a map of mark_id -> Mark."""
    raw = await page.evaluate(_OVERLAY_JS)
    marks: dict[int, Mark] = {}
    for r in raw:
        m = Mark(
            mark_id=r["mark_id"],
            role=r.get("role", ""),
            name=r.get("name", ""),
            x=int(r["x"]),
            y=int(r["y"]),
            w=int(r["w"]),
            h=int(r["h"]),
            href=r.get("href", "") or "",
            placeholder=r.get("placeholder", "") or "",
            tag=r.get("tag", "") or "",
            viewport_region=r.get("viewport_region", "") or "",
            in_shadow=bool(r.get("in_shadow")),
            in_iframe=bool(r.get("in_iframe")),
        )
        marks[m.mark_id] = m
    return marks


async def clear_marks(page: Page) -> None:
    await page.evaluate(_CLEAR_OVERLAY_JS)


async def mark_and_screenshot(page: Page) -> tuple[bytes, dict[int, Mark]]:
    """Overlay marks, screenshot, return (png_bytes, marks). Overlay stays."""
    marks = await mark_page(page)
    png = await page.screenshot(full_page=False)
    return png, marks


def marks_to_list(marks: dict[int, Mark]) -> list[dict[str, Any]]:
    return [asdict(m) for m in sorted(marks.values(), key=lambda m: m.mark_id)]
