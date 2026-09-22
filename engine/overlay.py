#!/usr/bin/python3
"""Click-through on-screen overlay: hint badges, the zoom grid, click flashes, HUD, the orb.

Runs under the system Python (PyGObject + gtk4-layer-shell).  The layer shell
library must be loaded before GTK, so start it with
  LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so /usr/bin/python3 overlay.py

JSON lines on stdin (coordinates are logical screen pixels):
  {"op": "hints", "items": [{"label": "1", "x": 10, "y": 20, "w": 80, "h": 20}], "title": "..."}
  {"op": "grid", "rect": [x, y, w, h], "cols": 3, "rows": 3}
  {"op": "flash", "x": 100, "y": 200}
  {"op": "hud", "text": "…", "sub": "…", "ms": 2500, "tone": "ok|warn|busy"}
  {"op": "clear"}           hints + grid
  {"op": "monitor", "x": 0, "y": 0}
  {"op": "orb", "state": "listening|hearing|thinking|acting|transcribe|error|off", "level": 0.0}
"""

import json
import math
import os
import sys
import time
import tomllib
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
import cairo  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Layer  # noqa: E402

THEME_NAME = Path.home() / ".local/state/omarchy/current/theme.name"
THEME_DIRS = (Path.home() / ".config/omarchy/themes", Path("/usr/share/omarchy/themes"))
ACCENT = (0.43, 0.91, 0.72)
BADGE_BG = (0.07, 0.09, 0.15)
WARN = (0.98, 0.75, 0.30)
FONT = "sans-serif"


def hex_rgb(value: str, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    text = str(value).strip().lstrip("#")
    if len(text) != 6:
        return fallback
    try:
        return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))   # type: ignore[return-value]
    except ValueError:
        return fallback


class Theme:
    """The colours of the current Omarchy theme, re-read when the theme changes."""

    def __init__(self) -> None:
        self.accent = ACCENT
        self.background = (0.07, 0.09, 0.15)
        self.warn = WARN
        self.stamp = 0.0
        self.reload()

    def reload(self) -> None:
        try:
            stamp = THEME_NAME.stat().st_mtime
        except OSError:
            return
        if stamp == self.stamp:
            return
        self.stamp = stamp
        try:
            slug = THEME_NAME.read_text().strip()
            colors = next((d / slug / "colors.toml" for d in THEME_DIRS if (d / slug / "colors.toml").is_file()), None)
            if not colors:
                return
            data = tomllib.loads(colors.read_text())
        except Exception:
            return
        self.accent = hex_rgb(data.get("accent", ""), ACCENT)
        self.background = hex_rgb(data.get("dark_background") or data.get("background", ""), self.background)
        self.warn = hex_rgb(data.get("yellow", ""), WARN)


