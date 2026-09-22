"""Installed applications (XDG desktop entries) and spoken-name matching.

The model never names an app on its own: code proposes candidates whose names
overlap the transcript, and the model only picks one of them.
"""

from __future__ import annotations

import configparser
import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

DIRS = [
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]
STOP = {"open", "launch", "start", "run", "the", "a", "an", "app", "application", "please", "up", "bring", "show", "me", "switch", "to", "go", "my"}


@dataclass(frozen=True)
class App:
    id: str          # desktop file id, e.g. "spotify.desktop"
    name: str
    generic: str
    keywords: str
    wm_class: str

    @property
    def words(self) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", f"{self.name} {self.generic} {self.keywords}".lower()))


_cache: list[App] | None = None


def installed() -> list[App]:
    global _cache
    if _cache is not None:
        return _cache
    found: dict[str, App] = {}
    for base in DIRS:
        if not base.is_dir():
            continue
        for path in base.rglob("*.desktop"):
            desktop_id = str(path.relative_to(base)).replace("/", "-")
            if desktop_id in found:
                continue  # user entries shadow system ones
            parser = configparser.RawConfigParser(strict=False, interpolation=None)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except Exception:
                continue
            if entry.get("Type", "Application") != "Application":
                continue
            if entry.get("NoDisplay", "false").lower() == "true" or entry.get("Hidden", "false").lower() == "true":
                found[desktop_id] = None  # type: ignore[assignment] — still shadows
                continue
            found[desktop_id] = App(
                id=desktop_id, name=entry.get("Name", desktop_id.removesuffix(".desktop")),
                generic=entry.get("GenericName", ""), keywords=entry.get("Keywords", "").replace(";", " "),
                wm_class=entry.get("StartupWMClass", ""),
            )
    _cache = sorted((a for a in found.values() if a), key=lambda a: a.name.lower())
    return _cache


def candidates(transcript: str, limit: int = 8) -> list[App]:
    words = [w for w in re.findall(r"[a-z0-9]+", transcript.lower()) if w not in STOP]
    if not words:
        return []
    phrase = " ".join(words)
    scored = []
    for app in installed():
        name = app.name.lower()
        score = difflib.SequenceMatcher(None, phrase, name).ratio()
        if name in phrase:
            score += 1.0
        overlap = len(set(words) & app.words)
        score += 0.35 * overlap
        for w in words:
            if len(w) > 2 and any(difflib.SequenceMatcher(None, w, n).ratio() > 0.84 for n in name.split()):
                score += 0.5
        if score > 0.55:
            scored.append((score, app))
    scored.sort(key=lambda s: -s[0])
    return [a for _, a in scored[:limit]]


def by_id(desktop_id: str) -> App | None:
    return next((a for a in installed() if a.id == desktop_id), None)


def launch_argv(app: App) -> list[str]:
    return ["uwsm-app", "--", app.id]


def window_matches(app: App, window_class: str, window_title: str) -> bool:
    cls = window_class.lower()
    names = {app.wm_class.lower(), app.id.removesuffix(".desktop").lower(), app.name.lower()}
    names.discard("")
    first_word = app.name.lower().split()[0] if app.name else ""
    return cls in names or cls == first_word or any(n and n in cls for n in names if len(n) > 3)


def kind_for_class(window_class: str) -> str:
    """What sort of app a window is ("Web Browser", "Terminal", "Mail Client"), from its desktop entry."""
    cls = window_class.lower()
    for app in installed():
        if app.generic and (app.wm_class.lower() == cls or app.id.removesuffix(".desktop").lower() == cls
                            or app.id.removesuffix(".desktop").lower().endswith("." + cls)):
            return app.generic
    return ""
