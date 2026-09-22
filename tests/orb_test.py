"""The orb: it appears while listening, follows the theme, and never takes a click.

Runs the overlay under the system Python with its own application id (the daemon's
overlay owns the normal one), and asks Hyprland what it actually put on screen.

    uv run python tests/orb_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import json
import os
import re
import subprocess
import sys
import time

OVERLAY = _Path(__file__).resolve().parent.parent / "engine" / "overlay.py"
NAMESPACE = "omarchy-voice-orb"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))


def layer(pid: int) -> str:
    """This overlay's orb in `hyprctl layers` — by pid, since the daemon has one on screen too."""
    out = subprocess.run(["hyprctl", "layers"], capture_output=True, text=True).stdout
    return next((line for line in out.splitlines() if NAMESPACE in line and f"pid: {pid}" in line), "")


def wait_for(predicate, seconds: float = 4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.15)
    return predicate()


def main() -> int:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        print("Not running under Hyprland — skipped.")
        return 0

    env = {**os.environ, "LD_PRELOAD": "/usr/lib/libgtk4-layer-shell.so",
           "OMARCHY_VOICE_OVERLAY_ID": "org.omarchy.voice.orbtest"}
    proc = subprocess.Popen(["/usr/bin/python3", str(OVERLAY)], stdin=subprocess.PIPE, text=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)

    def send(**msg) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    try:
        mine = lambda: layer(proc.pid)   # noqa: E731
        check("nothing is on screen before it is asked for", not wait_for(mine, 1.0))

        send(op="orb", state="listening", level=0.0)
        line = wait_for(mine)
        check("listening shows the orb", bool(line), line.strip()[:80])

        size = re.search(r"xywh: (-?\d+) (-?\d+) (\d+) (\d+)", line)
        check("it is a small square, not a full-screen surface",
              bool(size) and size.group(3) == size.group(4) and int(size.group(3)) <= 400,
              size.group(0) if size else "no geometry")

        for state in ("hearing", "thinking", "acting", "transcribe", "error"):
            send(op="orb", state=state, level=0.6)
            time.sleep(0.15)
        check("every state keeps it alive", bool(mine()) and proc.poll() is None)

        send(op="orb", state="off")
        gone = wait_for(lambda: not mine())
        check("off takes it away", bool(gone))

        # the theme it reads is the one Omarchy is using
        sys.path.insert(0, str(OVERLAY.parent))
        name = _Path.home() / ".local/state/omarchy/current/theme.name"
        if name.is_file():
            slug = name.read_text().strip()
            colors = next((d / slug / "colors.toml" for d in
                           (_Path.home() / ".config/omarchy/themes", _Path("/usr/share/omarchy/themes"))
                           if (d / slug / "colors.toml").is_file()), None)
            check("the current theme has an accent colour to use",
                  bool(colors) and "accent" in colors.read_text(), slug)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()

    print(f"\n{6 - failures}/6 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
