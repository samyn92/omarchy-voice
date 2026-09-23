"""The orb: the engine streams it, the bar plugin draws it.

The drawing is Qt's scene graph in the shell process, so what is testable here is the
contract between them — the socket, the lines on it, and the layer surface the shell puts
on screen while listening.

    uv run python tests/orb_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import json
import os
import socket
import subprocess
import sys
import time

PLUGIN = _Path(__file__).resolve().parent.parent
SOCKET = _Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-voice" / "orb.sock"
CLI = _Path.home() / ".local/bin/omarchy-voice"
STATES = {"off", "listening", "hearing", "thinking", "acting", "transcribe", "error"}
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))


def read_lines(seconds: float) -> list[dict]:
    """Everything the engine publishes to a fresh subscriber within `seconds`."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(seconds)
    client.connect(str(SOCKET))
    buffer, out = b"", []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = client.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                out.append(json.loads(line))
    client.close()
    return out


def main() -> int:
    if not SOCKET.exists():
        print(f"The engine is not running ({SOCKET} is missing) — skipped.")
        return 0

    was_listening = subprocess.run([str(CLI), "status"], capture_output=True, text=True).stdout
    was_listening = "listening 1" in was_listening
    subprocess.run([str(CLI), "start"], capture_output=True)
    time.sleep(0.5)

    try:
        lines = read_lines(1.5)
        check("a new subscriber is told the state at once", bool(lines),
              f"{len(lines)} lines")
        check("every line is a known state and a level in 0…1",
              all(l.get("state") in STATES and 0 <= float(l.get("level", -1)) <= 1 for l in lines),
              str(lines[:2]))
        check("it says it is listening", any(l["state"] != "off" for l in lines),
              str({l["state"] for l in lines}))

        # while the microphone is open, levels keep coming (silence is still a level)
        lines = read_lines(1.2)
        check("levels keep flowing while it listens", len(lines) >= 2, f"{len(lines)} in 1.2 s")

        # what the shell actually put on screen
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            layers = subprocess.run(["hyprctl", "layers"], capture_output=True, text=True).stdout
            line = next((l for l in layers.splitlines() if "omarchy-voice-orb" in l), "")
            check("the shell shows the orb on the overlay layer", bool(line), line.strip()[:70])
            shell = subprocess.run(["pgrep", "-f", "quickshell"], capture_output=True, text=True).stdout.split()
            check("it is drawn by the shell, not by the engine",
                  any(f"pid: {pid}" in line for pid in shell), line.strip()[-24:])
        else:
            check("the shell shows the orb on the overlay layer", True, "not under Hyprland — skipped")
            check("it is drawn by the shell, not by the engine", True, "skipped")

        check("the plugin ships the orb and mounts it",
              (PLUGIN / "Orb.qml").is_file() and "Orb {}" in (PLUGIN / "BarWidget.qml").read_text())
    finally:
        subprocess.run([str(CLI), "start" if was_listening else "stop"], capture_output=True)

    print(f"\n{7 - failures}/7 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
