"""Playwright executor: attaches to an already-running Chromium-family browser over CDP.

Snapshot = visible elements with stable ids, viewport first. Execution
highlights what it touches so you can see it act in the browser window."""

import json
import re
import subprocess
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9223"

SNAPSHOT_JS = """
() => {
  const seen = new Set();
  const out = [];
  const vw = innerWidth, vh = innerHeight;
  const visible = e => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    if (r.bottom < 0 || r.top > vh) return null;
    const s = getComputedStyle(e);
    return (s.visibility !== 'hidden' && s.display !== 'none') ? r : null;
  };
  const name = e => {
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\\s+/)
      .map(id => document.getElementById(id)?.textContent?.trim()).filter(Boolean).join(' ');
    return labelled || e.getAttribute('aria-label') || e.getAttribute('placeholder')
      || e.getAttribute('title')
      || (['button','submit','reset'].includes(e.type||'') ? e.value : '')
      || (e.innerText || '').trim().split('\\n')[0] || '';
  };
  const sel = 'a[href], button, input, textarea, select, summary, [contenteditable="true"],'
    + ' [role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="option"],'
    + ' [role="textbox"],[role="searchbox"],[role="combobox"]';
  for (const e of document.querySelectorAll(sel)) {
    if (seen.has(e)) continue;
    seen.add(e);
    const r = visible(e);
    if (!r) continue;
    if (e.closest('[aria-hidden="true"],[inert]')) continue;
    if (e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    if (['password','file','hidden'].includes(e.type)) continue;
    let role = e.tagName.toLowerCase();
    if (e.getAttribute('role')) role = e.getAttribute('role');
    if (e.tagName === 'INPUT' && ['text','email','search','url','tel','number'].includes(e.type||'')) role = 'textbox';
    if (e.tagName === 'INPUT' && ['submit','button','reset'].includes(e.type||'')) role = 'button';
    const text = name(e).slice(0, 60);
    if (!text && !['textbox','searchbox','combobox','spinbutton'].includes(role)) continue;
    const id = 'e' + String(out.length + 1).padStart(2, '0');
    let href = '';
    if (e.tagName === 'A' && e.href) {
      try { href = new URL(e.href).hostname.replace(/^www\\./,''); } catch {}
    }
    out.push({ id, role, text, href,
      placeholder: e.getAttribute('placeholder') || '',
      x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
      w: Math.round(r.width), h: Math.round(r.height),
      below_fold: r.top > vh,
      is_input: ['INPUT','TEXTAREA'].includes(e.tagName) || e.isContentEditable,
      tag: e.tagName });
    if (out.length >= 100) break;
  }
  return { elements: out, url: location.href, title: document.title.slice(0,120), vh, vw,
           dpr: devicePixelRatio };
}
"""

HIGHLIGHT_JS = """
(x, y) => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;left:' + (x-5) + 'px;top:' + (y-5) +
    'px;width:10px;height:10px;border-radius:50%;background:#6ee7b7;' +
    'box-shadow:0 0 0 8px rgba(110,231,183,.35);z-index:2147483647;' +
    'pointer-events:none;transition:opacity .4s;';
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 800);
}
"""

OVERLAY_JS = """
(items) => {
  document.querySelectorAll('[data-vb-badge]').forEach(e => e.remove());
  for (const it of items) {
    const d = document.createElement('div');
    d.dataset.vbBadge = '1';
    d.textContent = it.n;
    d.style.cssText = 'position:fixed;left:' + (it.x+8) + 'px;top:' + (it.y-24) +
      'px;background:#111827;color:#f9fafb;font:600 12px system-ui;' +
      'padding:2px 8px;border-radius:8px;z-index:2147483647;pointer-events:none;';
    document.body.appendChild(d);
  }
  setTimeout(() => document.querySelectorAll('[data-vb-badge]').forEach(e => e.remove()), 8000);
}
"""

