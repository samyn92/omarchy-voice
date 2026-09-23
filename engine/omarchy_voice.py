"""Voice control for Omarchy, Hyprland, and the local desktop.

The daemon owns microphone capture, two racing Whisper recognizers and the
desktop brain (brain.py): Jev decisions over the open windows and what is on
screen around the mouse, executed through Hyprland and uinput pointer/keyboard devices.
UI clients use its Unix socket.  Model output only ever selects ids from
catalogs built in code; it is never treated as a command line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import ear as ear_client
import livemic

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "omarchy-voice"
STATE_PATH = RUNTIME_DIR / "state.json"
LISTENING_PATH = RUNTIME_DIR / "listening"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
ORB_SOCKET_PATH = RUNTIME_DIR / "orb.sock"     # one line per change, for the bar widget's orb
TRACE_PATH = RUNTIME_DIR / "trace.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "omarchy-voice"
HISTORY_PATH = STATE_DIR / "history.jsonl"   # every utterance and its outcome; stays on this machine
TESTING_PATH = RUNTIME_DIR / "testing"         # set by the e2e scripts: their speech is not the user's
VOXTYPE_STATE = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voxtype" / "state"


class OrbStream:
    """Pushes {state, level} lines to whoever listens — the orb in the bar plugin.

    Levels are disposable: a client that cannot keep up is skipped, never waited for, so
    the audio thread is never blocked by drawing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.clients: list[socket.socket] = []
        self.lock = threading.Lock()
        self.last = '{"state": "off", "level": 0}'
        self.server: socket.socket | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(4)
        threading.Thread(target=self._accept, name="orb-stream", daemon=True).start()

    def _accept(self) -> None:
        while self.server is not None:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            client.setblocking(False)
            with self.lock:
                self.clients.append(client)
            try:            # the current state, so a widget that starts later is not blank
                client.sendall((self.last + "\n").encode())
            except OSError:
                pass

    def publish(self, state: str, level: float) -> None:
        line = json.dumps({"state": state, "level": round(float(level), 3)})
        self.last = line
        data = (line + "\n").encode()
        with self.lock:
            for client in list(self.clients):
                try:
                    client.sendall(data)
                except BlockingIOError:
                    pass          # its buffer is full: skip this level, the next one is 25 ms away
                except OSError:
                    self.clients.remove(client)
                    client.close()

    def stop(self) -> None:
        server, self.server = self.server, None
        if server:
            server.close()
        with self.lock:
            for client in self.clients:
                client.close()
            self.clients.clear()
        self.path.unlink(missing_ok=True)


def voxtype_state() -> str:
    try:
        return VOXTYPE_STATE.read_text().strip()
    except OSError:
        return ""
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "omarchy-voice"
SETTINGS_PATH = CONFIG_DIR / "config.json"

CONFIRM_WORDS = {"confirm", "yes confirm", "do it", "go ahead", "proceed"}
CANCEL_WORDS = {"cancel", "never mind", "nevermind", "stop", "no cancel"}
STOP_WORDS = {"stop listening", "voice control off", "go to sleep", "stop voice control"}
ENABLE_YOLO_WORDS = {"enable yolo mode", "turn on yolo mode", "yolo mode on"}
DISABLE_YOLO_WORDS = {"disable yolo mode", "turn off yolo mode", "yolo mode off"}
TOGGLE_YOLO_WORDS = {"toggle yolo mode"}
# Whisper writes Jev as "Jeff" or "Chef"
JEV_ONLY_WORDS = {f"{verb}{name} only{mode}" for verb in ("", "toggle ") for name in ("jev", "jeff", "chef", "jeb")
                  for mode in ("", " mode")}
# A pause after these means the sentence is not finished: wait for the rest.
INCOMPLETE = re.compile(
    r"(?:search(?: the)?(?: web| internet| on google| google| youtube| on youtube)?(?: for)?|look up|google|type|write|enter|"
    r"go to|open|click on|select the|press the|switch to|move (?:this |the )?(?:window )?to|"
    r"send (?:this |it )?to|put (?:this |it )?on|make (?:it|this)|snap (?:it|this)?(?: to)?)"
)
PARTIAL_TTL = 6.0


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    group: str
    description: str
    argv: tuple[str, ...]
    aliases: tuple[str, ...]
    confirm: bool = False
    detached: bool = False


def action(
    id: str,
    label: str,
    group: str,
    description: str,
    argv: Iterable[str],
    *aliases: str,
    confirm: bool = False,
    detached: bool = False,
) -> Action:
    return Action(id, label, group, description, tuple(argv), tuple(aliases), confirm, detached)


_ACTIONS = [
    action("volume_up", "Volume up", "audio", "Raise speaker volume", ["omarchy", "audio", "output", "volume", "raise"], "volume up", "turn up the volume", "make it louder"),
    action("volume_down", "Volume down", "audio", "Lower speaker volume", ["omarchy", "audio", "output", "volume", "lower"], "volume down", "turn down the volume", "make it quieter"),
    action("mute_audio", "Mute speakers", "audio", "Toggle speaker mute", ["omarchy", "audio", "output", "volume", "mute-toggle"], "mute", "mute audio", "mute the speakers", "unmute", "unmute audio"),
    action("mute_microphone", "Mute microphone", "audio", "Toggle microphone mute", ["omarchy", "audio", "input", "mute"], "mute microphone", "mute my microphone", "unmute microphone", "unmute my microphone"),
    action("switch_audio", "Switch audio output", "audio", "Cycle to the next audio output", ["omarchy", "audio", "output", "switch"], "switch audio output", "switch speakers", "change audio output"),

    action("media_play_pause", "Play or pause", "media", "Toggle current media playback", ["omarchy-shell", "media", "playPause"], "play", "pause", "play pause", "pause music", "resume music"),
    action("media_next", "Next track", "media", "Skip to the next media item", ["omarchy-shell", "media", "next"], "next track", "next song", "skip song"),
    action("media_previous", "Previous track", "media", "Return to the previous media item", ["omarchy-shell", "media", "previous"], "previous track", "previous song", "go back a song"),

    action("brightness_up", "Brightness up", "display", "Raise focused display brightness", ["omarchy", "brightness", "display", "+5%"], "brightness up", "make the screen brighter", "increase brightness"),
    action("brightness_down", "Brightness down", "display", "Lower focused display brightness", ["omarchy", "brightness", "display", "5%-"], "brightness down", "dim the screen", "decrease brightness"),
    action("brightness_max", "Maximum brightness", "display", "Set focused display to full brightness", ["omarchy", "brightness", "display", "100%"], "maximum brightness", "full brightness", "brightness max"),
    action("brightness_min", "Minimum brightness", "display", "Set focused display to minimum brightness", ["omarchy", "brightness", "display", "1%"], "minimum brightness", "lowest brightness", "brightness min"),
    action("nightlight", "Toggle night light", "display", "Toggle warm screen temperature", ["omarchy", "toggle", "nightlight"], "toggle night light", "night light", "nightlight"),

    action("window_close", "Close window", "window", "Close the focused window", ["hyprctl", "dispatch", "hl.dsp.window.close()"], "close window", "close this window", "close the current window", confirm=True),
    action("window_fullscreen", "Toggle full screen", "window", "Toggle full screen for the focused window", ["hyprctl", "dispatch", 'hl.dsp.window.fullscreen({ mode = "fullscreen" })'], "full screen", "fullscreen", "toggle full screen"),
    action("window_float", "Toggle floating", "window", "Toggle floating or tiled state", ["hyprctl", "dispatch", 'hl.dsp.window.float({ action = "toggle" })'], "toggle floating", "float this window", "tile this window"),
    action("focus_left", "Focus left", "window", "Focus the window to the left", ["hyprctl", "dispatch", 'hl.dsp.focus({ direction = "l" })'], "focus left", "window left", "go left"),
    action("focus_right", "Focus right", "window", "Focus the window to the right", ["hyprctl", "dispatch", 'hl.dsp.focus({ direction = "r" })'], "focus right", "window right", "go right"),
    action("focus_up", "Focus up", "window", "Focus the window above", ["hyprctl", "dispatch", 'hl.dsp.focus({ direction = "u" })'], "focus up", "window above", "go up"),
    action("focus_down", "Focus down", "window", "Focus the window below", ["hyprctl", "dispatch", 'hl.dsp.focus({ direction = "d" })'], "focus down", "window below", "go down"),
    action("swap_left", "Move window left", "window", "Swap the focused window left", ["hyprctl", "dispatch", 'hl.dsp.window.swap({ direction = "l" })'], "move window left", "swap window left"),
    action("swap_right", "Move window right", "window", "Swap the focused window right", ["hyprctl", "dispatch", 'hl.dsp.window.swap({ direction = "r" })'], "move window right", "swap window right"),
    action("swap_up", "Move window up", "window", "Swap the focused window upward", ["hyprctl", "dispatch", 'hl.dsp.window.swap({ direction = "u" })'], "move window up", "swap window up"),
    action("swap_down", "Move window down", "window", "Swap the focused window downward", ["hyprctl", "dispatch", 'hl.dsp.window.swap({ direction = "d" })'], "move window down", "swap window down"),

    action("workspace_next", "Next workspace", "workspace", "Switch to the next workspace", ["hyprctl", "dispatch", 'hl.dsp.focus({ workspace = "e+1" })'], "next workspace", "workspace next"),
    action("workspace_previous", "Previous workspace", "workspace", "Switch to the previous workspace", ["hyprctl", "dispatch", 'hl.dsp.focus({ workspace = "e-1" })'], "previous workspace", "last workspace", "workspace previous"),

    action("open_terminal", "Open terminal", "shell", "Launch the default terminal", ["omarchy", "launch", "terminal"], "open terminal", "launch terminal", "new terminal", detached=True),
    action("open_browser", "Open browser", "shell", "Launch the default browser", ["omarchy", "launch", "browser"], "open browser", "launch browser", "new browser", detached=True),
    action("open_files", "Open files", "shell", "Launch the file manager", ["omarchy", "launch", "nautilus"], "open files", "open file manager", "launch file manager", "open the file manager", "open file explorer", "open the file explorer", "file explorer", detached=True),
    action("open_editor", "Open editor", "shell", "Launch the configured editor", ["omarchy", "launch", "editor", str(Path.home())], "open editor", "launch editor", detached=True),
    action("apps_menu", "Apps menu", "shell", "Open the Omarchy application menu", ["omarchy", "menu", "summon", "apps"], "open apps", "show apps", "apps menu", "open application drawer", "application drawer", "app drawer", "open app drawer"),
    action("system_menu", "System menu", "shell", "Open the Omarchy system menu", ["omarchy", "menu", "summon", "system"], "open system menu", "show system menu", "system menu"),
    action("clipboard", "Clipboard history", "shell", "Open clipboard history", ["omarchy", "menu", "clipboard"], "open clipboard", "clipboard history", "show clipboard"),
    action("emoji", "Emoji picker", "shell", "Open the emoji picker", ["omarchy", "menu", "emoji"], "open emoji picker", "emoji picker", "show emojis"),

    action("toggle_bar", "Toggle top bar", "session", "Show or hide the Omarchy bar", ["omarchy", "toggle", "bar"], "toggle bar", "hide the bar", "show the bar", "toggle top bar"),
    action("notifications", "Toggle do not disturb", "session", "Toggle notification silencing", ["omarchy", "toggle", "notification", "silencing"], "toggle do not disturb", "do not disturb", "silence notifications"),
    action("stay_awake", "Toggle stay awake", "session", "Toggle idle and automatic sleep behavior", ["omarchy", "toggle", "idle"], "toggle stay awake", "stay awake", "allow idle"),
    action("lock", "Lock system", "session", "Lock the desktop and turn off the display", ["omarchy", "system", "lock"], "lock system", "lock computer", "lock the screen", confirm=True),
    action("logout", "Log out", "session", "Close the graphical session", ["omarchy", "system", "logout"], "log out", "logout", confirm=True),
    action("reboot", "Restart computer", "session", "Reboot the computer", ["omarchy", "system", "reboot"], "restart computer", "reboot computer", "reboot", confirm=True),
    action("shutdown", "Shut down", "session", "Shut down the computer", ["omarchy", "system", "shutdown"], "shut down", "shutdown", "turn off the computer", confirm=True),
]