class Orb:
    """A soft glowing circle in the theme's accent colour: voice control is listening.

    Its own small layer surface, so the breathing animation repaints a 260 px square
    instead of the whole screen.
    """

    SIZE = 260
    STATES = ("listening", "hearing", "thinking", "acting", "transcribe", "error")

    def __init__(self, app: Gtk.Application, theme: Theme) -> None:
        self.theme = theme
        self.state = "off"
        self.level = 0.0
        self.smooth = 0.0
        self.since = time.monotonic()
        self.ticking = False
        self.win = Gtk.ApplicationWindow(application=app)
        Layer.init_for_window(self.win)
        Layer.set_layer(self.win, Layer.Layer.OVERLAY)
        Layer.set_namespace(self.win, "omarchy-voice-orb")
        Layer.set_keyboard_mode(self.win, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(self.win, -1)
        Layer.set_anchor(self.win, Layer.Edge.BOTTOM, True)
        Layer.set_margin(self.win, Layer.Edge.BOTTOM, 86)   # clear of the HUD, which sits at the bottom too
        self.win.set_default_size(self.SIZE, self.SIZE)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(self.win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.area = Gtk.DrawingArea()
        self.area.set_content_width(self.SIZE)
        self.area.set_content_height(self.SIZE)
        self.area.set_draw_func(self.draw)
        self.win.set_child(self.area)
        self.win.connect("realize", self.on_realize)
        self.win.connect("map", self.on_realize)

    def on_realize(self, *_):
        surface = self.win.get_surface()
        if surface:
            surface.set_input_region(cairo.Region())   # never steal a click

    def apply(self, msg: dict) -> None:
        state = str(msg.get("state", "off"))
        if state != self.state:
            self.since = time.monotonic()
        self.state = state if state in self.STATES else "off"
        self.level = max(0.0, min(1.0, float(msg.get("level", 0.0))))
        if self.state == "off":
            self.win.set_visible(False)
            self.ticking = False
            return
        self.theme.reload()
        if not self.win.get_visible():
            self.smooth = 0.0
            self.win.set_visible(True)
            self.on_realize()
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(33, self.tick)

    def tick(self) -> bool:
        if self.state == "off" or not self.win.get_visible():
            self.ticking = False
            return False
        self.smooth += (self.level - self.smooth) * 0.35      # follow the voice, without jitter
        self.area.queue_draw()
        return True

    def color(self) -> tuple[float, float, float]:
        if self.state == "error":
            return self.theme.warn
        return self.theme.accent

    def draw(self, _area, cr, width, height) -> None:
        cx, cy = width / 2, height / 2
        t = time.monotonic() - self.since
        r, g, b = self.color()

        if self.state == "listening":                      # slow breathing
            core = 13 + 1.6 * math.sin(t * 2.0)
            alpha, halo = 0.55, 2.9
        elif self.state == "hearing":                      # follows the voice
            core = 14 + 22 * self.smooth
            alpha, halo = 0.75 + 0.2 * self.smooth, 3.4
        elif self.state == "thinking":                     # quicker pulse while it decides
            core = 15 + 3.5 * math.sin(t * 7.0)
            alpha, halo = 0.7, 3.1
        elif self.state == "acting":                       # one bright bloom
            age = min(1.0, t / 0.45)
            core = 16 + 26 * (1 - age)
            alpha, halo = 0.85 * (1 - age) + 0.25, 3.6
        elif self.state == "transcribe":                   # steady and wide: it is recording you
            core = 17 + 1.2 * math.sin(t * 3.2) + 14 * self.smooth
            alpha, halo = 0.8, 3.2
        else:                                              # error
            core = 16 + 4 * math.sin(t * 9.0)
            alpha, halo = 0.85, 3.0

        outer = min(core * halo, self.SIZE / 2 - 6)
        glow = cairo.RadialGradient(cx, cy, core * 0.35, cx, cy, outer)
        glow.add_color_stop_rgba(0.0, r, g, b, alpha)
        glow.add_color_stop_rgba(0.45, r, g, b, alpha * 0.28)
        glow.add_color_stop_rgba(1.0, r, g, b, 0.0)
        cr.set_source(glow)
        cr.arc(cx, cy, outer, 0, 2 * math.pi)
        cr.fill()

        core_gradient = cairo.RadialGradient(cx - core * 0.3, cy - core * 0.35, core * 0.1, cx, cy, core)
        core_gradient.add_color_stop_rgba(0.0, min(1, r + 0.35), min(1, g + 0.35), min(1, b + 0.35), 0.98)
        core_gradient.add_color_stop_rgba(1.0, r, g, b, 0.92)
        cr.set_source(core_gradient)
        cr.arc(cx, cy, core, 0, 2 * math.pi)
        cr.fill()

        if self.state == "thinking":                       # a ring that goes round while it decides
            cr.set_source_rgba(r, g, b, 0.9)
            cr.set_line_width(2.5)
            start = (t * 3.0) % (2 * math.pi)
            cr.arc(cx, cy, core + 9, start, start + 1.4)
            cr.stroke()


class Overlay:
    def __init__(self, app: Gtk.Application, theme: Theme | None = None) -> None:
        self.theme = theme or Theme()
        self.win = Gtk.ApplicationWindow(application=app)
        Layer.init_for_window(self.win)
        Layer.set_layer(self.win, Layer.Layer.OVERLAY)
        Layer.set_namespace(self.win, "omarchy-voice-overlay")
        Layer.set_keyboard_mode(self.win, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(self.win, -1)
        for edge in (Layer.Edge.TOP, Layer.Edge.BOTTOM, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(self.win, edge, True)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(self.win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        self.win.set_child(self.area)
        self.win.connect("realize", self.on_realize)
        self.win.connect("map", self.on_realize)   # a hidden layer surface can come back as a new wl_surface

        self.origin = (0, 0)
        self.hints: list[dict] = []
        self.hint_title = ""
        self.grid = None
        self.flashes: list[tuple[float, float, float]] = []
        self.hud = None
        self.hud_until = 0.0
        self.visible = False
        self.ticking = False

    def on_realize(self, *_):
        surface = self.win.get_surface()
        if surface:
            surface.set_input_region(cairo.Region())  # never steal a click

    # -- state ---------------------------------------------------------------
    def apply(self, msg: dict) -> None:
        op = msg.get("op")
        now = time.monotonic()
        if op == "hints":
            self.hints = msg.get("items", [])
            self.hint_title = msg.get("title", "")
            self.grid = None
        elif op == "grid":
            self.grid = {"rect": msg["rect"], "cols": int(msg.get("cols", 3)), "rows": int(msg.get("rows", 3))}
            self.hints = []
        elif op == "clear":
            self.hints, self.grid = [], None
        elif op == "flash":
            self.flashes.append((float(msg["x"]), float(msg["y"]), now))
        elif op == "hud":
            self.hud = {"text": msg.get("text", ""), "sub": msg.get("sub", ""), "tone": msg.get("tone", "ok")}
            self.hud_until = now + msg.get("ms", 2200) / 1000
        elif op == "orb":
            return      # handled by the orb window (see main)
        elif op == "monitor":
            self.origin = (int(msg.get("x", 0)), int(msg.get("y", 0)))
        self.refresh()

    def active(self) -> bool:
        now = time.monotonic()
        self.flashes = [f for f in self.flashes if now - f[2] < 0.6]
        if self.hud and now > self.hud_until:
            self.hud = None
        return bool(self.hints or self.grid or self.flashes or self.hud)

    def refresh(self) -> None:
        if self.active():
            if not self.visible:
                self.win.set_visible(True)
                self.visible = True
                self.on_realize()
            self.area.queue_draw()
            if not self.ticking and (self.flashes or self.hud):
                self.ticking = True
                GLib.timeout_add(33, self.tick)
        elif self.visible:
            self.win.set_visible(False)
            self.visible = False

    def tick(self) -> bool:
        self.refresh()
        keep = bool(self.flashes or self.hud)
        if not keep:
            self.ticking = False
        return keep

    # -- drawing ---------------------------------------------------------------
    def draw(self, _area, cr, width, height) -> None:
        ox, oy = self.origin
        cr.translate(-ox, -oy)
        if self.grid:
            self.draw_grid(cr)
        for item in self.hints:
            self.draw_badge(cr, item)
        now = time.monotonic()
        for x, y, t in self.flashes:
            age = (now - t) / 0.6
            cr.set_source_rgba(*self.theme.accent, max(0.0, 0.9 * (1 - age)))
            cr.set_line_width(3)
            cr.new_path()
            cr.arc(x, y, 8 + 34 * age, 0, 2 * math.pi)
            cr.stroke()
        if self.hud:
            cr.translate(ox, oy)
            self.draw_hud(cr, width, height)

    def rounded(self, cr, x, y, w, h, r) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def draw_badge(self, cr, item: dict) -> None:
        label = str(item["label"])
        x, y = float(item["x"]), float(item["y"])
        w, h = float(item.get("w", 0)), float(item.get("h", 0))
        if w and h:
            cr.set_source_rgba(*ACCENT, 0.55)
            cr.set_line_width(1.5)
            self.rounded(cr, x - w / 2, y - h / 2, w, h, 4)
            cr.stroke()
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(15)
        ext = cr.text_extents(label)
        bw, bh = ext.width + 14, 22
        bx = x - w / 2 - 4 if w else x - bw / 2
        by = y - h / 2 - bh + 4 if h else y - bh / 2
        cr.set_source_rgba(*BADGE_BG, 0.93)
        self.rounded(cr, bx, by, bw, bh, 7)
        cr.fill()
        cr.set_source_rgba(*ACCENT, 1)
        cr.move_to(bx + 7 - ext.x_bearing, by + bh / 2 - ext.y_bearing - ext.height / 2)
        cr.show_text(label)

    def draw_grid(self, cr) -> None:
        gx, gy, gw, gh = (float(v) for v in self.grid["rect"])
        cols, rows = self.grid["cols"], self.grid["rows"]
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.rectangle(gx, gy, gw, gh)
        cr.fill()
        cr.set_source_rgba(*ACCENT, 0.85)
        cr.set_line_width(2)
        cr.rectangle(gx, gy, gw, gh)
        cr.stroke()
        cr.set_line_width(1)
        for c in range(1, cols):
            cr.move_to(gx + gw * c / cols, gy)
            cr.line_to(gx + gw * c / cols, gy + gh)
        for r in range(1, rows):
            cr.move_to(gx, gy + gh * r / rows)
            cr.line_to(gx + gw, gy + gh * r / rows)
        cr.stroke()
        size = max(12, min(64, min(gw / cols, gh / rows) * 0.35))
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(size)
        n = 1
        for r in range(rows):
            for c in range(cols):
                label = str(n)
                ext = cr.text_extents(label)
                cx = gx + gw * (c + 0.5) / cols
                cy = gy + gh * (r + 0.5) / rows
                cr.set_source_rgba(*BADGE_BG, 0.55)
                cr.new_path()
                cr.arc(cx, cy, size * 0.75, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(*ACCENT, 0.95)
                cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
                cr.show_text(label)
                n += 1
        # the point a "click" would hit
        cr.set_source_rgba(*WARN, 0.95)
        cr.new_path()
        cr.arc(gx + gw / 2, gy + gh / 2, 4, 0, 2 * math.pi)
        cr.fill()

    def draw_hud(self, cr, width, height) -> None:
        text, sub = self.hud["text"], self.hud["sub"]
        accent = self.theme.accent
        tone = {"ok": accent, "warn": self.theme.warn, "busy": (0.6, 0.7, 1.0)}.get(self.hud["tone"], accent)
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(18)
        e1 = cr.text_extents(text)
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(14)
        e2 = cr.text_extents(sub) if sub else None
        w = max(e1.x_advance, e2.x_advance if e2 else 0) + 40
        h = 58 if sub else 40
        x, y = (width - w) / 2, height - h - 36
        cr.set_source_rgba(*self.theme.background, 0.94)
        self.rounded(cr, x, y, w, h, 14)
        cr.fill()
        cr.set_source_rgba(*tone, 1)
        cr.rectangle(x + 12, y + 12, 4, h - 24)
        cr.fill()
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(18)
        cr.set_source_rgba(1, 1, 1, 0.96)
        cr.move_to(x + 26, y + 26)
        cr.show_text(text)
        if sub:
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(14)
            cr.set_source_rgba(1, 1, 1, 0.62)
            cr.move_to(x + 26, y + 46)
            cr.show_text(sub)


def main() -> None:
    # one overlay per daemon; a second instance with the same id would hand over and exit
    app = Gtk.Application(application_id=os.environ.get("OMARCHY_VOICE_OVERLAY_ID", "org.omarchy.voice.overlay"))
    state = {}

    def on_line(channel, _cond):
        line = channel.readline()
        if not line:
            app.quit()
            return False
        try:
            msg = json.loads(line)
            if msg.get("op") == "orb":
                state["orb"].apply(msg)
            else:
                state["overlay"].apply(msg)
        except Exception as exc:
            print(f"overlay: {exc}", file=sys.stderr, flush=True)
        return True

    def on_activate(app):
        theme = Theme()
        state["overlay"] = Overlay(app, theme)
        state["orb"] = Orb(app, theme)
        GLib.timeout_add_seconds(5, lambda: (theme.reload(), True)[1])   # follow `omarchy theme set`
        app.hold()
        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, on_line)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