SITE_BY_HOST = {
    "google.com": "google", "duckduckgo.com": "duckduckgo", "youtube.com": "youtube",
    "wikipedia.org": "wikipedia", "github.com": "github", "amazon.com": "amazon",
    "reddit.com": "reddit", "x.com": "twitter_x", "news.ycombinator.com": "hacker_news",
    "example.com": "example_com",
}


def detect_site(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for domain, site in SITE_BY_HOST.items():
        if host == domain or host.endswith("." + domain):
            return site
    return "generic"


class Executor:
    def __init__(self, cdp_url=CDP_URL):
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self._page = None

    def connect(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        self._select_page()
        if self._page.url == "about:blank":
            self._page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
        return self

    def _all_pages(self):
        return [
            page
            for context in self._browser.contexts
            for page in context.pages
            if not page.is_closed()
        ]

    def _focused_browser_title(self):
        try:
            result = subprocess.run(
                ["hyprctl", "-j", "activewindow"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            window = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
        import browsers

        if result.returncode or not browsers.is_chromium(window.get("class", "")):
            return None
        return browsers.page_title(window.get("title", ""))

    def select_page_for_title(self, window_title):
        """Point at the tab shown in a specific browser window (by its compositor title)."""
        import browsers

        full = (window_title or "").strip()
        title = browsers.page_title(full)
        pages = self._all_pages()
        for wanted in (full, title):   # the title as shown, then without the browser's " - Name" suffix
            for page in pages:
                try:
                    if page.title() == wanted:
                        self._page = page
                        return page
                except Exception:
                    continue
        return self._select_page()

    def _select_page(self):
        """Use the active tab in the focused browser window when possible."""
        pages = self._all_pages()
        if not pages:
            context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self._page = context.new_page()
            return self._page

        focused_title = self._focused_browser_title()
        if focused_title:
            title_matches = []
            for page in pages:
                try:
                    if page.title() == focused_title:
                        title_matches.append(page)
                except Exception:
                    continue
            if len(title_matches) == 1:
                self._page = title_matches[0]
                return self._page

        focused_pages = []
        for page in pages:
            try:
                if page.evaluate("document.hasFocus()"):
                    focused_pages.append(page)
            except Exception:
                continue
        if len(focused_pages) == 1:
            self._page = focused_pages[0]
            return self._page

        if self._page not in pages:
            self._page = next((page for page in pages if page.url != "about:blank"), pages[0])
        return self._page

    @property
    def page_count(self):
        return len(self._all_pages())

    def close(self):
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    # -- snapshot -----------------------------------------------------------
    def snapshot(self, window_title=None) -> dict:
        page = self.select_page_for_title(window_title) if window_title else self._select_page()
        snap = page.evaluate(SNAPSHOT_JS)
        snap["site"] = detect_site(snap.get("url", ""))
        snap["tab_count"] = self.page_count
        sb = next((e["id"] for e in snap["elements"]
                   if e["role"] in ("searchbox", "textbox", "combobox")
                   and re.search(r"search|q\b", (e.get("placeholder") or "") + " " + (e.get("text") or ""), re.I)), None)
        snap["search_box_id"] = sb
        return snap

    # -- actions ------------------------------------------------------------
    def _flash(self, x, y):
        try:
            self._page.evaluate(HIGHLIGHT_JS, x, y)
        except Exception:
            pass

    def _element_by_id(self, target_id):
        snap = self._page.evaluate(SNAPSHOT_JS)
        return next((e for e in snap["elements"] if e["id"] == target_id), None)

    def execute(self, action: dict) -> dict:
        t = action.get("type")
        page = self._page
        try:
            if t == "navigate_url":
                page.goto(action["url"], wait_until="domcontentloaded", timeout=30000)
                return {"ok": True, "outcome": f"navigated to {page.url}"}

            if t == "click_element":
                el = self._element_by_id(action["targetId"])
                if not el:
                    return {"ok": False, "outcome": "element not found"}
                self._flash(el["x"], el["y"])
                page.mouse.click(el["x"], el["y"])
                page.wait_for_timeout(400)
                return {"ok": True, "outcome": "clicked", "label": el["text"]}

            if t == "type_into_field":
                el = self._element_by_id(action["targetId"])
                if not el:
                    return {"ok": False, "outcome": "field not found"}
                page.mouse.click(el["x"], el["y"])
                page.wait_for_timeout(150)
                page.keyboard.press("Control+a")
                page.keyboard.type(action["text"], delay=12)
                return {"ok": True, "outcome": "typed", "label": el["text"]}

            if t == "select_option":
                page.keyboard.press("Escape")
                return {"ok": True, "outcome": "select attempted"}

            if t == "press_enter":
                page.keyboard.press("Enter")
                page.wait_for_timeout(600)
                return {"ok": True, "outcome": "pressed enter"}

            if t == "scroll":
                direction, amount = action.get("direction", "down"), action.get("amount", "page")
                if amount == "little":
                    delta = 220
                elif amount == "end":
                    h = int(page.evaluate("document.body.scrollHeight"))
                    delta = h if direction == "down" else -h
                else:
                    delta = 800
                if direction == "up":
                    delta = -abs(delta)
                page.mouse.wheel(0, delta)
                page.wait_for_timeout(250)
                return {"ok": True, "outcome": f"scrolled {direction} {amount}"}

            if t == "go_back":
                page.go_back(wait_until="domcontentloaded", timeout=20000)
                return {"ok": True, "outcome": f"went back to {page.url}"}
            if t == "go_forward":
                page.go_forward(wait_until="domcontentloaded", timeout=20000)
                return {"ok": True, "outcome": "went forward"}
            if t == "reload":
                page.reload(wait_until="domcontentloaded", timeout=30000)
                return {"ok": True, "outcome": "reloaded"}

            if t == "open_new_tab":
                self._page = self._page.context.new_page()
                return {"ok": True, "outcome": "opened a new tab"}
            if t == "close_tab":
                ctx, url = self._page.context, self._page.url
                if len(ctx.pages) > 1:
                    self._page.close()
                    self._page = ctx.pages[0]
                    return {"ok": True, "outcome": "closed tab"}
                return {"ok": False, "outcome": "only one tab open"}
            if t == "switch_tab":
                pages = self._page.context.pages
                if len(pages) < 2:
                    return {"ok": False, "outcome": "no other tab"}
                idx = pages.index(self._page)
                direction = action.get("direction", "next")
                if direction == "previous":
                    self._page = pages[(idx - 1) % len(pages)]
                elif direction == "first":
                    self._page = pages[0]
                else:
                    self._page = pages[(idx + 1) % len(pages)]
                self._page.bring_to_front()
                return {"ok": True, "outcome": f"switched to {self._page.url}"}

            if t == "clear_field":
                el = self._element_by_id(action.get("targetId") or "")
                if el:
                    page.mouse.click(el["x"], el["y"])
                    page.keyboard.press("Control+a")
                    page.keyboard.press("Delete")
                    return {"ok": True, "outcome": "cleared"}
                return {"ok": False, "outcome": "nothing to clear"}

            return {"ok": False, "outcome": f"unknown action {t}"}
        except Exception as exc:
            return {"ok": False, "outcome": f"failed: {type(exc).__name__}: {exc}"[:200]}

    def overlay_candidates(self, candidates, snapshot):
        items = []
        by_id = {e["id"]: e for e in snapshot["elements"]}
        for i, c in enumerate(candidates, 1):
            el = by_id.get(c["id"])
            if el:
                items.append({"n": str(i), "x": el["x"], "y": el["y"]})
        try:
            self._page.evaluate(OVERLAY_JS, items)
        except Exception:
            pass
        return items