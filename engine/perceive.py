"""What is on screen, as numbered elements a decision model can pick from.

Three sources, best first, chosen per window:
  web   Chromium-family browser pages over CDP (exact DOM elements)
  a11y  AT-SPI for native apps (GTK, Qt, Thunderbird/Betterbird, LibreOffice…)
  ocr   grim + tesseract around the mouse, for everything else (terminals,
        Electron apps without accessibility, games, canvases)

All coordinates are Hyprland logical screen pixels, so any element can be
clicked by warping the cursor to it — whatever its source.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import browsers
import desktop

HERE = Path(__file__).resolve().parent
MAX_ELEMENTS = 250
NEAR_PX = 90


@dataclass
class Element:
    id: str
    role: str
    text: str
    x: int            # center, logical screen px
    y: int
    w: int = 0
    h: int = 0
    source: str = "ocr"
    editable: bool = False
    web_id: str = ""   # executor element id for web elements
    extra: str = ""

    def distance(self, px: int, py: int) -> float:
        dx = max(abs(px - self.x) - self.w / 2, 0)
        dy = max(abs(py - self.y) - self.h / 2, 0)
        return (dx * dx + dy * dy) ** 0.5


@dataclass
class Scene:
    desk: desktop.Desktop
    target: desktop.Window | None          # the window the user is most likely talking about
    elements: list[Element] = field(default_factory=list)
    source: str = ""
    web: dict | None = None                 # executor snapshot when target is a browser page
    region: tuple[int, int, int, int] | None = None
    timings: dict = field(default_factory=dict)

    @property
    def by_id(self) -> dict[str, Element]:
        return {e.id: e for e in self.elements}


# -- AT-SPI -------------------------------------------------------------------

class A11y:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["/usr/bin/python3", str(HERE / "a11y_helper.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        return self._proc

    def elements(self, win: desktop.Window, limit: int = 150) -> list[dict]:
        with self._lock:
            proc = self._ensure()
            try:
                proc.stdin.write(json.dumps({"pid": win.pid, "title": win.title,
                                             "window": [win.x, win.y, win.w, win.h], "limit": limit}) + "\n")
                proc.stdin.flush()
                reply = json.loads(proc.stdout.readline() or "{}")
            except (OSError, ValueError):
                self._proc = None
                return []
        return reply.get("elements", []) if reply.get("ok") else []


# -- OCR ----------------------------------------------------------------------

TESSDATA = "/usr/share/tessdata/"
OCR_STRIPS = 12
OCR_OVERLAP = 30   # px (screenshot scale) shared by neighbouring strips so no line is cut


class Ocr:
    """A pool of loaded Tesseract engines reading horizontal strips in parallel.

    One engine on a 1100x700 region takes ~1 s; six engines on overlapping strips
    take ~0.3 s (tesserocr releases the GIL, and each engine is already loaded).
    """

    def __init__(self, strips: int = OCR_STRIPS) -> None:
        import tesserocr
        from concurrent.futures import ThreadPoolExecutor

        self.apis = [tesserocr.PyTessBaseAPI(path=TESSDATA, lang="eng", psm=tesserocr.PSM.SPARSE_TEXT)
                     for _ in range(strips)]
        for api in self.apis:
            api.SetVariable("debug_file", "/dev/null")
        self.pool = ThreadPoolExecutor(strips, thread_name_prefix="ocr")
        self.lock = threading.Lock()

    def _strip(self, index: int, image, y0: int):
        api = self.apis[index]
        api.SetImage(image)
        words = []
        for row in api.GetTSVText(0).splitlines():
            cols = row.split("\t")
            if len(cols) < 12 or not cols[11].strip():
                continue
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            if conf < 45:
                continue
            left, top, width, height = (int(v) for v in cols[6:10])
            words.append({"key": (index, cols[2], cols[3], cols[4]), "text": cols[11].strip(),
                          "l": left, "t": top + y0, "r": left + width, "b": top + y0 + height})
        return words

    def read(self, image) -> list[dict]:
        width, height = image.size
        n = len(self.apis)
        step = max(1, height // n)
        jobs = []
        with self.lock:
            for i in range(n):
                y0 = max(0, i * step - OCR_OVERLAP)
                y1 = height if i == n - 1 else min(height, (i + 1) * step + OCR_OVERLAP)
                jobs.append(self.pool.submit(self._strip, i, image.crop((0, y0, width, y1)), y0))
            results = [job.result() for job in jobs]
        words, seen = [], []
        for strip_words in results:
            for w in strip_words:
                cx, cy = (w["l"] + w["r"]) / 2, (w["t"] + w["b"]) / 2
                if any(t == w["text"] and abs(cx - x) < 10 and abs(cy - y) < 10 for t, x, y in seen):
                    continue  # the same word read twice in an overlap band
                seen.append((w["text"], cx, cy))
                words.append(w)
        return words


_ocr: Ocr | None = None
_ocr_lock = threading.Lock()


def ocr_engine() -> Ocr:
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            _ocr = Ocr()
        return _ocr


def grab(x: int, y: int, w: int, h: int):
    """Screenshot a logical-pixel rectangle at native resolution, in memory."""
    from io import BytesIO

    from PIL import Image

    shot = subprocess.run(["grim", "-t", "ppm", "-g", f"{x},{y} {w}x{h}", "-"], capture_output=True, timeout=5)
    if shot.returncode or not shot.stdout:
        return None
    return Image.open(BytesIO(shot.stdout))


def ocr_region(x: int, y: int, w: int, h: int, scale: float) -> list[dict]:
    """Text phrases inside a logical-pixel rectangle, with logical screen boxes."""
    image = grab(x, y, w, h)
    if image is None:
        return []
    k = image.size[0] / max(1, w)   # screenshot px per logical px (the monitor scale)
    words = sorted(ocr_engine().read(image), key=lambda w: (w["key"], w["l"]))
    # group words into phrases: same tesseract line and no wide gap between them
    phrases: list[dict] = []
    for word in words:
        last = phrases[-1] if phrases else None
        gap = word["l"] - last["r"] if last else 0
        height = max(1, word["b"] - word["t"])
        if last and last["key"] == word["key"] and gap < height * 1.2:
            last["text"] += " " + word["text"]
            last["r"], last["b"] = max(last["r"], word["r"]), max(last["b"], word["b"])
            last["t"] = min(last["t"], word["t"])
        else:
            phrases.append(dict(word))
    out = []
    for p in phrases:
        text = p["text"].strip(" |_—-")
        if len(text) < 2 and not text.isalnum():
            continue
        out.append({
            "role": "text", "text": text[:80],
            "x": int(x + p["l"] / k), "y": int(y + p["t"] / k),
            "w": max(1, int((p["r"] - p["l"]) / k)), "h": max(1, int((p["b"] - p["t"]) / k)),
        })
    return out


# -- web ----------------------------------------------------------------------

def web_to_screen(snap: dict, win: desktop.Window, monitor_scale: float) -> tuple[float, float, float]:
    """Map viewport CSS pixels to logical screen pixels for this browser window."""
    dpr = float(snap.get("dpr") or monitor_scale)
    k = dpr / monitor_scale                        # logical px per CSS px
    inner_w, inner_h = snap.get("vw") or win.w / k, snap.get("vh") or win.h / k
    left = win.x + max(0.0, (win.w - inner_w * k) / 2)
    top = win.y + max(0.0, win.h - inner_h * k)    # browser chrome sits above the viewport
    return left, top, k


# -- scene --------------------------------------------------------------------

class Perceiver:
    def __init__(self, browser=None, settings: dict | None = None) -> None:
        self.a11y = A11y()
        self.browser = browser          # BrowserBridge or None
        self.settings = settings or {}
        self.is_own_text = lambda text: False   # set by the brain: text it recently drew on screen

    def look_size(self) -> tuple[int, int]:
        w, h = self.settings.get("look_region", [1100, 700])
        return int(w), int(h)

    def capture(self, with_elements: bool = True) -> Scene:
        started = time.perf_counter()
        desk = desktop.snapshot()
        target = desk.hovered or desk.focused
        scene = Scene(desk, target)
        scene.timings["desktop_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if not with_elements or target is None:
            return scene
        t0 = time.perf_counter()
        try:
            if browsers.is_chromium(target.cls) and self.browser:
                self._web(scene)
            if not scene.elements:
                self._a11y(scene)
            if not scene.elements:
                self._ocr(scene)
        except Exception as exc:  # perception must never break a command
            scene.timings["error"] = f"{type(exc).__name__}: {exc}"
        scene.timings[f"{scene.source or 'none'}_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._annotate(scene)
        return scene

    def _web(self, scene: Scene) -> None:
        win = scene.target
        snap = self.browser.snapshot_for(win.title)
        if not snap:
            return
        scale = float(scene.desk.monitor.get("scale") or 1)
        left, top, k = web_to_screen(snap, win, scale)
        scene.web = snap
        scene.source = "web"
        for el in snap.get("elements", []):
            text = el.get("text") or el.get("placeholder") or ""
            extra = f"-> {el['href']}" if el.get("href") else ""
            scene.elements.append(Element(
                id="", role=el["role"], text=text, x=int(left + el["x"] * k), y=int(top + el["y"] * k),
                w=int((el.get("w") or 0) * k), h=int((el.get("h") or 0) * k), source="web",
                editable=bool(el.get("is_input")), web_id=el["id"], extra=extra,
            ))

    def _a11y(self, scene: Scene) -> None:
        raw = self.a11y.elements(scene.target)
        if len(raw) < 2:
            return
        scene.source = "a11y"
        for el in raw:
            scene.elements.append(Element(
                id="", role=el["role"], text=el["text"], x=el["x"] + el["w"] // 2, y=el["y"] + el["h"] // 2,
                w=el["w"], h=el["h"], source="a11y", editable=el.get("editable", False),
                extra="checked" if el.get("checked") else "",
            ))

    def _ocr(self, scene: Scene) -> None:
        win = scene.target
        cx, cy = scene.desk.cursor
        rw, rh = self.look_size()
        if self.settings.get("look", "window") == "window":
            # read the whole window: the thing the user means is often not where the mouse is
            rw, rh = win.w, win.h
        # the look region: centered on the mouse when it is over the window, clipped to the window
        if not win.contains(cx, cy):
            cx, cy = win.x + win.w // 2, win.y + win.h // 2
        x0 = max(win.x, min(cx - rw // 2, win.x + win.w - rw))
        y0 = max(win.y, min(cy - rh // 2, win.y + win.h - rh))
        w0, h0 = min(rw, win.w), min(rh, win.h)
        scale = float(scene.desk.monitor.get("scale") or 1)
        scene.region = (x0, y0, w0, h0)
        scene.source = "ocr"
        for el in ocr_region(x0, y0, w0, h0, scale):
            scene.elements.append(Element(
                id="", role="text", text=el["text"], x=el["x"] + el["w"] // 2, y=el["y"] + el["h"] // 2,
                w=el["w"], h=el["h"], source="ocr",
            ))

    def _annotate(self, scene: Scene) -> None:
        cx, cy = scene.desk.cursor
        els = [e for e in scene.elements if e.text or e.editable]
        if scene.source == "ocr":
            # our own HUD lines are on screen too: never offer them as something to click
            els = [e for e in els if not self.is_own_text(e.text)]
            # line numbers, stray symbols and single letters are noise nobody asks for
            els = [e for e in els if sum(ch.isalpha() for ch in e.text) >= 2]
        if len(els) > MAX_ELEMENTS:
            # keep what is closest to the mouse; that is where attention is
            els = sorted(els, key=lambda e: e.distance(cx, cy))[:MAX_ELEMENTS]
        els.sort(key=lambda e: (e.y // 14, e.x))   # reading order: top-left first
        for i, el in enumerate(els, 1):
            el.id = f"e{i:02d}"
        scene.elements = els


def encode_element(el: Element, scene: Scene) -> str:
    s = f"{el.id} {el.role}"
    if el.text:
        s += " " + json.dumps(el.text[:60], ensure_ascii=False)
    if el.editable and el.role not in ("textbox", "searchbox", "combobox"):
        s += " (editable)"
    if el.extra:
        s += f" {el.extra}"
    cx, cy = scene.desk.cursor
    d = el.distance(cx, cy)
    if d == 0:
        s += " [under mouse]"
    elif d < NEAR_PX:
        s += " [next to mouse]"
    win = scene.target
    if win and win.w and win.h:
        col = "left" if el.x < win.x + win.w / 3 else "right" if el.x > win.x + 2 * win.w / 3 else "middle"
        row = "top" if el.y < win.y + win.h / 3 else "bottom" if el.y > win.y + 2 * win.h / 3 else "center"
        s += f" @{row}-{col}"
    return s
