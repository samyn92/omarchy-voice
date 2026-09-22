"""Hyprland as a set of addressable things: windows, workspaces, the cursor.

Talks to Hyprland's command socket directly (~0.1 ms per request, versus a
process spawn per `hyprctl`).  Every mutation is a fixed Lua dispatcher template
filled with validated values — never text from a model.
"""

from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
from dataclasses import dataclass, field

DIRECTIONS = {"left": "l", "right": "r", "up": "u", "down": "d"}


def _socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        return f"{runtime}/hypr/{sig}/.socket.sock"
    candidates = sorted(glob.glob(f"{runtime}/hypr/*/.socket.sock"), key=os.path.getmtime)
    if not candidates:
        raise RuntimeError("Hyprland socket not found")
    return candidates[-1]


def request(command: str) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(_socket_path())
        sock.sendall(command.encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def query(what: str):
    return json.loads(request(f"j/{what}"))


def dispatch(lua: str) -> tuple[bool, str]:
    out = request(f"dispatch {lua}").strip()
    return out == "ok", out


def lua_str(value: str) -> str:
    return json.dumps(str(value))  # JSON string literal is a valid Lua string literal


def cursor() -> tuple[int, int]:
    pos = query("cursorpos")
    return int(pos["x"]), int(pos["y"])


def move_cursor(x: int, y: int) -> None:
    dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})")


