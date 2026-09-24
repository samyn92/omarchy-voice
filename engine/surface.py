"""The standard surface: one way to see and operate whichever app is in front.

Electron and Chromium apps are web pages inside. Started with a debug port they answer the
same protocol as the browser, and everything the goal loop does in a browser — read the
elements, click, type, look again — works in them unchanged.  This module finds out whether
the focused window can be operated that way, and starts the apps the user opted in with a
port, bound to this machine only.

A debug port lets any program on this machine drive that app, so it is opt-in per app
(`surface_apps` in the config, `omarchy-voice surface add <app>`), and listed wherever the
engine shows what it can reach.
"""

from __future__ import annotations

import os
import re
import shlex
import socket
import zlib
from pathlib import Path

import apps
import browsers

# Never started with a debug port: a port on an unlocked vault would let any program on this
# machine read it.  Not a setting — there is no way to opt in.
NEVER = re.compile(r"bitwarden|1password|keepass|proton ?pass|enpass|lastpass|dashlane|keeper|passbolt|vault", re.I)

PORT_BASE = 9300           # 9300-9389: one stable port per app, away from the browser's 9222/9223
PORT_SPAN = 90
_bridges: dict[int, object] = {}


def debug_port(pid: int) -> int | None:
    """The debug port a process was started with, if any."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    for i, raw in enumerate(args):
        arg = raw.decode(errors="replace")
        if arg.startswith("--remote-debugging-port="):
            value = arg.split("=", 1)[1]
            return int(value) if value.isdigit() else None
        if arg == "--remote-debugging-port" and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
    return None


def app_key(window_class: str) -> str:
    """The name skills and maps are filed under: the window class, lowercased."""
    return re.sub(r"[^a-z0-9._-]+", "-", window_class.lower()).strip("-") or "app"


def kind_of(window) -> str:
    """browser · app (operable) · closed (an app that could be, but was started without a port)."""
    if browsers.is_chromium(window.cls):
        return "browser"
    if debug_port(window.pid):
        return "app"
    return "closed"


def bridge(port: int):
    """One bridge per port, reused: connecting costs a few hundred ms."""
    from browser import BrowserBridge

    if port not in _bridges:
        _bridges[port] = BrowserBridge((f"http://127.0.0.1:{port}",))
    return _bridges[port]


def for_window(window):
    """(bridge, key) for an operable app window, else None."""
    if window is None or browsers.is_chromium(window.cls):
        return None
    port = debug_port(window.pid)
    if not port:
        return None
    return bridge(port), app_key(window.cls)


# -- starting apps with a port -------------------------------------------------------------
def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def port_for(app: apps.App) -> int:
    """A stable port per app, so the same app always lands on the same one."""
    start = PORT_BASE + zlib.crc32(app.id.encode()) % PORT_SPAN
    for offset in range(PORT_SPAN):
        port = PORT_BASE + (start - PORT_BASE + offset) % PORT_SPAN
        if _free(port):
            return port
    return start


def _exec_line(app: apps.App) -> list[str] | None:
    for base in (Path.home() / ".local/share/applications", Path("/usr/share/applications")):
        path = base / app.id
        if not path.is_file():
            continue
        in_entry = False
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("["):
                in_entry = line.strip() == "[Desktop Entry]"
            elif in_entry and line.startswith("Exec="):
                argv = shlex.split(line[5:])
                # field codes are for file managers: "%U", and also "--uri=%u" inside an argument
                return [a for a in argv if not re.search(r"%[fFuUick]", a)]
    return None


def refused(app: apps.App) -> bool:
    return bool(NEVER.search(f"{app.name} {app.id} {app.wm_class}"))


def opted_in(app: apps.App, settings: dict) -> bool:
    if refused(app):
        return False
    wanted = {str(a).lower().removesuffix(".desktop") for a in settings.get("surface_apps") or []}
    names = {app.id.lower().removesuffix(".desktop"), app.name.lower(), app.wm_class.lower()}
    return bool(wanted & names)


def electron_entry(app: apps.App) -> apps.App:
    """Of several desktop entries with this name, the one that starts the Electron app itself.

    Hermes has two: a CLI in ~/.local that may not pass flags on, and hermes-desktop, which does.
    """
    if is_electron(app):
        return app
    same = [a for a in apps.installed() if a.name.lower() == app.name.lower() and a.id != app.id]
    return next((a for a in same if is_electron(a)), app)


def launch_argv(app: apps.App) -> list[str] | None:
    """How to start `app` so it can be operated — None if its desktop entry cannot be read."""
    if refused(app):
        return None
    app = electron_entry(app)
    argv = _exec_line(app)
    if not argv:
        return None
    flags = [f"--remote-debugging-port={port_for(app)}", "--remote-debugging-address=127.0.0.1"]
    if "--" in argv:              # "signal-desktop -- %u": everything after -- is not a flag
        cut = argv.index("--")
        return ["uwsm-app", "--", *argv[:cut], *flags, *argv[cut:]]
    return ["uwsm-app", "--", *argv, *flags]


def is_electron(app: apps.App) -> bool:
    """Whether an app is Chromium inside — the kind the debug port works for."""
    argv = _exec_line(app) or []
    if not argv:
        return False
    target = argv[0]
    path = Path(target) if os.path.isabs(target) else None
    if path is None:
        import shutil

        found = shutil.which(target)
        path = Path(found) if found else None
    if path is None:
        return False
    real = path.resolve()
    folders = [real.parent]
    try:
        text = real.read_text(errors="ignore")[:4000] if real.stat().st_size < 200_000 else ""
    except OSError:
        text = ""
    if re.search(r"\belectron\d*\b", text):                         # "exec electron43 …/app.asar"
        return True
    for match in re.findall(r"(/(?:opt|usr/lib|usr/share)/[\w.+-]+)", text):   # wrappers name their app folder
        folders.append(Path(match))
    return any((f / "chrome_100_percent.pak").exists() or (f / "resources" / "app.asar").exists()
               or (f / "app.asar").exists() for f in folders)