ACTIONS = {item.id: item for item in _ACTIONS}
GROUP_DESCRIPTIONS = {
    "audio": "speaker volume, mute, microphone, or audio output",
    "media": "music or video playback controls",
    "display": "screen brightness or night light",
    "window": "focused window position, focus, tiling, or closing",
    "workspace": "switching desktop workspaces",
    "shell": "opening applications or Omarchy menus",
    "session": "bar, notifications, idle, lock, logout, reboot, or shutdown",
    "none": "not a desktop-control request or no matching action",
}


# What a recognizer actually writes when this is said — collected from recordings of real
# speech (tools/asr_bench.py), not imagined.  Words that appear inside a longer sentence are
# left alone; only an utterance that is nothing but the mishearing is corrected, so "peace"
# in a dictated sentence stays "peace".
MISHEARD = {
    "and do": "undo", "i do": "undo", "en do": "undo",
    "pace": "paste", "peace": "paste", "haste": "paste",
    "select or": "select all", "select on": "select all", "select oil": "select all",
    "halfworth": "half width", "half worth": "half width", "half with": "half width",
    "scrolling down": "scroll down", "scrolling on": "scroll down", "scroll on": "scroll down",
    "full stream": "fullscreen", "fourth screen": "fullscreen", "for screen": "fullscreen",
    "rackspace": "workspace", "workspace 3": "workspace three",
}
# Mishearings that are safe to fix anywhere in a sentence: a spoken number, and the two words
# that are never right in a command otherwise.
MISHEARD_WORDS = {
    "work space": "workspace", "force workspace": "fourth workspace",
    "the force workspace": "the fourth workspace",
}


CORRECTIONS_PATH = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "omarchy-voice" / "corrections.json"
_LEARNED: dict[str, str] | None = None


def learned_corrections() -> dict[str, str]:
    """What this user accepted from `omarchy-voice learn`, read once."""
    global _LEARNED
    if _LEARNED is None:
        import learn

        _LEARNED = learn.load(CORRECTIONS_PATH)
    return _LEARNED


def normalize(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for prefix in ("please ", "could you ", "can you ", "would you "):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    if value.endswith(" please"):
        value = value[:-7]
    value = re.sub(r"\s+", " ", value).strip()
    for wrong, right in MISHEARD_WORDS.items():
        value = value.replace(wrong, right)
    return learned_corrections().get(value) or MISHEARD.get(value, value)


def dynamic_match(text: str) -> Action | None:
    match = re.fullmatch(r"(?:go to|switch to|open)? ?workspace (10|[1-9])", text)
    if match:
        number = match.group(1)
        return action(f"workspace_{number}", f"Workspace {number}", "workspace", f"Switch to workspace {number}", ["hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{number}" }})'])

    match = re.fullmatch(r"(?:move|send)(?: this| the current)? window to workspace (10|[1-9])", text)
    if match:
        number = match.group(1)
        return action(f"move_workspace_{number}", f"Move window to workspace {number}", "workspace", f"Move the focused window to workspace {number}", ["hyprctl", "dispatch", f'hl.dsp.window.move({{ workspace = "{number}" }})'])

    match = re.fullmatch(r"(?:set )?brightness(?: to)? (100|[1-9]?[0-9])(?: percent)?", text)
    if match:
        percent = max(1, min(100, int(match.group(1))))
        return action(f"brightness_{percent}", f"Brightness {percent}%", "display", f"Set focused display brightness to {percent}%", ["omarchy", "brightness", "display", f"{percent}%"])
    return None


def exact_match(text: str) -> Action | None:
    normalized = normalize(text)
    dynamic = dynamic_match(normalized)
    if dynamic:
        return dynamic
    for candidate in _ACTIONS:
        if normalized in candidate.aliases:
            return candidate
    return None


def public_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "group": item.group,
            "description": item.description,
            "confirm": item.confirm,
        }
        for item in _ACTIONS
    ]


DEFAULT_SETTINGS = {
    "yolo_mode": False,
    "look": "window",              # OCR the whole window under the mouse, or "mouse" for just look_region around it
    "look_region": [1100, 700],   # logical px read around the mouse when look = "mouse"
    "hud": True,                   # on-screen line with what was heard and done
    "speak": True,                 # spoken questions and errors (actions themselves stay silent)
    "confirm_risky_clicks_in_yolo": True,
    "jev_only": False,             # every command decided by Jev (for trying it out); mode switches stay local
    "transcribe_silence_s": 3.0,   # quick "transcribe" ends after this much silence ("start transcribe" never does)
    "keep_history": True,          # ~/.local/state/omarchy-voice/history.jsonl — what was heard and done, local only
    "default_agent": "",           # herdr agent kind for "new agent" ("" = the kind you run most)
    "fast_stt_model": "base.en",   # in-process Whisper for instant simple commands; "" to disable
    "ear": "auto",                 # the Rust ear owns the microphone when it is running (auto | on | off)
    "orb": True,                   # the glowing orb in the theme's accent colour while listening
    "autopilot_stage": 2,          # 1 look only · 2 may fill in, asks before committing · 3 also acts alone on trusted sites
    "autopilot_trusted_sites": [],  # stage 3 only, e.g. ["github.com"] — money/deletions/logins always ask
    "autopilot_max_steps": 14,     # a goal gives up after this many steps
}


