#!/usr/bin/python3
"""AT-SPI accessibility reader, run under the system Python (which has PyGObject).

Line protocol on stdin/stdout, one JSON object per line:
  -> {"pid": 1234, "title": "Inbox - Betterbird", "window": [x, y, w, h], "limit": 150}
  <- {"ok": true, "elements": [{"role", "text", "x", "y", "w", "h", "editable"}...], "ms": 42}

Coordinates come back in screen space: Wayland gives no global positions, so the
reader asks for window-relative extents and adds the Hyprland window origin.
"""

import json
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

R = Atspi.Role
INTERACTIVE = {
    R.PUSH_BUTTON: "button", R.TOGGLE_BUTTON: "toggle", R.CHECK_BOX: "checkbox",
    R.RADIO_BUTTON: "radio", R.MENU_ITEM: "menuitem", R.CHECK_MENU_ITEM: "menuitem",
    R.RADIO_MENU_ITEM: "menuitem", R.MENU: "menu", R.LINK: "link", R.ENTRY: "textbox",
    R.PASSWORD_TEXT: "password", R.COMBO_BOX: "combobox", R.PAGE_TAB: "tab",
    R.LIST_ITEM: "item", R.TREE_ITEM: "item", R.TABLE_CELL: "cell", R.ICON: "icon",
    R.SLIDER: "slider", R.SPIN_BUTTON: "spinbutton", R.TEXT: "text", R.HEADING: "heading",
    R.PUSH_BUTTON_MENU: "button", R.SWITCH: "switch", R.TABLE_ROW: "row",
}
SKIP_SUBTREE = {R.SCROLL_BAR}
MAX_NODES = 6000
BUDGET_S = 0.6


def app_for_pid(pid):
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        try:
            if app and app.get_process_id() == pid:
                yield app
        except Exception:
            continue


def pick_frame(apps, title):
    frames = []
    for app in apps:
        for i in range(app.get_child_count()):
            child = app.get_child_at_index(i)
            if child is None:
                continue
            try:
                states = child.get_state_set()
                name = child.get_name() or ""
            except Exception:
                continue
            frames.append((child, name, states.contains(Atspi.StateType.ACTIVE)))
    if not frames:
        return None
    for frame, name, _ in frames:
        if title and name == title:
            return frame
    for frame, name, active in frames:
        if active:
            return frame
    return frames[0][0]


def text_of(node, role):
    name = (node.get_name() or "").strip()
    if name:
        return name
    try:
        text = node.get_text_iface()
        if text:
            value = Atspi.Text.get_text(text, 0, min(120, Atspi.Text.get_character_count(text)))
            if value.strip():
                return value.strip()
    except Exception:
        pass
    return (node.get_description() or "").strip()


def collect(frame, window, limit):
    wx, wy, ww, wh = window
    out = []
    stack = [frame]
    seen = 0
    deadline = time.monotonic() + BUDGET_S
    while stack and seen < MAX_NODES and time.monotonic() < deadline:
        node = stack.pop()
        seen += 1
        try:
            role = node.get_role()
            states = node.get_state_set()
        except Exception:
            continue
        if role in SKIP_SUBTREE:
            continue
        showing = states.contains(Atspi.StateType.SHOWING)
        if not showing and node is not frame:
            continue
        kind = INTERACTIVE.get(role)
        if kind:
            try:
                ext = node.get_extents(Atspi.CoordType.WINDOW)
            except Exception:
                ext = None
            if ext and ext.width > 2 and ext.height > 2:
                x, y = wx + ext.x, wy + ext.y
                # keep only what is inside the window's visible rectangle
                if x + ext.width > wx and y + ext.height > wy and x < wx + ww and y < wy + wh:
                    editable = states.contains(Atspi.StateType.EDITABLE)
                    label = text_of(node, role)
                    if kind == "text" and not editable and not label:
                        kind = None
                    if kind and (label or editable or kind in ("button", "icon", "checkbox", "toggle")):
                        out.append({
                            "role": kind, "text": " ".join(label.split())[:80],
                            "x": int(x), "y": int(y), "w": int(ext.width), "h": int(ext.height),
                            "editable": editable,
                            "checked": states.contains(Atspi.StateType.CHECKED),
                            "selected": states.contains(Atspi.StateType.SELECTED),
                        })
        try:
            n = node.get_child_count()
        except Exception:
            continue
        # Huge lists and tables: the visible part is near the top of the child list.
        for i in reversed(range(min(n, 400))):
            try:
                child = node.get_child_at_index(i)
            except Exception:
                child = None
            if child is not None:
                stack.append(child)
    # drop exact duplicates (a cell and its inner text often share a box)
    unique, keys = [], set()
    for el in out:
        key = (el["x"], el["y"], el["w"], el["h"], el["text"])
        if key not in keys:
            keys.add(key)
            unique.append(el)
    unique.sort(key=lambda e: (e["y"] // 12, e["x"]))
    return unique[:limit], seen


def handle(req):
    started = time.monotonic()
    frame = pick_frame(list(app_for_pid(int(req["pid"]))), req.get("title") or "")
    if frame is None:
        return {"ok": False, "error": "application not on the accessibility bus", "elements": []}
    elements, seen = collect(frame, req["window"], int(req.get("limit", 150)))
    return {"ok": True, "elements": elements, "nodes": seen, "ms": round((time.monotonic() - started) * 1000)}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "elements": []}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
