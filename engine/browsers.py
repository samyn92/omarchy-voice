"""Which windows are web browsers, and how to reach them — for any user's browser.

Every browser gets keyboard-level control (address bar, history, tabs).  A
Chromium-family browser started with remote debugging additionally gets exact
page reading and clicking over CDP; its endpoint is discovered, not assumed.
"""

from __future__ import annotations

import json
import re
import urllib.request

# Hyprland window classes (lower case) of browsers with an address bar
CHROMIUM = {"helium", "chromium", "google-chrome", "google-chrome-stable", "google-chrome-beta", "brave-browser",
            "vivaldi", "vivaldi-stable", "microsoft-edge", "microsoft-edge-stable", "opera", "thorium", "ungoogled-chromium",
            "cromite", "supermium", "yandex-browser"}
FIREFOX = {"firefox", "firefox-esr", "librewolf", "zen", "zen-browser", "floorp", "waterfox", "mullvad-browser", "tor browser"}

# where Chromium's DevTools endpoint usually listens (--remote-debugging-port)
DEFAULT_CDP = ("http://127.0.0.1:9222", "http://127.0.0.1:9223")


def is_browser(window_class: str) -> bool:
    return is_chromium(window_class) or window_class.lower() in FIREFOX


def is_chromium(window_class: str) -> bool:
    c = window_class.lower()
    # "chrome-<site>-Default" windows are installed web apps: no address bar, not a browser window
    return c in CHROMIUM or (c.startswith(("brave-", "vivaldi-", "microsoft-edge-")) and "-default" not in c)


def page_title(window_title: str) -> str:
    """The page title inside a browser window title ("Inbox - Gmail - Google Chrome" -> "Inbox - Gmail")."""
    return re.sub(r"\s[-–—]\s[^-–—]+$", "", window_title or "").strip()


def tab_urls(endpoint: str, timeout: float = 0.5) -> dict[str, str]:
    """{target id: current URL} of every tab, straight from the DevTools HTTP endpoint (never stale)."""
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/list", timeout=timeout) as r:
            return {t["id"]: t.get("url", "") for t in json.loads(r.read()) if t.get("type") == "page"}
    except (OSError, ValueError):
        return {}


def cdp_endpoint(candidates=DEFAULT_CDP, timeout: float = 0.3) -> str | None:
    """The first DevTools endpoint that answers, or None when no browser allows remote debugging."""
    for url in ([candidates] if isinstance(candidates, str) else candidates):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as r:
                if "webSocketDebuggerUrl" in json.loads(r.read()):
                    return url
        except (OSError, ValueError):
            continue
    return None