def load_settings() -> dict:
    try:
        stored = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError, TypeError):
        stored = {}
    return {**DEFAULT_SETTINGS, **(stored if isinstance(stored, dict) else {})}


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS_PATH)


def load_yolo_mode() -> bool:
    return load_settings().get("yolo_mode") is True


def save_yolo_mode(enabled: bool) -> None:
    settings = load_settings()
    settings["yolo_mode"] = enabled
    save_settings(settings)


class State:
    def __init__(self, yolo_mode: bool = False) -> None:
        self.lock = threading.RLock()
        self.value: dict[str, object] = {
            "version": 1,
            "status": "starting",
            "listening": LISTENING_PATH.exists(),
            "model_ready": False,
            "transcript": "",
            "message": "Starting voice control",
            "pending_action": "",
            "pending_label": "",
            "confidence": 0.0,
            "source": "",
            "last_action": "",
            "last_action_label": "",
            "last_ok": True,
            "yolo_mode": yolo_mode,
            "jev_only": False,
            "mode": "",
            "voxtype": "",
            "close_panel": 0,
            "latency_ms": 0,
            "updated_at": time.time(),
        }
        self.write()

    def update(self, **changes: object) -> dict[str, object]:
        with self.lock:
            self.value.update(changes)
            self.value["updated_at"] = time.time()
            self.write()
            return dict(self.value)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return dict(self.value)

    def write(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.value, ensure_ascii=False, separators=(",", ":")) + "\n")
        tmp.replace(STATE_PATH)