@dataclass
class Window:
    address: str
    cls: str
    title: str
    workspace: int
    workspace_name: str
    x: int
    y: int
    w: int
    h: int
    floating: bool
    pinned: bool
    fullscreen: int
    pid: int
    focus_rank: int
    grouped: bool
    id: str = ""
    focused: bool = False
    under_cursor: bool = False

    @property
    def selector(self) -> str:
        return f"address:{self.address}"

    @property
    def visible_region(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    @property
    def app(self) -> str:
        return self.cls.split(".")[-1] if "." in self.cls else self.cls


@dataclass
class Desktop:
    windows: list[Window]
    workspaces: list[dict]
    monitors: list[dict]
    active_workspace: int
    cursor: tuple[int, int]
    layout: str = "scrolling"
    by_id: dict[str, Window] = field(default_factory=dict)

    @property
    def focused(self) -> Window | None:
        return next((w for w in self.windows if w.focused), None)

    @property
    def hovered(self) -> Window | None:
        return next((w for w in self.windows if w.under_cursor), None)

    @property
    def monitor(self) -> dict:
        for m in self.monitors:
            if m.get("focused"):
                return m
        return self.monitors[0]

    def logical_monitor(self) -> tuple[int, int, int, int]:
        m = self.monitor
        scale = float(m.get("scale") or 1)
        return int(m["x"]), int(m["y"]), round(m["width"] / scale), round(m["height"] / scale)

    def screen_side(self, win: Window) -> str:
        mx, _, mw, _ = self.logical_monitor()
        cx = (win.x + win.w / 2 - mx) / max(1, mw)
        return "left" if cx < 0.36 else "right" if cx > 0.64 else "center"


def snapshot() -> Desktop:
    clients = query("clients")
    workspaces = query("workspaces")
    monitors = query("monitors")
    active = query("activewindow") or {}
    cx, cy = cursor()
    active_ws = next((m["activeWorkspace"]["id"] for m in monitors if m.get("focused")), monitors[0]["activeWorkspace"]["id"])
    visible_ws = {m["activeWorkspace"]["id"] for m in monitors}
    visible_ws |= {m["specialWorkspace"]["id"] for m in monitors if m.get("specialWorkspace", {}).get("id")}

    windows: list[Window] = []
    for c in clients:
        if not c.get("mapped", True) or c.get("hidden"):
            continue
        ws = c["workspace"]
        windows.append(Window(
            address=c["address"], cls=c.get("class") or c.get("initialClass") or "?",
            title=c.get("title") or "", workspace=int(ws["id"]), workspace_name=str(ws.get("name", ws["id"])),
            x=c["at"][0], y=c["at"][1], w=c["size"][0], h=c["size"][1],
            floating=bool(c.get("floating")), pinned=bool(c.get("pinned")),
            fullscreen=int(c.get("fullscreen") or 0), pid=int(c.get("pid") or 0),
            focus_rank=int(c.get("focusHistoryID", 99)), grouped=bool(c.get("grouped")),
        ))
    # Visible workspace first, then by recency: w01 is what the user most likely means.
    windows.sort(key=lambda w: (w.workspace not in visible_ws, w.focus_rank))
    for i, w in enumerate(windows, 1):
        w.id = f"w{i:02d}"
        w.focused = w.address == active.get("address")
    under = [w for w in windows if w.workspace in visible_ws and w.contains(cx, cy)]
    if under:
        # floating/pinned windows sit on top of tiled ones
        top = sorted(under, key=lambda w: (not (w.floating or w.pinned), w.focus_rank))[0]
        top.under_cursor = True
    layout = next((w.get("tiledLayout") for w in workspaces if w["id"] == active_ws), "") or "scrolling"
    return Desktop(windows, workspaces, monitors, active_ws, (cx, cy), layout, {w.id: w for w in windows})


def encode_window(win: Window, desk: Desktop, kind: str = "") -> str:
    title = win.title.replace("\n", " ")[:70]
    parts = [win.id, win.app + (f" ({kind.lower()})" if kind else "")]
    if title and title.lower() != win.app.lower():
        parts.append(json.dumps(title, ensure_ascii=False))
    if win.workspace_name.startswith("special:"):
        parts.append(f"(hidden in {win.workspace_name.removeprefix('special:')})")
    else:
        where = "this workspace" if win.workspace == desk.active_workspace else f"workspace {win.workspace}"
        parts.append(f"on {where}")
        if win.workspace == desk.active_workspace:
            parts.append(f"{desk.screen_side(win)} of screen")
    flags = [name for name, on in (
        ("focused", win.focused), ("under mouse", win.under_cursor), ("floating", win.floating),
        ("pinned", win.pinned), ("fullscreen", win.fullscreen > 0),
    ) if on]
    if flags:
        parts.append("[" + ", ".join(flags) + "]")
    return " ".join(parts)


# -- actions ------------------------------------------------------------------

def _win(win: Window | None) -> str:
    return f"window = {lua_str(win.selector)}" if win else ""


def focus_window(win: Window) -> tuple[bool, str]:
    return dispatch(f"hl.dsp.focus({{ {_win(win)} }})")


def with_focus(win: Window | None, lua: str) -> tuple[bool, str]:
    """For dispatchers that act on the active window: focus the target first."""
    if win and not win.focused:
        ok, out = focus_window(win)
        if not ok:
            return ok, out
    return dispatch(lua)


def window_verb(verb: str, win: Window | None, desk: Desktop, **slots) -> tuple[bool, str]:
    sel = _win(win)
    sel_comma = f"{sel}, " if sel else ""
    if verb == "focus":
        return focus_window(win) if win else (False, "no window")
    if verb == "close":
        return dispatch(f"hl.dsp.window.close({{ {sel} }})")
    if verb == "move_to_workspace":
        ws = slots["workspace"]
        follow = "true" if slots.get("follow", True) else "false"
        return dispatch(f"hl.dsp.window.move({{ {sel_comma}workspace = {lua_str(ws)}, follow = {follow} }})")
    if verb == "minimize":
        return dispatch(f"hl.dsp.window.move({{ {sel_comma}workspace = \"special:minimized\", follow = false }})")
    if verb == "restore":
        ws = str(desk.active_workspace)
        return dispatch(f"hl.dsp.window.move({{ {sel_comma}workspace = {lua_str(ws)}, follow = true }})")
    if verb == "fullscreen":
        return with_focus(win, 'hl.dsp.window.fullscreen({ mode = "fullscreen" })')
    if verb == "maximize":
        return with_focus(win, 'hl.dsp.window.fullscreen({ mode = "maximized" })')
    if verb == "float":
        return dispatch(f"hl.dsp.window.float({{ {sel_comma}action = \"toggle\" }})")
    if verb == "pin":
        if win and not win.floating:
            dispatch(f"hl.dsp.window.float({{ {sel_comma}action = \"enable\" }})")
        return dispatch(f"hl.dsp.window.pin({{ {sel} }})")
    if verb == "center":
        if win and not win.floating:
            dispatch(f"hl.dsp.window.float({{ {sel_comma}action = \"enable\" }})")
        return dispatch(f"hl.dsp.window.center({{ {sel} }})")
    if verb in ("wider", "narrower", "taller", "shorter"):
        step = {"little": 0.04, "normal": 0.1, "lot": 0.25}.get(slots.get("amount", "normal"), 0.1)
        if win and (win.floating or desk.layout != "scrolling") or verb in ("taller", "shorter"):
            _, _, mw, mh = desk.logical_monitor()
            dx = round(mw * step) * (1 if verb == "wider" else -1 if verb == "narrower" else 0)
            dy = round(mh * step) * (1 if verb == "taller" else -1 if verb == "shorter" else 0)
            return dispatch(f"hl.dsp.window.resize({{ {sel_comma}x = {dx}, y = {dy}, relative = true }})")
        return with_focus(win, f'hl.dsp.layout("colresize {"+conf" if verb == "wider" else "-conf"}")')
    if verb == "width":
        fraction = max(0.1, min(1.0, float(slots["fraction"])))
        if win and win.floating:
            _, _, mw, _ = desk.logical_monitor()
            return dispatch(f"hl.dsp.window.resize({{ {sel_comma}x = {round(mw * fraction)}, y = {win.h} }})")
        return with_focus(win, f'hl.dsp.layout("colresize {fraction:.3f}")')
    if verb == "snap":
        region = slots["region"]
        mx, my, mw, mh = desk.logical_monitor()
        gap = 12
        top = my + 42  # below the Omarchy bar
        full_h = mh - (top - my) - gap
        halves = {
            "left": (mx + gap, top, mw // 2 - 1.5 * gap, full_h),
            "right": (mx + mw // 2 + gap / 2, top, mw // 2 - 1.5 * gap, full_h),
            "top": (mx + gap, top, mw - 2 * gap, full_h // 2 - gap / 2),
            "bottom": (mx + gap, top + full_h // 2 + gap / 2, mw - 2 * gap, full_h // 2 - gap / 2),
            "center": (mx + mw // 4, top + full_h // 8, mw // 2, full_h * 3 // 4),
            "top_left": (mx + gap, top, mw // 2 - 1.5 * gap, full_h // 2 - gap / 2),
            "top_right": (mx + mw // 2 + gap / 2, top, mw // 2 - 1.5 * gap, full_h // 2 - gap / 2),
            "bottom_left": (mx + gap, top + full_h // 2 + gap / 2, mw // 2 - 1.5 * gap, full_h // 2 - gap / 2),
            "bottom_right": (mx + mw // 2 + gap / 2, top + full_h // 2 + gap / 2, mw // 2 - 1.5 * gap, full_h // 2 - gap / 2),
        }
        if region not in halves:
            return False, f"unknown region {region}"
        x, y, w, h = (int(v) for v in halves[region])
        if win and not win.floating:
            dispatch(f"hl.dsp.window.float({{ {sel_comma}action = \"enable\" }})")
        ok, out = dispatch(f"hl.dsp.window.resize({{ {sel_comma}x = {w}, y = {h} }})")
        if not ok:
            return ok, out
        return dispatch(f"hl.dsp.window.move({{ {sel_comma}x = {x}, y = {y} }})")
    if verb == "tile":
        return dispatch(f"hl.dsp.window.float({{ {sel_comma}action = \"disable\" }})")
    if verb == "swap":
        d = DIRECTIONS[slots["direction"]]
        if desk.layout == "scrolling" and d in "lr" and not (win and win.floating):
            return with_focus(win, f'hl.dsp.layout("swapcol {d}")')
        return with_focus(win, f'hl.dsp.window.swap({{ direction = "{d}" }})')
    if verb == "group":
        return with_focus(win, "hl.dsp.group.toggle()")
    if verb == "fit":
        return with_focus(win, 'hl.dsp.layout("fit active")')
    return False, f"unknown window verb {verb}"


def focus_direction(direction: str) -> tuple[bool, str]:
    return dispatch(f'hl.dsp.focus({{ direction = "{DIRECTIONS[direction]}" }})')


def go_workspace(target: str) -> tuple[bool, str]:
    return dispatch(f"hl.dsp.focus({{ workspace = {lua_str(target)} }})")


def toggle_special(name: str = "scratchpad") -> tuple[bool, str]:
    return dispatch(f"hl.dsp.workspace.toggle_special({lua_str(name)})")


def send_shortcut(mods: str, key: str, win: Window | None = None) -> tuple[bool, str]:
    extra = f", {_win(win)}" if win else ""
    return dispatch(f"hl.dsp.send_shortcut({{ mods = {lua_str(mods)}, key = {lua_str(key)}{extra} }})")


def exec_detached(argv: list[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