class Tracer:
    """What the daemon heard, decided and did — one entry per utterance, for the panel."""

    def __init__(self, keep: int = 60) -> None:
        from collections import deque

        self.entries = deque(maxlen=keep)
        self.lock = threading.Lock()
        self.next_id = 1
        self.day = time.strftime("%Y-%m-%d")
        self.cost_today = 0.0
        self.jev_today = 0
        self.write()

    def record(self, entry: dict) -> None:
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.cost_today, self.jev_today = today, 0.0, 0
            entry.setdefault("at", time.time())
            last = self.entries[0] if self.entries else None
            if last and entry["heard"].startswith("(") and last.get("heard") == entry["heard"]:
                last["count"] = last.get("count", 1) + 1   # one row per voxtype session, not per sentence
                last["at"] = entry["at"]
                self.write()
                return
            entry["id"] = self.next_id
            self.next_id += 1
            self.cost_today += float(entry.get("cost") or 0)
            self.jev_today += 1 if entry.get("route") == "jev" else 0
            self.entries.appendleft(entry)
            self.write()
            self.append_history(entry)

    def append_history(self, entry: dict) -> None:
        if not load_settings().get("keep_history", True) or str(entry.get("heard", "")).startswith("("):
            return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            if HISTORY_PATH.exists() and HISTORY_PATH.stat().st_size > 20_000_000:   # keep the newest half
                lines = HISTORY_PATH.read_text().splitlines()
                HISTORY_PATH.write_text("\n".join(lines[len(lines) // 2:]) + "\n")
            with HISTORY_PATH.open("a") as f:
                row = {k: entry.get(k) for k in ("at", "heard", "fast", "route", "action", "ok", "message", "detail")}
                if TESTING_PATH.exists():
                    row["test"] = True
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def stats(self) -> dict:
        acted = [e for e in self.entries if e.get("route") not in ("ignored", "waiting")]
        latencies = sorted(e["ms"]["e2e"] for e in acted if e.get("ms", {}).get("e2e"))
        routes = {}
        for e in self.entries:
            routes[e.get("route", "?")] = routes.get(e.get("route", "?"), 0) + 1
        local = sum(1 for e in acted if e.get("route") in ("local", "fuzzy"))
        return {
            "commands": len(acted),
            "heard": len(self.entries),
            "median_ms": latencies[len(latencies) // 2] if latencies else 0,
            "p90_ms": latencies[int(len(latencies) * 0.9)] if latencies else 0,
            "local_pct": round(100 * local / len(acted)) if acted else 0,
            "failed": sum(1 for e in acted if e.get("ok") is False),
            "routes": routes,
            "jev_today": self.jev_today,
            "cost_today": round(self.cost_today, 5),
        }

    def write(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TRACE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": list(self.entries)[:25], "stats": self.stats()}, ensure_ascii=False) + "\n")
        tmp.replace(TRACE_PATH)


def route_of(decision) -> str:
    if decision.source == "voxtype" and decision.ignored:
        return "voxtype"
    if decision.ignored:
        return "ignored"
    src = decision.source or ""
    if src.startswith("fuzzy"):
        return "fuzzy"
    if src.startswith("jev"):
        return "jev" if decision.action else "unclear"
    return "local" if decision.action else "unclear"


class VoiceController:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.yolo = self.settings["yolo_mode"] is True
        self.state = State(self.yolo)
        self.state.update(jev_only=self.settings.get("jev_only") is True,
                          transcribe_silence_s=float(self.settings.get("transcribe_silence_s", 3.0)))
        self.lock = threading.RLock()
        self.handling = threading.Lock()   # one command at a time, from mic or socket
        self.pending = None                # brain.Action or catalog Action awaiting "confirm"
        self.autopilot = None              # the goal being worked on, while one is running
        self.autopilot_stop = threading.Event()
        self._orb_busy = False             # while true, the level stream leaves the orb alone
        self.orb_stream = OrbStream(ORB_SOCKET_PATH)
        self._orb_sent = (0.0, 0.0)        # (level, when): silence is not worth a message every 30 ms
        self.autopilot_reply: threading.Event | None = None
        self.autopilot_said_yes = False
        self.running = True
        self.speaker = None
        self.brain = None
        self.fast_stt = None
        # the Rust ear owns the microphone when it is running (settings: "ear": auto | off | on)
        wanted = str(self.settings.get("ear", "auto")).lower()
        self.use_ear = wanted == "on" or (wanted == "auto" and ear_client.available())
        self.partial: tuple[str, str, float] | None = None   # (accurate, quick, time) of an unfinished command
        self.tracer = Tracer()
        self.speaking = False                 # the user is in the middle of saying something
        self.last_speech_end = 0.0
        self.trace_ctx: dict = {}                            # speech timings for the utterance being handled

    def notify(self, message: str) -> None:
        subprocess.Popen(
            ["notify-send", "-u", "low", "Voice", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def say(self, message: str) -> None:
        if not self.speaker:
            return
        try:
            self.speaker.speak(message, wait=False)
        except Exception:
            pass

    def set_listening(self, enabled: bool) -> dict[str, object]:
        if enabled:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            LISTENING_PATH.touch()
        else:
            LISTENING_PATH.unlink(missing_ok=True)
            if self.speaker:
                self.speaker.stop()
        status = "listening" if enabled else "idle"
        message = "Listening" if enabled else "Voice control paused"
        self.notify(message)
        self.orb(self.orb_rest())
        return self.state.update(listening=enabled, status=status, message=message)

    def set_yolo(self, enabled: bool) -> dict[str, object]:
        with self.lock:
            self.yolo = enabled
            self.pending = None
        save_yolo_mode(enabled)
        message = "YOLO mode enabled — confirmations bypassed" if enabled else "YOLO mode disabled — confirmations required"
        self.notify(message)
        return self.state.update(
            yolo_mode=enabled,
            status="listening" if LISTENING_PATH.exists() else "idle",
            message=message,
            pending_action="",
            pending_label="",
        )

    def set_jev_only(self, enabled: bool) -> dict[str, object]:
        self.settings["jev_only"] = enabled
        stored = load_settings()
        stored["jev_only"] = enabled
        save_settings(stored)
        message = "Jev only — every command goes to Jev" if enabled else "Local + Jev — common commands decided on this machine"
        if self.brain:
            self.brain.hud(message, tone="busy", ms=2500)
        return self.state.update(jev_only=enabled, message=message)

    def set_pending(self, selected, confidence: float, source: str) -> dict[str, object]:
        with self.lock:
            self.pending = selected
        message = f"Confirm: {selected.label}"
        self.notify(message)
        self.say(f"Confirm {selected.label}, or say cancel.")
        return self.state.update(
            status="confirming",
            message=message,
            pending_action=getattr(selected, "id", None) or selected.kind,
            pending_label=selected.label,
            confidence=confidence,
            source=source,
        )

    def cancel_pending(self) -> dict[str, object]:
        with self.lock:
            self.pending = None
        return self.state.update(
            status="listening" if LISTENING_PATH.exists() else "idle",
            message="Cancelled",
            pending_action="",
            pending_label="",
        )

    def execute(self, selected: Action, source: str) -> dict[str, object]:
        self.state.update(status="acting", message=selected.label, source=source)
        try:
            if selected.detached:
                subprocess.Popen(
                    selected.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                ok = True
                message = selected.label
            else:
                result = subprocess.run(selected.argv, capture_output=True, text=True, timeout=20)
                ok = result.returncode == 0
                detail = (result.stderr or result.stdout).strip().splitlines()
                message = selected.label if ok else (detail[-1] if detail else f"{selected.label} failed")
        except Exception as exc:
            ok = False
            message = f"{selected.label} failed: {exc}"

        with self.lock:
            self.pending = None
        if not ok:
            self.notify(message)
            self.say("That did not work.")
        return self.state.update(
            status="listening" if LISTENING_PATH.exists() else "idle",
            message=message,
            pending_action="",
            pending_label="",
            last_action=selected.id,
            last_action_label=selected.label,
            last_ok=ok,
        )

    def preview(self, transcript: str) -> dict[str, object]:
        """What would happen for this sentence — decided, never executed."""
        if self.brain is None:
            selected = exact_match(transcript)
            return {"ok": bool(selected), "label": selected.label if selected else "", "source": "catalog"}
        decision = self.brain.decide(transcript)
        action = decision.action
        return {"ok": action is not None, "label": action.label if action else (decision.say or decision.hud or "ignored"),
                "kind": action.kind if action else "", "source": decision.source, "confirm": bool(action and action.confirm),
                "ms": decision.ms.get("total")}

    def execute_any(self, selected, source: str, said: str = "") -> dict[str, object]:
        """Run a catalog Action (panel/legacy) or a brain Action."""
        if isinstance(selected, Action):
            return self.execute(selected, source)
        self.state.update(status="acting", message=selected.label, source=source)
        ok, message = self.brain.execute(selected, said)
        with self.lock:
            self.pending = None
        if not ok:
            self.brain.hud(f"✗ {selected.label}", message[:90], tone="warn")
            self.say("That did not work.")
        return self.state.update(
            status="listening" if LISTENING_PATH.exists() else "idle",
            message=selected.label if ok else message, pending_action="", pending_label="",
            last_action=selected.kind, last_action_label=selected.label, last_ok=ok, mode=self.brain.mode,
            transcribe_kind=self.brain.transcribe_kind,
        )

    def handle_transcript(self, transcript: str, decision=None, alternatives: tuple[str, ...] = ()) -> dict[str, object]:
        with self.handling:
            try:
                return self._handle_transcript(transcript, decision, alternatives)
            finally:
                threading.Timer(0.5, lambda: self.orb(self.orb_rest())).start()

    def _handle_transcript(self, transcript: str, decision=None, alternatives: tuple[str, ...] = ()) -> dict[str, object]:
        spoken = normalize(transcript)
        self.state.update(transcript=transcript, status="deciding", message="Understanding command")
        if self.orb_rest() != "transcribe":
            self.orb("thinking")
        if spoken in ENABLE_YOLO_WORDS:
            return self.set_yolo(True)
        if spoken in DISABLE_YOLO_WORDS:
            return self.set_yolo(False)
        if spoken in TOGGLE_YOLO_WORDS:
            return self.set_yolo(not self.yolo)
        if spoken in JEV_ONLY_WORDS:
            return self.set_jev_only(not self.settings.get("jev_only"))

        handled = self.autopilot_heard(spoken)
        if handled is not None:
            return handled

        with self.lock:
            pending = self.pending
        if pending:
            if spoken in CONFIRM_WORDS or spoken in ("yes", "yeah", "yep", "sure", "confirmed"):
                return self.execute_any(pending, "confirmation", transcript)
            if spoken in CANCEL_WORDS or spoken in ("no", "nope"):
                if self.brain:
                    self.brain.hud("Cancelled", tone="warn", ms=1200)
                return self.cancel_pending()
            self.cancel_pending()

        if spoken in STOP_WORDS:
            return self.set_listening(False)

        if self.brain is not None:
            return self.handle_with_brain(transcript, decision, alternatives)

        selected = exact_match(spoken)
        if selected:
            if selected.confirm and not self.yolo:
                return self.set_pending(selected, 1.0, "exact")
            return self.execute(selected, "exact")

        return self.state.update(status="error", message="Voice control is not ready — see: journalctl --user -u omarchy-voice")

    def handle_with_brain(self, transcript: str, decision=None, alternatives: tuple[str, ...] = ()) -> dict[str, object]:
        if decision is None:
            decision = self.brain.decide(transcript, "", alternatives)
        ctx, self.trace_ctx = self.trace_ctx, {}
        state = self._act_on(transcript, decision)
        ms = {"stt_fast": ctx.get("stt_fast_ms"), "stt": ctx.get("stt_ms")}
        ms.update({k.removesuffix("_ms"): v for k, v in decision.ms.items() if isinstance(v, (int, float))})
        ms["decide"] = ms.pop("total", None)
        ms["exec"] = state.get("_exec_ms") if isinstance(state, dict) else None
        if ctx.get("t0"):
            ms["e2e"] = round((time.perf_counter() - ctx["t0"]) * 1000)
        elif decision.action:
            ms["e2e"] = round((ms.get("decide") or 0) + (ms.get("exec") or 0))
        action = decision.action
        self.tracer.record({
            "heard": transcript, "fast": ctx.get("fast") or "",
            "route": route_of(decision) if not (action and action.confirm and self.pending is action) else "confirm",
            "detail": decision.source, "scene": decision.scene,
            "action": action.label if action else "", "kind": action.kind if action else "",
            "ok": (state.get("last_ok") if action and self.pending is not action else None) if isinstance(state, dict) else None,
            "message": (state.get("message") if isinstance(state, dict) else "") or decision.say or decision.hud,
            "ms": {k: v for k, v in ms.items() if v is not None}, "cost": round(decision.cost, 6),
        })
        return state

    def _act_on(self, transcript: str, decision) -> dict[str, object]:
        idle = "listening" if LISTENING_PATH.exists() else "idle"
        timing = " · ".join(f"{k.removesuffix('_ms')} {v}" for k, v in decision.ms.items() if isinstance(v, (int, float)) and k != "total")
        sub = f"{decision.source} · {decision.ms.get('total', 0)} ms" + (f" ({timing})" if timing else "")
        print(f"[omarchy-voice] {transcript!r} -> {decision.action.kind + ': ' + decision.action.label if decision.action else ('ignored' if decision.ignored else decision.say)} | {sub}", flush=True)
        if decision.ignored:
            self.orb(self.orb_rest())
            if decision.hud:
                self.brain.hud(decision.hud, tone="busy", ms=1200)
            return self.state.update(status=idle, message="Not a command", source=decision.source, latency_ms=decision.ms.get("total", 0))
        action = decision.action
        if action is None:
            self.brain.hud(f"“{transcript[:60]}”", decision.hud or decision.say, tone="warn")
            if decision.say:
                self.say(decision.say)
            return self.state.update(status=idle, message=decision.say or decision.hud or "Command not understood",
                                     source=decision.source, latency_ms=decision.ms.get("total", 0))
        if action.kind == "autopilot":
            return self.start_autopilot(action, decision)
        if action.kind == "confirm":
            with self.lock:
                pending = self.pending
            return self.execute_any(pending, "confirmation", transcript) if pending else self.state.update(message="Nothing to confirm")
        if action.kind == "cancel":
            return self.cancel_pending()
        risky_click = action.kind == "click" and action.confirm and self.settings.get("confirm_risky_clicks_in_yolo", True)
        if action.confirm and (not self.yolo or risky_click):
            self.brain.hud(f"Confirm: {action.label}?", "say confirm or cancel", tone="warn", ms=8000)
            return self.set_pending(action, 1.0, decision.source)
        self.brain.hud(f"“{transcript[:60]}”", f"{action.label} · {sub}")
        self.orb("acting")
        if decision.say:
            self.say(decision.say)
        t_exec = time.perf_counter()
        state = self.execute_any(action, decision.source, transcript)
        exec_ms = round((time.perf_counter() - t_exec) * 1000)
        state = self.state.update(latency_ms=decision.ms.get("total", 0), mode=self.brain.mode)
        return {**state, "_exec_ms": exec_ms}

    # -- working towards a goal in the browser --------------------------------
    def start_autopilot(self, action, decision) -> dict[str, object]:
        import autopilot as autopilot_mod

        if self.autopilot:
            self.brain.hud("Already working on a goal — say stop", tone="warn", ms=2500)
            return self.state.update(message="A goal is already running")
        if not self.brain.browser.available():
            message = "Open a browser with remote debugging first (see the README)"
            self.brain.hud(message, tone="warn", ms=4000)
            self.say("I need a browser I can read.")
            return self.state.update(status="listening" if LISTENING_PATH.exists() else "idle", message=message, last_ok=False)

        goal = str(action.args.get("goal", "")).strip()
        stage = int(self.settings.get("autopilot_stage", 2))
        trusted = tuple(self.settings.get("autopilot_trusted_sites") or ())
        self.autopilot_stop.clear()

        def ask(label: str, reason: str) -> bool:
            """Put a committing step to the user and wait for an answer (no answer = no)."""
            reply = threading.Event()
            self.autopilot_reply, self.autopilot_said_yes = reply, False
            self.state.update(status="confirming", pending_label=label,
                              message=f"Confirm: {label}" + (f" — {reason}" if reason else ""))
            self.brain.hud(f"Confirm: {label}?", reason or "say confirm or stop", tone="warn", ms=60000)
            self.say(f"{reason}. Confirm {label}, or say stop." if reason else f"Confirm {label}, or say stop.")
            answered = reply.wait(90)
            self.autopilot_reply = None
            self.state.update(status="acting", pending_label="")
            return bool(answered and self.autopilot_said_yes)

        def on_step(step, run) -> None:
            self.state.update(status="acting", message=f"{run.goal[:40]} · {step.n}. {step.label}",
                              autopilot_step=step.n, autopilot_goal=run.goal[:80], autopilot_label=step.label)
            self.brain.hud(f"Goal: {run.goal[:50]}", f"{step.n}. {step.label}", ms=6000)
            self.tracer.record({"heard": f"(step {step.n})", "route": "autopilot", "detail": step.kind,
                                "action": step.label, "ok": step.ok,
                                "message": step.outcome or (autopilot_mod.REASONS.get(step.reason, "") if step.asked else ""),
                                "ms": {"e2e": step.ms} if step.ms else {}, "cost": round(step.cost, 6)})

        pilot = autopilot_mod.Autopilot(
            self.brain.browser, stage=stage, trusted_hosts=trusted, ask=ask, on_step=on_step,
            stop=self.autopilot_stop.is_set, max_steps=int(self.settings.get("autopilot_max_steps", 14)))

        def work() -> None:
            try:
                run = pilot.run(goal)
            except Exception as exc:
                print(f"[omarchy-voice] autopilot: {exc}", file=sys.stderr, flush=True)
                run = autopilot_mod.Run(goal=goal, status="error", answer=str(exc)[:120])
            self.autopilot = None
            spoken = run.answer if run.status == "done" and run.answer else run.summary
            self.brain.hud(f"{run.summary}: {run.goal[:40]}", run.answer[:90], tone="ok" if run.status == "done" else "warn", ms=8000)
            self.say(spoken[:300])
            self.tracer.record({"heard": goal, "route": "autopilot", "detail": run.status,
                                "action": run.summary, "ok": run.status == "done",
                                "message": run.answer[:160], "ms": {}, "cost": round(run.cost, 6)})
            self.state.update(status="listening" if LISTENING_PATH.exists() else "idle",
                              message=f"{run.summary} — {run.answer[:80]}" if run.answer else run.summary,
                              autopilot_step=0, autopilot_goal="", autopilot_label="",
                              last_action_label=f"Goal: {goal[:40]}", last_ok=run.status == "done")

        self.autopilot = goal
        threading.Thread(target=work, name="autopilot", daemon=True).start()
        self.brain.hud(f"Goal: {goal[:50]}", "working — say stop to end it", ms=6000)
        return self.state.update(status="acting", message=f"Working on: {goal[:60]}",
                                 autopilot_goal=goal[:80], last_action_label=f"Goal: {goal[:40]}")

    def autopilot_heard(self, spoken: str) -> dict[str, object] | None:
        """While a goal runs, only stopping and answering a confirmation mean anything."""
        if not self.autopilot:
            return None
        waiting = self.autopilot_reply
        if spoken in STOP_WORDS or spoken in CANCEL_WORDS or spoken in ("stop", "stop it", "stop autopilot",
                                                                       "stop the goal", "abort", "no", "nope"):
            self.autopilot_stop.set()
            if waiting:
                self.autopilot_said_yes = False
                waiting.set()
            self.brain.hud("Stopping the goal", tone="warn", ms=2000)
            return self.state.update(message="Stopping the goal")
        if waiting and (spoken in CONFIRM_WORDS or spoken in ("yes", "yeah", "yep", "sure", "confirmed", "do it")):
            self.autopilot_said_yes = True
            waiting.set()
            return self.state.update(message="Confirmed")
        self.brain.hud("Working on the goal — say stop to end it", tone="busy", ms=2000)
        return self.state.update(message="Working on a goal")

    def handle_request(self, request: dict[str, object]) -> dict[str, object]:
        operation = str(request.get("op", "status"))
        if operation == "status":
            return {"ok": True, "state": self.state.snapshot()}
        if operation == "catalog":
            return {"ok": True, "actions": public_catalog()}
        if operation == "toggle":
            state = self.set_listening(not LISTENING_PATH.exists())
            return {"ok": True, "state": state}
        if operation == "listen":
            state = self.set_listening(bool(request.get("enabled")))
            return {"ok": True, "state": state}
        if operation == "yolo":
            mode = str(request.get("mode", "toggle"))
            if mode == "status":
                return {"ok": True, "state": self.state.snapshot()}
            enabled = not self.yolo if mode == "toggle" else mode == "on"
            return {"ok": True, "state": self.set_yolo(enabled)}
        if operation == "transcribe_silence":
            steps = (0.5, 1.0, 2.0, 3.0, 5.0)
            current = float(self.settings.get("transcribe_silence_s", 3.0))
            value = request.get("seconds")
            nxt = float(value) if value is not None else next((s for s in steps if s > current), steps[0])
            return {"ok": True, "state": self.set_transcribe_silence(nxt)}
        if operation == "jev_only":
            mode = str(request.get("mode", "toggle"))
            enabled = (not self.settings.get("jev_only")) if mode == "toggle" else mode == "on"
            return {"ok": True, "state": self.set_jev_only(enabled)}
        if operation == "cancel":
            return {"ok": True, "state": self.cancel_pending()}
        if operation == "confirm":
            with self.lock:
                pending = self.pending
            if not pending:
                return {"ok": False, "error": "no action is awaiting confirmation"}
            return {"ok": True, "state": self.execute(pending, "panel-confirmation")}
        if operation == "action":
            selected = ACTIONS.get(str(request.get("id", "")))
            if not selected:
                return {"ok": False, "error": "unknown action"}
            if selected.confirm and not self.yolo:
                return {"ok": True, "state": self.set_pending(selected, 1.0, "panel")}
            return {"ok": True, "state": self.execute(selected, "panel")}
        if operation == "preview":
            return self.preview(str(request.get("text", "")))
        if operation == "speak":
            return {"ok": True, "state": self.handle_transcript(str(request.get("text", "")))}
        if operation in ("hints", "grid", "clear", "dictation") and self.brain:
            from brain import Action as BrainAction

            action = {
                "hints": BrainAction("hints", "Show hints", repeatable=False),
                "grid": BrainAction("grid", "Mouse grid", {"screen": bool(request.get("screen"))}, repeatable=False),
                "clear": BrainAction("clear", "Hide overlay", repeatable=False),
                "dictation": BrainAction("mode", "Dictation", {"mode": "" if self.brain.mode == "dictation" else "dictation"}, repeatable=False),
            }[operation]
            with self.handling:
                return {"ok": True, "state": self.execute_any(action, "panel")}
        return {"ok": False, "error": f"unknown operation: {operation}"}

    def load_models(self) -> None:
        self.state.update(status="loading", message="Starting voice control")
        from voiceio import Speaker

        try:
            self.speaker = Speaker()
        except Exception as exc:
            print(f"[omarchy-voice] TTS unavailable: {exc}", file=sys.stderr, flush=True)
        try:
            from brain import Brain
            from browser import BrowserBridge

            self.brain = Brain(ACTIONS, exact_match, normalize, self.settings, BrowserBridge(), self.speaker)
            self.brain.before_typing = self.close_panel
            threading.Thread(target=self.warm_up, name="warm-up", daemon=True).start()
            threading.Thread(target=self.watch_voxtype, name="voxtype-watch", daemon=True).start()
        except Exception as exc:
            print(f"[omarchy-voice] engine failed to start: {exc}", file=sys.stderr, flush=True)
            self.brain = None
            self.state.update(status="error", message=f"Engine failed to start: {exc}"[:160])
            return
        self.state.update(
            model_ready=True,
            status="listening" if LISTENING_PATH.exists() else "idle",
            message="Listening" if LISTENING_PATH.exists() else "Voice control ready",
        )

    def close_panel(self) -> None:
        """An open bar popup holds the keyboard: typed text would vanish into it. Ask it to close."""
        seq = int(self.state.snapshot().get("close_panel", 0)) + 1
        self.state.update(close_panel=seq)
        time.sleep(0.25)

    def quick_transcribe_check(self) -> None:
        """Quick transcription ends by itself once the user has been silent long enough."""
        if self.speaking or not LISTENING_PATH.exists():
            return
        limit = float(self.settings.get("transcribe_silence_s", 3.0))
        quiet_since = max(self.last_speech_end, self.brain.transcribe_started)
        nothing_yet = self.last_speech_end < self.brain.transcribe_started
        if nothing_yet:
            limit = max(limit, 6.0)   # time to start talking after "transcribe"
        if time.monotonic() - quiet_since < limit:
            return
        from brain import Action as BrainAction

        op = "cancel" if nothing_yet else "stop"
        label = "Nothing to transcribe" if nothing_yet else f"Transcribed ({limit:g} s of silence)"
        with self.handling:
            if self.brain.mode != "transcribe":
                return
            state = self.execute_any(BrainAction("voxtype", label, {"op": op}, repeatable=False), "silence")
        self.brain.hud(("✓ " if op == "stop" else "") + label, tone="ok" if op == "stop" else "busy", ms=2000)
        self.tracer.record({"heard": f"({limit:g} s of silence)", "route": "local", "detail": "quick transcription auto-stop",
                            "action": label, "ok": state.get("last_ok") if isinstance(state, dict) else None,
                            "message": "", "ms": {}})

    def set_transcribe_silence(self, seconds: float) -> dict[str, object]:
        seconds = max(0.5, min(30.0, float(seconds)))
        self.settings["transcribe_silence_s"] = seconds
        stored = load_settings()
        stored["transcribe_silence_s"] = seconds
        save_settings(stored)
        return self.state.update(transcribe_silence_s=seconds, message=f"Quick transcription stops after {seconds:g} s of silence")

    def warm_up(self) -> None:
        """Load OCR engines, open the CDP connection and the HTTP/2 session before the first command."""
        import jev
        import perceive

        try:
            if self.settings.get("fast_stt_model") and not self.use_ear:
                # with the ear running, the fast transcript comes from it: a second Whisper
                # in this process would cost a gigabyte to duplicate work
                self.fast_stt = livemic.FastWhisper(self.settings["fast_stt_model"])
            perceive.ocr_engine()
            import uinput

            uinput.pointer()   # a new input device needs a moment before Hyprland routes its clicks
            import keyboard

            keyboard.keyboard()
            self.brain.overlay.send(op="clear")
            self.brain.perceiver.capture()
            jev.decide({"transcript": "warm up"}, {"ok": {"type": "noul", "instructions": {"question": "Is this a warm-up?"}}})
        except Exception as exc:
            import traceback

            print(f"[omarchy-voice] warm-up: {exc}", file=sys.stderr, flush=True)
            traceback.print_exc()

    def orb_rest(self) -> str:
        """What the orb shows when nothing is happening right now."""
        if not LISTENING_PATH.exists():
            return "off"
        if self.brain is not None and (self.brain.mode == "transcribe" or voxtype_state() == "recording"):
            return "transcribe"
        return "listening"

    def orb(self, state: str, level: float = 0.0) -> None:
        # thinking and acting own the orb until the next rest, so the level stream cannot overwrite them
        self._orb_busy = state in ("thinking", "acting")
        if self.settings.get("orb", True):
            self.orb_stream.publish(state, level)

    def on_level(self, level: float) -> None:
        """Every 30 ms block while listening: the orb must move before the VAD has decided."""
        if self._orb_busy:
            return                       # thinking / acting own the orb until they are done
        rest = self.orb_rest()
        if rest == "off":
            return
        last, when = self._orb_sent
        now = time.monotonic()
        if abs(level - last) < 0.01 and now - when < 0.1:
            return                       # nothing changed worth drawing
        self._orb_sent = (level, now)
        if rest == "transcribe":
            self.orb("transcribe", level)
        elif self.speaking or level > 0.02:
            self.orb("hearing", level)
        else:
            self.orb("listening", level)

    def on_speech_end(self) -> None:
        self.speaking = False
        self.last_speech_end = time.monotonic()
        self.orb(self.orb_rest())

    def on_speech(self) -> None:
        self.speaking = True
        self.orb("hearing", 0.4)
        if self.brain is not None and self.brain.mode not in ("dictation", "transcribe") and voxtype_state() != "recording":
            self.brain.prefetch()

    def watch_voxtype(self) -> None:
        """Keep the "transcribe" mode in sync with voxtype (F9, the 10-minute cap, a crash)."""
        last = ""
        while self.running:
            state = voxtype_state()
            if self.brain is not None:
                if (self.brain.mode == "transcribe" and state not in ("recording", "transcribing")
                        and time.monotonic() - self.brain.transcribe_started > 2.0):
                    self.brain.mode = ""
                    self.state.update(mode="", message="Transcription finished")
                elif self.brain.mode == "transcribe" and self.brain.transcribe_kind == "quick" and state == "recording":
                    self.quick_transcribe_check()
                if state != last:
                    self.state.update(voxtype=state)
            last = state
            quick = self.brain is not None and self.brain.mode == "transcribe" and self.brain.transcribe_kind == "quick"
            time.sleep(0.1 if quick else 0.4)   # a quick session's silence limit can be as short as 0.5 s

    def run_audio(self) -> None:
        """Mic → utterance queue (recorder thread) → transcribe and act (this thread).

        The microphone stays open while a command is being handled, so a quick
        "off" right after "transcribe" is queued instead of lost.
        """
        import queue as queue_mod

        utterances: queue_mod.Queue = queue_mod.Queue()

        def record() -> None:
            vad = None
            failures = 0
            while self.running:
                if not LISTENING_PATH.exists():
                    time.sleep(0.25)
                    continue
                try:
                    vad = vad or livemic.LiveVAD()
                    if failures:
                        print("[omarchy-voice] microphone: reconnected", flush=True)
                        self.state.update(status="listening", message="Microphone reconnected", last_ok=True)
                        failures = 0
                    for audio in vad.utterances(lambda: self.running and LISTENING_PATH.exists(), on_speech=self.on_speech,
                                                on_end=self.on_speech_end, on_level=self.on_level):
                        utterances.put((audio, voxtype_state(), None, None))
                except Exception as exc:
                    failures += 1
                    if failures in (1, 5) or failures % 30 == 0:   # don't flood the journal while it is gone
                        print(f"[omarchy-voice] microphone: {exc} (retrying)", file=sys.stderr, flush=True)
                    self.state.update(status="error", message="Microphone unavailable — reconnecting", last_ok=False)
                    vad = None
                    livemic.reset_audio()          # the device list changed: read it again
                    time.sleep(min(5.0, 0.5 * failures))

        def listen_to_ear() -> None:
            """The Rust ear owns the microphone: take its levels, speech marks and transcripts."""
            client = ear_client.Ear()
            while self.running:
                for event in client.events(lambda: self.running and self.use_ear):
                    kind = event.get("t")
                    if kind == "level":
                        self.on_level(float(event.get("v", 0.0)))
                    elif kind == "speech":
                        self.on_speech()
                    elif kind == "idle":
                        self.on_speech_end()
                    elif kind == "utterance":
                        self.on_speech_end()
                        text = str(event.get("text", "")).strip()
                        if text:
                            utterances.put((None, voxtype_state(), text, event.get("wav")))
                time.sleep(0.5)

        if self.use_ear:
            print("[omarchy-voice] listening through the ear (Rust)", flush=True)
            threading.Thread(target=listen_to_ear, name="ear", daemon=True).start()
        else:
            threading.Thread(target=record, name="mic", daemon=True).start()
        while self.running:
            if not LISTENING_PATH.exists():
                time.sleep(0.25)
                continue
            if self.state.snapshot().get("status") not in ("listening", "confirming"):
                self.state.update(status="listening", listening=True, message="Listening")
            try:
                audio, vox, quick, wav = utterances.get(timeout=0.3)
            except queue_mod.Empty:
                continue
            if self.brain is not None and self.brain.mode != "transcribe" and vox in ("recording", "transcribing"):
                # the user is dictating with voxtype (F9): those words are not commands, don't even transcribe
                self.tracer.record({"heard": "(dictating with voxtype)", "route": "voxtype", "detail": "voice control steps aside while voxtype records",
                                    "action": "", "ok": None, "message": "", "ms": {}})
                continue
            self.state.update(status="transcribing", message="Transcribing speech")
            try:
                self.transcribe_and_handle(audio, quick=quick, wav=wav)
            except Exception as exc:
                self.state.update(status="error", message=str(exc), last_ok=False)
                self.notify(f"Voice command failed: {exc}")
            if self.state.snapshot().get("status") in ("transcribing", "deciding", "acting"):
                self.state.update(status="listening" if LISTENING_PATH.exists() else "idle")

    def transcribe_and_handle(self, audio=None, quick: str | None = None, wav: str | None = None) -> None:
        """Two recognizers race: a small in-process Whisper and voxtype's large one.

        - small transcript is a simple command (fast path)  -> act on it now (~0.2 s)
        - otherwise Jev decides on the small transcript while voxtype finishes;
          if voxtype agrees, that decision is used as is, else it is recomputed.
        """
        from concurrent.futures import ThreadPoolExecutor

        t0 = time.perf_counter()
        pool = ThreadPoolExecutor(2)
        path = wav or str(livemic.save_wav(audio, RUNTIME_DIR / "utterance.wav"))
        accurate = pool.submit(lambda: livemic.transcribe(path))
        if quick is None and self.fast_stt is not None and self.brain is not None:
            try:
                quick = self.fast_stt.transcribe(audio)
            except Exception as exc:
                print(f"[omarchy-voice] fast stt failed: {exc}", file=sys.stderr, flush=True)
        fast_ms = round((time.perf_counter() - t0) * 1000)
        self.trace_ctx = {"t0": t0, "fast": quick or "", "stt_fast_ms": fast_ms}
        partial = self.partial if self.partial and time.monotonic() - self.partial[2] < PARTIAL_TTL else None
        self.partial = None
        if partial and quick:
            quick = f"{partial[1] or partial[0]} {quick}"
        speculative = None
        dictating = self.brain is not None and self.brain.mode in ("dictation", "transcribe")
        if quick and not dictating:
            spoken = normalize(quick)
            with self.lock:
                pending = self.pending
            instant = None
            if not pending and spoken not in STOP_WORDS and not INCOMPLETE.fullmatch(spoken):
                instant = self.brain.fast_path(spoken, quick) or self.brain.fuzzy_command([spoken], threshold=92)
                if not self.brain.allows_local(instant):
                    instant = None
            if instant is not None and instant.action is not None:
                print(f"[omarchy-voice] stt fast {fast_ms} ms: {quick!r} (acting now, {instant.source})", flush=True)
                pool.shutdown(wait=False)
                self.handle_transcript(quick, instant)
                return
            if not pending and not INCOMPLETE.fullmatch(spoken):
                speculative = pool.submit(self.brain.decide, quick, "")
        elif quick and self.brain.mode == "transcribe" and getattr(self.brain.fast_path(normalize(quick), quick), "action", None) is not None:
            # the small model already heard "transcribe off": stop voxtype now, not 0.4 s later
            pool.shutdown(wait=False)
            self.handle_transcript(quick)
            return
        elif quick and dictating and normalize(quick) in ("stop dictation", "end dictation", "dictation off", "new line", "scratch that"):
            pool.shutdown(wait=False)
            self.handle_transcript(quick)
            return
        transcript = accurate.result()
        self.trace_ctx["stt_ms"] = round((time.perf_counter() - t0) * 1000)
        print(f"[omarchy-voice] stt fast {fast_ms} ms: {quick!r} | accurate {round((time.perf_counter() - t0) * 1000)} ms: {transcript!r}", flush=True)
        pool.shutdown(wait=False)
        if not transcript:
            self.trace_ctx = {}
            return
        if partial:
            transcript = f"{partial[0]} {transcript}"
        if not dictating and INCOMPLETE.fullmatch(normalize(transcript)):
            # "search for" … pause … "flights to Zürich": hold the first half
            self.partial = (transcript.rstrip(".!?,… "), (quick or "").rstrip(".!?,… "), time.monotonic())
            self.tracer.record({"heard": transcript, "fast": quick or "", "route": "waiting", "detail": "unfinished — waiting for the rest",
                                "action": "", "ok": None, "message": "", "ms": {"stt": self.trace_ctx.get("stt_ms")}})
            self.trace_ctx = {}
            if self.brain:
                self.brain.hud(f"“{transcript.rstrip('.!?, ')} …”", "go on", tone="busy", ms=int(PARTIAL_TTL * 1000))
            self.state.update(message=f"{transcript} …", transcript=transcript)
            return
        decision = None
        if speculative is not None and normalize(transcript) == normalize(quick or ""):
            decision = speculative.result()
            decision.source += "+spec"
        alternatives = (quick,) if quick and normalize(quick) != normalize(transcript) else ()
        self.handle_transcript(transcript, decision, alternatives)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline().decode("utf-8"))
            response = self.server.controller.handle_request(request)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, controller: VoiceController):
        SOCKET_PATH.unlink(missing_ok=True)
        super().__init__(str(SOCKET_PATH), ControlHandler)
        self.controller = controller


def run_daemon() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import tesserocr  # noqa: F401 — cysignals installs handlers and must initialise on the main thread
    except ImportError:
        pass
    controller = VoiceController()
    server = ControlServer(controller)
    server_thread = threading.Thread(target=server.serve_forever, name="voice-control-socket", daemon=True)
    server_thread.start()

    def stop(_signum=None, _frame=None) -> None:
        # keep the listening flag: a restart of the service should come back listening if it was
        controller.running = False
        # a transcription or Jev call still in flight must not hold the process for systemd's 90 s
        safety = threading.Timer(5.0, lambda: os._exit(0))
        safety.daemon = True
        safety.start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        controller.orb_stream.start()
        controller.load_models()
        controller.orb(controller.orb_rest())    # listening was on before a restart
        controller.run_audio()
    finally:
        controller.orb("off")
        controller.orb_stream.stop()
        server.shutdown()
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)
        controller.state.update(status="stopped", message="Voice control stopped")
    return 0


def ensure_daemon(timeout: float = 5.0) -> None:
    if SOCKET_PATH.exists():
        return
    started = subprocess.run(["systemctl", "--user", "start", "omarchy-voice.service"],
                             capture_output=True, text=True)
    if started.returncode != 0:
        if "not found" in started.stderr:
            raise RuntimeError("Omarchy Voice isn't set up yet — run its install.sh "
                               "(~/.config/omarchy/plugins/io.github.samyn92.omarchy-voice/install.sh)")
        raise RuntimeError("Omarchy Voice failed to start — see: journalctl --user -u omarchy-voice")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if SOCKET_PATH.exists():
            return
        time.sleep(0.05)
    raise RuntimeError("Omarchy Voice did not start in time — see: journalctl --user -u omarchy-voice")


def request(payload: dict[str, object], start: bool = True) -> dict[str, object]:
    if start:
        ensure_daemon()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(30)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.extend(chunk)
    finally:
        client.close()
    if not chunks:
        raise RuntimeError("voice-control daemon returned no response")
    return json.loads(chunks.decode("utf-8"))


def store_key() -> int:
    import getpass

    from brain import ENV_FILE

    key = os.environ.get("OPENROUTER_API_KEY") or getpass.getpass("OpenRouter API key (https://openrouter.ai/settings/keys): ").strip()
    if not key:
        print("no key entered")
        return 1
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.touch(mode=0o600, exist_ok=True)
    ENV_FILE.chmod(0o600)
    ENV_FILE.write_text(f"OPENROUTER_API_KEY={key}\n")
    print(f"saved to {ENV_FILE}")
    if SOCKET_PATH.exists():
        subprocess.run(["systemctl", "--user", "restart", "omarchy-voice.service"])
    return 0


def misses(hours: float = 24.0, show_all: bool = False, replay: bool = False) -> int:
    """What was said but not done — the things worth teaching voice control next."""
    import collections

    cutoff = time.time() - hours * 3600
    rows = []
    try:
        for line in HISTORY_PATH.read_text().splitlines():
            e = json.loads(line)
            if e.get("at", 0) >= cutoff and not e.get("test") and e.get("route") != "voxtype":
                rows.append(e)
    except (OSError, ValueError):
        print(f"no history yet ({HISTORY_PATH})")
        return 1
    failed = [e for e in rows if e.get("route") in ("unclear",) or e.get("ok") is False
              or (e.get("route") == "ignored" and (show_all or len(str(e.get("heard", "")).split()) <= 6))]
    groups = collections.OrderedDict()
    for e in sorted(failed, key=lambda e: -e["at"]):
        key = normalize(str(e.get("heard", "")))
        g = groups.setdefault(key, {"n": 0, "last": e, "said": e.get("heard")})
        g["n"] += 1
    done = sum(1 for e in rows if e.get("action") and e.get("ok") is not False)
    print(f"last {hours:g} h: {len(rows)} heard · {done} done · {len(failed)} not done ({len(groups)} different)\n")
    brain = None
    if replay:
        # decide each miss again with today's code — decisions only, nothing is executed
        load_api = __import__("brain")
        brain = load_api.Brain(ACTIONS, exact_match, normalize, {**load_settings(), "hud": False, "speak": False})
        print("replaying with the current code (decisions only, nothing is executed)…\n")
    fixed = 0
    for key, g in groups.items():
        e = g["last"]
        why = e.get("message") or e.get("route")
        when = time.strftime("%H:%M", time.localtime(e["at"]))
        line = f"{g['n']:>3}×  {when}  “{g['said']}”  → {e.get('route')}: {why}"
        if brain is not None and len(str(g["said"]).split()) <= 8:
            try:
                d = brain.decide(str(g["said"]))
                now = (d.action.label if d.action else ("ignored" if d.ignored else d.say or d.hud or "no action"))
            except Exception as exc:
                now = f"error: {exc}"
            ok = d.action is not None if not isinstance(now, str) or not now.startswith("error") else False
            fixed += ok
            line += f"\n        now: {'✓ ' if ok else '· '}{now}"
        print(line)
    if brain is not None:
        print(f"\n{fixed} of {len(groups)} would now lead to an action (chatter should stay ignored)")
    return 0


def print_response(response: dict[str, object], raw: bool) -> int:
    if raw:
        print(json.dumps(response, ensure_ascii=False))
    elif response.get("ok"):
        state = response.get("state")
        if isinstance(state, dict):
            print(f"status {state.get('status', '')}")
            print(f"listening {1 if state.get('listening') else 0}")
            print(f"model_ready {1 if state.get('model_ready') else 0}")
            print(f"yolo_mode {1 if state.get('yolo_mode') else 0}")
            print(f"message {state.get('message', '')}")
            print(f"transcript {state.get('transcript', '')}")
            print(f"pending_label {state.get('pending_label', '')}")
            print(f"last_action_label {state.get('last_action_label', '')}")
            print(f"last_ok {1 if state.get('last_ok') else 0}")
        else:
            print(json.dumps(response, ensure_ascii=False))
    else:
        print(str(response.get("error", "request failed")), file=sys.stderr)
    return 0 if response.get("ok") else 1


def run_learn(args) -> int:
    """Read the history, show what this voice keeps saying that the engine keeps missing."""
    import learn
    from brain import Brain

    if args.forget:
        CORRECTIONS_PATH.unlink(missing_ok=True)
        print("Forgot every accepted correction.")
        return 0

    brain = Brain(ACTIONS, exact_match, normalize, {"hud": False, "speak": False, "jev_only": False})
    brain.fuzzy_element = lambda hypotheses: None    # no screen to look at from the command line

    def decides(text: str) -> bool:
        decision = brain.fast_path(text, text) or brain.fuzzy_command([text], threshold=84)
        return bool(decision is not None and decision.action is not None)

    found = learn.proposals(HISTORY_PATH, brain.grammar(), normalize,
                            hours=args.hours, min_count=args.min_count, decides=decides)
    if not found:
        print("Nothing to learn yet — say more, or lower --min-count.")
        return 0

    current = learn.load(CORRECTIONS_PATH)
    print(f"{len(found)} mishearing{'s' if len(found) != 1 else ''} in what you said:\n")
    for p in found:
        mark = " (already corrected)" if current.get(p["heard"]) == p["means"] else ""
        print(f"  “{p['heard']}”  →  “{p['means']}”   {p['times']}×, {p['score']:.0f}% alike{mark}")
        for example in p["examples"][:2]:
            print(f"        heard as: {example!r}")
    if not args.apply:
        print("\nAccept them with:  omarchy-voice learn --apply")
        return 0

    # Even a filtered proposal can be subtly wrong — "open to browsers" is really "open two
    # browsers" — so each one is put to the user rather than written on their behalf.
    accepted = 0
    for p in found:
        if sys.stdin.isatty():
            answer = input(f"\n  “{p['heard']}” → “{p['means']}”?  [y/N/q] ").strip().lower()
            if answer == "q":
                break
            if answer not in ("y", "yes"):
                continue
        current[p["heard"]] = p["means"]
        accepted += 1
    if not accepted:
        print("\nNothing accepted.")
        return 0
    learn.save(CORRECTIONS_PATH, current)
    print(f"\nAccepted {accepted}. {len(current)} corrections in {CORRECTIONS_PATH}")
    print("They apply after:  systemctl --user restart omarchy-voice")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local voice control for Omarchy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon")
    sub.add_parser("toggle")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("confirm")
    sub.add_parser("cancel")
    sub.add_parser("yolo")
    sub.add_parser("jev-only")
    sub.add_parser("key", help="store your OpenRouter API key for Jev (~/.config/omarchy-voice/env)")
    silence_parser = sub.add_parser("transcribe-silence", help="seconds of silence that end a quick transcription")
    silence_parser.add_argument("seconds", nargs="?", type=float)
    learn_parser = sub.add_parser("learn", help="mishearings this voice produces, found in the local history")
    learn_parser.add_argument("--apply", action="store_true", help="accept them (writes corrections.json)")
    learn_parser.add_argument("--hours", type=float, default=None)
    learn_parser.add_argument("--min-count", type=int, default=2)
    learn_parser.add_argument("--forget", action="store_true", help="throw away every accepted correction")
    misses_parser = sub.add_parser("misses", help="what was said but not done (from the local history)")
    misses_parser.add_argument("--hours", type=float, default=24.0)
    misses_parser.add_argument("--all", action="store_true", help="include longer ignored sentences (chatter)")
    misses_parser.add_argument("--replay", action="store_true", help="decide each miss again with the current code")
    sub.add_parser("yolo-on")
    sub.add_parser("yolo-off")
    catalog_parser = sub.add_parser("catalog")
    catalog_parser.add_argument("--json", action="store_true")
    action_parser = sub.add_parser("action")
    action_parser.add_argument("id")
    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("text")
    speak_parser = sub.add_parser("speak")
    speak_parser.add_argument("text")
    goal_parser = sub.add_parser("goal", help="work towards a goal in the browser, step by step")
    goal_parser.add_argument("text")
    sub.add_parser("hints")
    grid_parser = sub.add_parser("grid")
    grid_parser.add_argument("--screen", action="store_true")
    sub.add_parser("clear")
    sub.add_parser("dictation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "key":
        return store_key()
    if args.command == "misses":
        return misses(args.hours, args.all, args.replay)
    if args.command == "daemon":
        code = run_daemon()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)   # don't wait for idle worker threads (OCR pool, in-flight transcriptions)
    operation: dict[str, object]
    if args.command == "toggle":
        operation = {"op": "toggle"}
    elif args.command == "start":
        operation = {"op": "listen", "enabled": True}
    elif args.command == "stop":
        operation = {"op": "listen", "enabled": False}
    elif args.command == "status":
        operation = {"op": "status"}
    elif args.command in ("confirm", "cancel", "catalog"):
        operation = {"op": args.command}
    elif args.command == "transcribe-silence":
        operation = {"op": "transcribe_silence", "seconds": args.seconds}
    elif args.command == "jev-only":
        operation = {"op": "jev_only", "mode": "toggle"}
    elif args.command == "yolo":
        operation = {"op": "yolo", "mode": "toggle"}
    elif args.command == "yolo-on":
        operation = {"op": "yolo", "mode": "on"}
    elif args.command == "yolo-off":
        operation = {"op": "yolo", "mode": "off"}
    elif args.command == "action":
        operation = {"op": "action", "id": args.id}
    elif args.command in ("preview", "speak"):
        operation = {"op": args.command, "text": args.text}
    elif args.command == "learn":
        return run_learn(args)
    elif args.command == "goal":
        operation = {"op": "speak", "text": f"autopilot {args.text}"}
    elif args.command in ("hints", "clear", "dictation"):
        operation = {"op": args.command}
    elif args.command == "grid":
        operation = {"op": "grid", "screen": args.screen}
    else:
        parser.error("unsupported command")
        return 2

    try:
        response = request(operation)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return print_response(response, bool(args.json or getattr(args, "json", False)))


if __name__ == "__main__":
    raise SystemExit(main())
