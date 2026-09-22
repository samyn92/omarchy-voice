"""The desktop brain: transcript + what is on screen -> one safe, concrete action.

  1. fast path   plain code for the phrases people say most (scroll, click,
                 keys, numbers while hints/grid are up, "again"): no network.
  2. Jev         one fan-out request over typed questions.  The state carries
                 the windows, the elements around the mouse, the recent actions.
                 Jev only *picks* ids — windows, elements, keys, apps, spans.
  3. policy      code turns the answers into an Action with validated values.
  4. execute     Hyprland dispatchers, uinput pointer + keyboard, Playwright.

Model output is never executed as text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import apps
import browsers
import desktop
import fuzzy
import herdr_ctl
import jev
import keyboard
import perceive
from spans import extract_text_candidates, extract_url_candidates, to_http_url
from uinput import pointer

TERMINALS = {"foot", "kitty", "alacritty", "com.mitchellh.ghostty", "ghostty", "wezterm", "org.wezfurlong.wezterm"}

# name -> (mods, key, label)
KEYS: dict[str, tuple[str, str, str]] = {
    "enter": ("", "Return", "Enter"), "escape": ("", "Escape", "Escape"), "tab": ("", "Tab", "Tab"),
    "shift_tab": ("SHIFT", "Tab", "Shift+Tab"), "line_break": ("SHIFT", "Return", "New line"), "backspace": ("", "BackSpace", "Backspace"),
    "delete": ("", "Delete", "Delete"), "space": ("", "space", "Space"),
    "up": ("", "Up", "Up"), "down": ("", "Down", "Down"), "left": ("", "Left", "Left"), "right": ("", "Right", "Right"),
    "page_up": ("", "Prior", "Page up"), "page_down": ("", "Next", "Page down"),
    "home": ("", "Home", "Home"), "end": ("", "End", "End"),
    "top": ("CTRL", "Home", "Go to top"), "bottom": ("CTRL", "End", "Go to bottom"),
    "copy": ("CTRL", "c", "Copy"), "paste": ("CTRL", "v", "Paste"), "cut": ("CTRL", "x", "Cut"),
    "undo": ("CTRL", "z", "Undo"), "redo": ("CTRL SHIFT", "z", "Redo"), "select_all": ("CTRL", "a", "Select all"),
    "save": ("CTRL", "s", "Save"), "find": ("CTRL", "f", "Find"), "print": ("CTRL", "p", "Print"),
    "new_tab": ("CTRL", "t", "New tab"), "close_tab": ("CTRL", "w", "Close tab"),
    "reopen_tab": ("CTRL SHIFT", "t", "Reopen tab"), "next_tab": ("CTRL", "Tab", "Next tab"),
    "previous_tab": ("CTRL SHIFT", "Tab", "Previous tab"), "refresh": ("", "F5", "Refresh"),
    "zoom_in": ("CTRL", "plus", "Zoom in"), "zoom_out": ("CTRL", "minus", "Zoom out"),
    "zoom_reset": ("CTRL", "0", "Reset zoom"), "new_window": ("CTRL", "n", "New window"),
    "delete_word": ("CTRL", "BackSpace", "Delete word"), "address_bar": ("CTRL", "l", "Address bar"),
    "back": ("ALT", "Left", "Back"), "forward": ("ALT", "Right", "Forward"),
    "word_left": ("CTRL", "Left", "Word left"), "word_right": ("CTRL", "Right", "Word right"),
    "select_word_left": ("CTRL SHIFT", "Left", "Select word left"),
    "select_word_right": ("CTRL SHIFT", "Right", "Select word right"),
    "interrupt": ("CTRL", "c", "Interrupt"),
    **{f"digit_{n}": ("", str(n), f"Press {n}") for n in range(10)},
}
KEY_DESCRIPTIONS = {
    "enter": "Enter / Return / submit", "escape": "Escape / close popup / exit", "tab": "Tab / next field",
    "shift_tab": "Shift+Tab / previous field", "backspace": "Backspace (delete one character back)",
    "delete": "Delete key", "space": "Space bar (also play/pause in players)",
    "up": "Arrow up", "down": "Arrow down", "left": "Arrow left", "right": "Arrow right",
    "page_up": "Page up", "page_down": "Page down", "home": "Home (start of line)", "end": "End (end of line)",
    "top": "Jump to the very top of the document", "bottom": "Jump to the very bottom of the document",
    "copy": "Copy the selection", "paste": "Paste the clipboard", "cut": "Cut the selection",
    "undo": "Undo the last edit", "redo": "Redo", "select_all": "Select all", "save": "Save the file/document",
    "find": "Find / search in page or file", "print": "Print", "new_tab": "Open a new tab",
    "close_tab": "Close the current tab", "reopen_tab": "Reopen the tab that was just closed",
    "next_tab": "Switch to the next tab", "previous_tab": "Switch to the previous tab",
    "refresh": "Refresh / reload the page", "zoom_in": "Zoom in / make text bigger",
    "zoom_out": "Zoom out / make text smaller", "zoom_reset": "Reset zoom to 100%",
    "new_window": "Open a new window of the app", "delete_word": "Delete the previous word",
    "address_bar": "Focus the browser address bar", "back": "Go back (browser history)",
    "forward": "Go forward (browser history)", "word_left": "Move cursor one word left",
    "word_right": "Move cursor one word right", "select_word_left": "Extend selection one word left",
    "select_word_right": "Extend selection one word right",
    "interrupt": "Interrupt / cancel the running program in a terminal (Ctrl+C)",
    **{f"digit_{n}": f"The number key {n} (pick option {n} in a menu or dialog)" for n in range(10)},
}

INTENTS = {
    "focus_window": {"what": "Switch to / focus / show / bring up a window that is already open",
                     "examples": ["switch to the browser", "focus spotify", "show me the terminal", "go to my email window"]},
    "close_window": {"what": "Close / quit a window", "examples": ["close this window", "close spotify", "quit the terminal"]},
    "move_window_to_workspace": {"what": "Move / send a window to another workspace (desktop)",
                                 "examples": ["move this to workspace 3", "send spotify to workspace two", "put the browser on the next workspace"]},
    "arrange_window": {"what": "Change a window's size, state or placement: fullscreen, maximize, float, tile, pin, center, wider, narrower, taller, shorter, half screen, snap left/right/top/bottom/corners, minimize, restore, group, fit",
                       "not_for": "Moving a window to another workspace; swapping two windows",
                       "examples": ["make it fullscreen", "make this wider", "snap the browser to the left half", "float this window", "minimize spotify", "center it", "half width"]},
    "swap_window": {"what": "Swap / move a window left, right, up or down within the layout",
                    "examples": ["move this window left", "swap it to the right", "push the terminal right"]},
    "focus_direction": {"what": "Move focus to the neighbouring window in a direction",
                        "examples": ["focus left", "select right", "select the left window", "the window on the right", "go up a window"]},
    "go_to_workspace": {"what": "Switch to a workspace (virtual desktop): a number, next, previous or an empty one",
                        "examples": ["workspace three", "next workspace", "go to desktop two", "empty workspace"]},
    "show_scratchpad": {"what": "Show or hide the scratchpad (hidden windows)", "examples": ["show scratchpad", "toggle scratchpad"]},
    "click": {"what": "Click / press / open / select / choose something visible on screen (a button, link, tab, item, text, icon) — or click where the mouse is",
              "not_for": "Opening an application by name; switching windows",
              "examples": ["click save", "press the blue button", "open the second result", "click on settings", "click here", "right click the file", "double click that"]},
    "point_mouse": {"what": "Move the mouse pointer onto something or in a direction, without clicking (hover)",
                    "examples": ["move the mouse to the search box", "hover over the menu", "mouse left a bit"]},
    "scroll": {"what": "Scroll the content up, down, left or right", "examples": ["scroll down", "scroll up a lot", "scroll right", "go down a bit"]},
    "type_text": {"what": "Type / write / enter specific text (optionally into a named field)",
                  "not_for": "Web searches (search_web); pressing a single key",
                  "examples": ["type hello world", "write thanks for the update", "enter my name in the name field"]},
    "press_key": {"what": "Press a key or keyboard shortcut: enter, escape, tab, arrows, page up/down, copy, paste, undo, save, find, new tab, zoom...",
                  "examples": ["press enter", "escape", "copy that", "paste", "undo", "save", "new tab", "zoom in", "press down three times"]},
    "navigate_url": {"what": "Open a website by name or address in the browser",
                     "examples": ["go to github", "open youtube dot com", "take me to wikipedia"]},
    "search_web": {"what": "Search the web or a named site for something",
                   "examples": ["search for alan turing", "google cheap flights", "search youtube for techno"]},
    "herdr_focus": {"what": "Switch to a coding agent (or an agent workspace) in herdr, named by its project, task, kind or state",
                    "not_for": "Switching to a desktop window or application; giving the agent a task",
                    "examples": ["go to the agentops agent", "switch to claude", "show me the agent working on homecluster", "open the presentation agent"]},
    "prompt_agent": {"what": "Send a message / instruction / task to a coding agent in herdr (tell, ask, instruct an agent)",
                     "not_for": "Typing text into the focused field; switching to an agent",
                     "examples": ["tell the agentops agent to run the tests", "ask claude to commit and push", "tell homecluster to check the pods"]},
    "launch_app": {"what": "Open / launch / start an installed application by name",
                   "not_for": "Clicking something inside a window; opening a website",
                   "examples": ["open spotify", "launch the file manager", "start signal", "open my email app"]},
    "system_action": {"what": "A system control: volume, mute, media play/pause/next, brightness, night light, menus, clipboard, emoji picker, do not disturb, lock, log out, reboot, shut down",
                      "examples": ["volume up", "mute", "next song", "brighter", "lock the screen", "show the app menu"]},
    "show_hints": {"what": "Show numbered labels on the clickable things on screen", "examples": ["show numbers", "show hints", "label everything", "what can I click"]},
    "show_grid": {"what": "Show the numbered mouse grid to point precisely", "examples": ["mouse grid", "show the grid", "grid"]},
    "hide_overlay": {"what": "Hide the hints / grid / labels", "examples": ["hide numbers", "close the grid", "clear"]},
    "read_screen": {"what": "A question about what is on screen: read aloud / tell what is shown, playing, selected or under the mouse",
                    "examples": ["what's this", "read this", "what does it say", "what's on screen", "which song is playing", "what's the title here"]},
    "repeat_last": {"what": "Do the previous action again", "examples": ["again", "once more", "do that again", "more", "repeat"]},
    "confirm": {"what": "Approve a pending action", "examples": ["yes", "confirm", "do it"]},
    "cancel": {"what": "Cancel the pending action / never mind", "examples": ["cancel", "never mind", "no"]},
    "none": {"what": "Not a computer command: chit-chat, talking to someone else, filler, a fragment",
             "examples": ["um", "what do you think", "the weather is nice", "okay so"]},
}

WINDOW_VERBS = {
    "fullscreen": "Toggle true fullscreen", "maximize": "Maximize (fill the screen but keep the bar)",
    "float": "Toggle floating", "tile": "Put it back into the tiling layout",
    "pin": "Pin: keep on top on every workspace", "center": "Center the window",
    "wider": "Make wider", "narrower": "Make narrower / thinner", "taller": "Make taller", "shorter": "Make shorter",
    "half_width": "Half the screen width", "third_width": "One third of the screen width",
    "full_width": "Full screen width", "fit": "Fit / reveal the whole window",
    "snap_left": "Left half of the screen", "snap_right": "Right half of the screen",
    "snap_top": "Top half", "snap_bottom": "Bottom half",
    "snap_top_left": "Top-left quarter", "snap_top_right": "Top-right quarter",
    "snap_bottom_left": "Bottom-left quarter", "snap_bottom_right": "Bottom-right quarter",
    "minimize": "Minimize / hide the window", "restore": "Restore / bring back a minimized window",
    "group": "Group / ungroup into tabs", "none": "Not about window arrangement",
}

# "transcribe": voxtype records and types the whole passage itself (its own model, OSD and filters)
# two ways in: "transcribe" = quick (ends by itself after a few seconds of silence),
# "start transcribe" = long (pauses allowed, ends only with "end transcribe")
TRANSCRIBE_QUICK = ("transcribe", "transcription", "quick transcribe", "transcribe quick", "transcribe this")
TRANSCRIBE_LONG = ("start transcribe", "start transcribing", "start transcription", "start transcribe mode",
                   "begin transcription", "begin transcribing", "long transcribe", "long transcription",
                   "transcribe on", "transcription on", "transcribe mode", "transcription mode")
TRANSCRIBE_ON = TRANSCRIBE_QUICK + TRANSCRIBE_LONG
# Whisper often writes "end transcribe" as "and transcribe": both count, as the last words before a pause
TRANSCRIBE_OFF = ("end transcribe", "end transcription", "end transcribing", "and transcribe", "and transcription",
                  "transcribe off", "transcription off", "stop transcribing", "stop transcription", "stop transcribe",
                  "transcribe done", "transcription done", "finish transcribing", "finish transcription",
                  "transcribe stop", "transcribe finish", "transcribe end", "done transcribing")
TRANSCRIBE_SEND = ("end transcribe send", "end transcribe and send", "and transcribe send", "and transcribe and send",
                   "end transcription and send", "transcribe send", "transcribe and send", "send transcription",
                   "transcribe submit", "transcription send", "transcribe enter", "transcribe off and send", "transcribe and submit")
TRANSCRIBE_CANCEL = ("cancel transcription", "cancel transcribing", "transcribe cancel", "discard transcription")
# a goal for the browser to work towards by itself, rather than one command
GOAL_PREFIXES = ("autopilot", "auto pilot", "browse for", "work out", "figure out", "find out",
                 "research", "look into", "take over and", "do this for me")

# spoken addresses: "go to example com" (the dot does not survive normalization)
TLDS = "com|org|net|dev|io|ai|app|de|eu|co|tv|me|info|news|sh|gg|xyz"
READ_SCREEN = ("what is this", "what's this", "what is this page about", "what's this page about",
               "what is under the mouse", "what's under the mouse", "what is under my mouse",
               "read this", "read it", "read the screen", "read this page", "what does it say",
               "what does this say", "what is on screen", "what's on screen", "what's on the screen",
               "which song is playing", "what song is playing", "what is playing")
DICTATION_ON = ("start dictation", "dictation mode", "start dictating", "dictation on", "dictate")
AGAIN = ("again", "repeat", "repeat that", "do that again", "do it again", "once more", "one more time", "more", "one more")
HINTS = ("show hints", "show numbers", "numbers", "hints", "show labels", "label everything", "show links", "what can i click")
GRID = ("grid", "mouse grid", "show grid", "show the grid", "show mouse grid")
SCREEN_GRID = ("screen grid", "full screen grid", "whole screen grid")
CLEAR = ("hide hints", "hide numbers", "hide grid", "close grid", "clear", "hide", "hide labels")
SIMPLE_WINDOW = {
    "fullscreen": "fullscreen", "full screen": "fullscreen", "toggle fullscreen": "fullscreen",
    "maximize": "maximize", "maximise": "maximize", "float": "float", "float this": "float",
    "tile": "tile", "tile this": "tile", "pin": "pin", "pin this": "pin", "center": "center",
    "center this": "center", "center it": "center", "minimize": "minimize", "minimise": "minimize",
    "snap left": "snap_left", "snap right": "snap_right", "half width": "half_width",
}
MODE_KINDS = ("voxtype", "mode")   # decisions that switch how speech is treated: never delegated
SEARCH_SITES = {"youtube": "youtube", "wikipedia": "wikipedia", "github": "github", "amazon": "amazon", "reddit": "reddit",
                "google": "google", "duckduckgo": "duckduckgo", "hacker news": "hacker_news", "twitter": "twitter_x"}
FOCUS_DIRECTION = re.compile(r"(?:focus|window|select|switch|go to)(?: the)? (left|right|up|down|above|below|beneath|underneath)(?: window)?")
FOCUS_SYNONYM = {"above": "up", "below": "down", "beneath": "down", "underneath": "down"}

NUMBER_WORDS = {
    "zero": 0, "one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "ate": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")


def parse_number(text: str) -> int | None:
    t = text.strip().lower()
    t = re.sub(r"^(\d{1,3})(?:st|nd|rd|th)$", r"\1", t)   # Whisper writes "1st", "2nd", "3rd"
    if re.fullmatch(r"\d{1,3}", t):
        return int(t)
    parts = t.replace("-", " ").split()
    if not parts or any(p not in NUMBER_WORDS for p in parts):
        return None
    if len(parts) == 1:
        return NUMBER_WORDS[parts[0]]
    if len(parts) == 2 and NUMBER_WORDS[parts[0]] % 10 == 0 and NUMBER_WORDS[parts[0]] >= 20 and NUMBER_WORDS[parts[1]] < 10:
        return NUMBER_WORDS[parts[0]] + NUMBER_WORDS[parts[1]]
    return None


def times_in(text: str) -> int:
    m = re.search(r"\b(\w+(?: \w+)?) times\b", text) or re.search(r"\b(twice)\b", text)
    if not m:
        return 1
    if m.group(1) == "twice":
        return 2
    n = parse_number(m.group(1)) or parse_number(m.group(1).split()[-1])
    return max(1, min(30, n or 1))


def workspace_in(text: str) -> str | None:
    m = re.search(r"\b(?:workspace|desktop|space)\s+(?:number\s+)?(\w+(?:[ -]\w+)?)", text)
    if m:
        n = parse_number(m.group(1)) or parse_number(m.group(1).split()[0])
        if n and 1 <= n <= 10:
            return str(n)
    m = re.search(r"\b(\w+) (?:workspace|desktop)\b", text)          # "the second workspace", "2nd desktop"
    if m and (n := parse_number(m.group(1))) and 1 <= n <= 10:
        return str(n)
    if re.search(r"\bnext (?:workspace|desktop)\b", text):
        return "e+1"
    if re.search(r"\b(?:previous|last) (?:workspace|desktop)\b", text):
        return "e-1"
    if re.search(r"\b(?:empty|new|free) (?:workspace|desktop)\b", text):
        return "empty"
    return None


@dataclass
class Action:
    kind: str
    label: str
    args: dict = field(default_factory=dict)
    confirm: bool = False
    repeatable: bool = True


@dataclass
class Decision:
    action: Action | None = None
    say: str = ""
    hud: str = ""
    source: str = "fast"
    ms: dict = field(default_factory=dict)
    ignored: bool = False
    cost: float = 0.0
    scene: str = ""        # what was looked at: "web · 42 elements · helium"


ENV_FILE = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "omarchy-voice" / "env"


def load_api_key() -> bool:
    """OPENROUTER_API_KEY from the environment or ~/.config/omarchy-voice/env (written by `omarchy-voice key`)."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY=") and line.split("=", 1)[1].strip():
                os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
                return True
    except OSError:
        pass
    return False


class Overlay:
    """Client for overlay.py (a separate system-Python GTK process)."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def send(self, **msg) -> None:
        import json

        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                try:
                    self.proc = subprocess.Popen(
                        ["/usr/bin/python3", str(Path(__file__).with_name("overlay.py"))],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                        env={**os.environ, "LD_PRELOAD": "/usr/lib/libgtk4-layer-shell.so"},
                    )
                except OSError:
                    self.proc = None
                    return
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.proc = None


class Brain:
    def __init__(self, catalog: dict, exact_match, normalizer, settings: dict, browser=None, speaker=None) -> None:
        load_api_key()
        self.catalog = catalog              # system action id -> omarchy_voice.Action
        self.exact_match = exact_match      # omarchy_voice.exact_match (static catalog, no model)
        self.normalize = normalizer
        self.settings = settings
        self.browser = browser
        self.speaker = speaker
        self.perceiver = perceive.Perceiver(browser, settings)
        self._hud_texts: list[tuple[float, str]] = []
        self.perceiver.is_own_text = self.is_own_text
        self.overlay = Overlay()
        self.recent: deque = deque(maxlen=3)
        self.last_action: Action | None = None
        self.mode = ""                        # "" | "hints" | "grid" | "choose" | "dictation"
        self.mode_items: list = []            # hint elements or disambiguation candidates
        self.grid_rect: tuple[float, float, float, float] | None = None
        self.scene: perceive.Scene | None = None
        self.scene_at = 0.0
        self._prefetch: threading.Thread | None = None
        self._prefetch_scene: perceive.Scene | None = None
        self.last_typed = ""
        self._kinds: dict[str, str] = {}
        self._grammar_cache: dict[str, str] | None = None
        self._herdr_pids: dict[int, str | None] = {}
        self._jev_herdr = None
        self._agent_messages: list[str] = []
        self.transcribe_started = 0.0
        self.transcribe_tail = ""
        self.mode_since = 0.0
        self.transcribe_passage = 0
        self.transcribe_kind = "long"
        self.before_typing = None   # set by the daemon: closes the bar panel, which would swallow keystrokes

    # -- perception ---------------------------------------------------------
    def prefetch(self) -> None:
        """Called when speech starts: look at the screen while the user talks."""
        if self._prefetch and self._prefetch.is_alive():
            return

        def run():
            try:
                self._prefetch_scene = self.perceiver.capture()
            except Exception:
                self._prefetch_scene = None

        self._prefetch_scene = None
        self._prefetch = threading.Thread(target=run, name="prefetch", daemon=True)
        self._prefetch.start()

    def current_scene(self, need_elements: bool = True, max_age: float = 8.0) -> perceive.Scene:
        if self._prefetch:
            self._prefetch.join(timeout=2.0)
            self._prefetch = None
            if self._prefetch_scene is not None:
                self.scene, self.scene_at = self._prefetch_scene, time.monotonic()
                self._prefetch_scene = None
        if self.scene is not None and time.monotonic() - self.scene_at < min(max_age, 4.0) and (self.scene.elements or not need_elements):
            # the desktop part changes fast; refresh it cheaply
            fresh = desktop.snapshot()
            same_target = self.scene.target and any(w.address == self.scene.target.address and (w.x, w.y, w.w, w.h) == (self.scene.target.x, self.scene.target.y, self.scene.target.w, self.scene.target.h) for w in fresh.windows)
            if same_target:
                self.scene.desk = fresh
                self.scene.target = next(w for w in fresh.windows if w.address == self.scene.target.address)
                return self.scene
        self.scene, self.scene_at = self.perceiver.capture(with_elements=need_elements), time.monotonic()
        return self.scene

    def invalidate(self) -> None:
        self.scene = None

    # -- feedback -------------------------------------------------------------
    def hud(self, text: str, sub: str = "", tone: str = "ok", ms: int = 2200) -> None:
        if self.settings.get("hud", True):
            now = time.monotonic()
            self._hud_texts = [(t, x) for t, x in self._hud_texts if now - t < 30] + [(now, text), (now, sub)]
            self.overlay.send(op="hud", text=text, sub=sub, tone=tone, ms=ms)

    def orb(self, state: str, level: float = 0.0) -> None:
        """The glowing orb: listening · hearing · thinking · acting · transcribe · error · off."""
        if self.settings.get("orb", True):
            self.overlay.send(op="orb", state=state, level=round(level, 3))

    def is_own_text(self, text: str) -> bool:
        """Text the overlay showed in the last 30 s (OCR reads it back from the screen)."""
        t = text.lower().strip()
        return len(t) >= 4 and any(x and fuzzy.fuzz.partial_ratio(t, x.lower()) >= 88 for _, x in self._hud_texts)

    def say(self, text: str) -> None:
        if self.speaker and self.settings.get("speak", True):
            try:
                self.speaker.speak(text, wait=False)
            except Exception:
                pass

    # -- entry point ------------------------------------------------------------
    def decide(self, transcript: str, pending_label: str = "", alternatives: tuple[str, ...] = ()) -> Decision:
        """alternatives: other recognizers' versions of the same utterance."""
        started = time.perf_counter()
        if self.mode in ("hints", "choose", "grid") and time.monotonic() - self.mode_since > 20:
            self.clear_modal()
        spoken = self.normalize(transcript)
        others = [self.normalize(a) for a in alternatives if a and self.normalize(a) != spoken]
        if self.mode == "transcribe":
            decision = self.transcribing([spoken, *others])
            decision.ms["total"] = round((time.perf_counter() - started) * 1000)
            return decision
        if self.jev_only():
            # everything is decided by Jev; only mode switches (transcribe/dictation) stay local
            local = self.fast_path(spoken, transcript)
            if local is not None and local.action is not None and local.action.kind in MODE_KINDS:
                decision = local
            else:
                decision = self.ask_jev(transcript, spoken, pending_label, others)
                decision.source += " · jev only"
            decision.ms["total"] = round((time.perf_counter() - started) * 1000)
            return decision
        decision = self.fast_path(spoken, transcript)
        if decision is None:
            for other in others:  # the other recognizer may have heard an exact command
                if self.mode != "dictation" and (decision := self.fast_path(other, other)) is not None:
                    decision.source = "fast/alt"
                    break
        if decision is None and self.mode != "dictation":
            decision = self.fuzzy_command([spoken, *others], threshold=84)
        if decision is None and self.mode != "dictation":
            decision = self.fuzzy_element([spoken, *others])
        if decision is None:
            decision = self.ask_jev(transcript, spoken, pending_label, others)
            if decision.action and others and max(fuzzy.fuzz.ratio(spoken, o) for o in others) < 45:
                # "There you go." vs "Take this.": no stable hearing, so don't guess at an action
                decision = Decision(ignored=True, source=decision.source,
                                    hud=f"Didn't catch that (“{transcript[:40]}” / “{others[0][:40]}”)")
        decision.ms["total"] = round((time.perf_counter() - started) * 1000)
        return decision

    def jev_only(self) -> bool:
        """Jev-only mode, except while a numbered overlay or a typing mode owns what is said."""
        return bool(self.settings.get("jev_only")) and self.mode == ""

    def allows_local(self, decision: Decision | None) -> bool:
        return decision is not None and (not self.jev_only() or (decision.action is not None and decision.action.kind in MODE_KINDS))

    # -- fuzzy ----------------------------------------------------------------
    def grammar(self) -> dict[str, str]:
        """Every command phrase the fast path knows -> the canonical text it understands."""
        g: dict[str, str] = {}
        for d in fuzzy.DIRECTIONS:
            for amount in ("", " a bit", " a lot"):
                g[f"scroll {d}{amount}"] = f"scroll {d}{amount}"
            for verb in ("focus", "select", "window", "switch"):
                g[f"{verb} {d}"] = f"focus {d}"
                g[f"{verb} the {d}"] = f"focus {d}"
                g[f"{verb} the {d} window"] = f"focus {d}"
        for phrase in ("page up", "page down", "scroll to the top", "scroll to the bottom", "click", "left click",
                       "right click", "double click", "middle click", "click here", "next workspace",
                       "previous workspace", "wider", "narrower", "taller", "shorter", "bigger", "smaller",
                       "make it wider", "make it narrower", "start dictation", "stop dictation",
                       "expand", "expand window", "expand this window", "enlarge window", "shrink", "shrink window",
                       *AGAIN, *HINTS, *GRID, *SCREEN_GRID, *CLEAR, *SIMPLE_WINDOW, *TRANSCRIBE_ON,
                       *TRANSCRIBE_OFF, *TRANSCRIBE_CANCEL, *TRANSCRIBE_SEND, *HERDR_PHRASES,
                       "send", "send it", "hit enter", "submit it"):
            g[phrase] = phrase
        for n, word in enumerate(ORDINALS[:9], 1):
            for phrase in (f"{word} agent", f"select the {word} agent", f"select {word} agent", f"go to the {word} agent"):
                g[phrase] = f"{word} agent"
        for n, word in enumerate(fuzzy.NUMBERS[:10], 1):
            g[f"workspace {word}"] = f"workspace {word}"
            g[f"go to workspace {word}"] = f"workspace {word}"
        for phrase, key in FAST_KEYS.items():
            g[phrase] = phrase
            g[f"press {phrase}"] = phrase
        for item in self.catalog.values():
            for alias in item.aliases:
                g[alias] = alias
        return g

    def modal_grammar(self) -> dict[str, str]:
        g: dict[str, str] = {}
        count = 9 if self.mode == "grid" else min(len(self.mode_items), 20)
        for n in range(1, count + 1):
            word = fuzzy.NUMBERS[n - 1]
            for prefix in ("", "select ", "click ", "pick ", "number ", "choose "):
                g[f"{prefix}{word}"] = f"click {word}" if prefix == "click " or self.mode != "grid" else word
        if self.mode == "grid":
            for phrase in ("click", "right click", "double click", "move", "cancel"):
                g[phrase] = phrase
        return g

    def fuzzy_command(self, hypotheses: list[str], threshold: float = 84) -> Decision | None:
        grammar = self.modal_grammar() if self.mode in ("hints", "choose", "grid") else {}
        grammar.update(self._grammar_cache or self._build_grammar())
        match = fuzzy.best_match(hypotheses, grammar)
        if not match or match.score < threshold or match.margin < 4:
            return None
        decision = self.fast_path(match.canonical, match.canonical)
        if decision is None or decision.action is None:
            return None
        decision.source = f"fuzzy “{match.heard}”→“{match.phrase}” {match.score:.0f}"
        return decision

    def _build_grammar(self) -> dict[str, str]:
        self._grammar_cache = self.grammar()
        return self._grammar_cache

    def fuzzy_element(self, hypotheses: list[str]) -> Decision | None:
        """"click / select / press <label>": match the label against what is on screen."""
        targets = [t for t in (fuzzy.element_target(h) for h in hypotheses) if t]
        if not targets:
            return None
        scene = self.current_scene(need_elements=True)
        if not scene.elements:
            return None
        best: list = []
        for target in targets:
            ranked = fuzzy.best_elements(target, scene.elements)
            if ranked and (not best or ranked[0][0] > best[0][0]):
                best = ranked
        if not best or best[0][0] < 86:
            return None
        top_score, el = best[0]
        rivals = [e for s, e in best[1:] if s > top_score - 6]
        if rivals:
            same_text = [e for e in rivals if e.text.strip().lower() == el.text.strip().lower()]
            if len(same_text) == len(rivals):
                return Decision(Action("choose", "Which one?", {"items": [el, *rivals]}, repeatable=False),
                                say="Which one? Say a number.", source="fuzzy/element")
            return None  # genuinely unclear: let Jev weigh it with the rest of the context
        return Decision(Action("click", f"Click “{el.text[:40]}”", {"x": el.x, "y": el.y, "kind": "left", "element": el},
                               confirm=self._risky(el), repeatable=False),
                        source=f"fuzzy/element {top_score:.0f}")

    # -- 1. fast path -----------------------------------------------------------
    def fast_path(self, s: str, raw: str) -> Decision | None:
        if not s:
            return Decision(ignored=True)

        if self.mode == "transcribe":
            return self.transcribing([s])
        if s in TRANSCRIBE_LONG:
            return Decision(Action("voxtype", "Transcribe (long)", {"op": "start", "kind": "long"}, repeatable=False))
        if s in TRANSCRIBE_QUICK:
            return Decision(Action("voxtype", "Transcribe (quick)", {"op": "start", "kind": "quick"}, repeatable=False))
        if s in TRANSCRIBE_OFF or s in TRANSCRIBE_CANCEL or s in TRANSCRIBE_SEND:
            return Decision(ignored=True, source="fast", hud="Not transcribing right now")
        m = re.match(r"\s*((?:start|begin)\s+)?transcri(?:be|ption|bing)\b[\s,.:;!-]*(.+)", raw, re.IGNORECASE)
        if m and s.split()[0] in ("transcribe", "transcription", "start", "begin") and len(s.split()) >= 2 \
                and not re.fullmatch(r"(?:start |begin )?transcri\w* (?:off|on|mode|stop|done|end|finish\w*|send|quick|this)", s):
            # "Transcribe, cd ls …" in one breath: those words were said before voxtype listened
            kind = "long" if m.group(1) else "quick"
            return Decision(Action("voxtype", f"Transcribe ({kind})", {"op": "start", "kind": kind, "prefix": m.group(2).strip()},
                                   repeatable=False))

        if self.mode == "dictation":
            return self.dictation(s, raw)

        if s in DICTATION_ON:
            return Decision(Action("mode", "Dictation on", {"mode": "dictation"}, repeatable=False))

        if self.mode in ("hints", "choose", "grid"):
            picked = self.modal(s)
            if picked is not None:
                return picked
            if not re.fullmatch(r"(?:select |pick |click |number |choose )?\w+", s):
                self.clear_modal()   # the user moved on: don't leave numbers waiting on screen

        if s in AGAIN and self.last_action:
            if self.last_action.repeatable:
                return Decision(self.last_action)

        if s in HINTS:
            return Decision(Action("hints", "Show hints", repeatable=False))
        if s in GRID:
            return Decision(Action("grid", "Mouse grid", {"screen": False}, repeatable=False))
        if s in SCREEN_GRID:
            return Decision(Action("grid", "Mouse grid", {"screen": True}, repeatable=False))
        if s in CLEAR:
            return Decision(Action("clear", "Hide overlay", repeatable=False))

        m = re.fullmatch(r"(scroll|page|go|move) (up|down|left|right)(?: (a bit|a little|slightly|a lot|more|lots|far|all the way))?", s)
        if m and (m.group(1) in ("scroll", "page") or (m.group(3) and m.group(2) in ("up", "down"))):
            direction = m.group(2)
            amount = {"a bit": "little", "a little": "little", "slightly": "little", "a lot": "lot", "lots": "lot", "far": "lot", "all the way": "end"}.get(m.group(3) or "", "normal")
            if m.group(1) == "page" and direction in ("up", "down"):
                return self.key_decision("page_down" if direction == "down" else "page_up", 1)
            if amount == "end" and direction in ("up", "down"):
                return self.key_decision("bottom" if direction == "down" else "top", 1)
            return Decision(Action("scroll", f"Scroll {direction}", {"direction": direction, "amount": "lot" if amount == "end" else amount}))
        if s in ("scroll to the top", "go to the top", "back to the top", "top of the page"):
            return self.key_decision("top", 1)
        if s in ("scroll to the bottom", "go to the bottom", "bottom of the page"):
            return self.key_decision("bottom", 1)

        # "new claude", "create a new agent", "start another codex agent", "new oh my pi"
        m = re.fullmatch(r"(?:please )?(?:(?:create|start|launch|open|spawn|make|add|run)(?: me)?(?: a| an| another)?(?: new)?|new|another)"
                         r"(?: (.+?))?(?: coding)?(?: agents?| sessions?)?", s)
        if m and shutil.which("herdr") and (re.search(r"\b(new|another|agent|create|spawn)\b", s) or self.herdr_window()):
            kind = self.agent_kind(m.group(1) or "")
            if kind:
                return Decision(Action("herdr", f"New {kind} agent", {"op": "new_agent", "arg": kind}, repeatable=False))
            if m.group(1) and re.search(r"\bagents?\b", s):
                return Decision(say=f"I don't know an agent called {m.group(1)}.", hud=f"Unknown agent “{m.group(1)}”", source="fast")

        op = HERDR_PHRASES.get(s)
        # the agent rows in herdr's sidebar: "select the second agent", "agent three", "3rd agent"
        m = (re.fullmatch(r"(?:(?:select|go to|switch to|focus|open|show|pick|take)(?: the)? )?(\w+) agent", s)
             or re.fullmatch(r"(?:(?:select|go to|switch to|focus|open|show|pick|take)(?: the)? )?agent (?:number )?(\w+)", s))
        if op is None and m and parse_number(m.group(1)):
            op = ("nth_agent", parse_number(m.group(1)))
        agent_only = op is not None and op[0] in AGENT_OPS
        herdr = self.herdr_window(anywhere=agent_only) if op else None
        if op and agent_only and not herdr:
            return Decision(say="Herdr isn't open. Say open herdr.", hud="herdr isn't open — say “open herdr”", source="fast")
        if herdr:
            if op:
                return Decision(Action("herdr", s.capitalize(), {"op": op[0], "arg": op[1], "session": herdr[1], "address": herdr[0].address},
                                       repeatable=op[0] in ("step_agent", "step_tab", "step_workspace", "pane")))

        if s in READ_SCREEN:
            return Decision(Action("read", "Read screen", {"element": None}, repeatable=False))

        m = re.fullmatch(r"(left |right |middle |double |triple )?click(?: here| there| that| it)?", s)
        if m:
            kind = (m.group(1) or "left ").strip()
            return Decision(Action("click", f"{kind.title()} click here", {"here": True, "kind": kind}))

        s = re.sub(r"^double (press|tap|hit) (.+)", r"press \2 twice", s)   # "double press escape"
        bare = re.sub(r" (?:\w+ times|twice)$", "", s)
        key = FAST_KEYS.get(bare) or FAST_KEYS.get(re.sub(r"^(?:press|hit|tap) (?:the )?|(?: key)$", "", bare))
        if key:
            return self.key_decision(key, times_in(s))

        ws = workspace_in(s)
        if ws and re.fullmatch(
            r"(?:go to |switch to |open |show )?(?:the )?"
            r"(?:(?:workspace|desktop|space)(?: number)? \w+(?: \w+)?|\w+ (?:workspace|desktop))", s):
            return Decision(Action("workspace", f"Workspace {ws}", {"target": ws}))
        m = re.fullmatch(r"(?:move|send|put)(?: this| the| this window| the window| it)? (?:window )?(?:to|on) (?:the )?.*", s)
        if m and ws and ws not in ("e+1", "e-1", "empty") and re.fullmatch(
                r"(?:move|send|put)(?: this| it| this window| the window| window)? (?:to|on) (?:the )?(?:(?:workspace|desktop) \w+|\w+ (?:workspace|desktop))", s):
            return Decision(Action("window", f"Move to workspace {ws}", {"verb": "move_to_workspace", "slots": {"workspace": ws}}))

        m = FOCUS_DIRECTION.fullmatch(s)
        if m:
            direction = FOCUS_SYNONYM.get(m.group(1), m.group(1))
            return Decision(Action("focus_dir", f"Focus {direction}", {"direction": direction}))

        m = re.fullmatch(r"(?:make (?:it|this|this window|the window) )?(wider|narrower|taller|shorter|bigger|smaller|expand|enlarge|widen|grow|shrink|narrow)"
                         r"(?: (?:it|this|the window|this window|window))?(?: (a bit|a little|a lot))?", s)
        if m:
            verb = {"bigger": "wider", "expand": "wider", "enlarge": "wider", "widen": "wider", "grow": "wider",
                    "smaller": "narrower", "shrink": "narrower", "narrow": "narrower"}.get(m.group(1), m.group(1))
            amount = {"a bit": "little", "a little": "little", "a lot": "lot"}.get(m.group(2) or "", "normal")
            return Decision(Action("window", f"{verb.title()}", {"verb": verb, "slots": {"amount": amount}}))
        if s in SIMPLE_WINDOW:
            verb = SIMPLE_WINDOW[s]
            return Decision(Action("window", WINDOW_VERBS[verb], {"verb": verb}))

        # the static system catalog (volume, media, brightness, menus, session…)
        selected = self.exact_match(s)
        if selected:
            return Decision(Action("system", selected.label, {"action": selected}, confirm=selected.confirm,
                                   repeatable=selected.group in ("audio", "display", "media", "workspace", "window")))

        # "search for hermes agent", "search youtube for stone techno", "google cheap flights"
        sites = "|".join(SEARCH_SITES)
        m = re.fullmatch(rf"(search|google|look up)(?: search)?(?: (?:on|in))?(?: the)?(?: ({sites}|web|internet))?(?: for)? (.+)", s)
        if m and m.group(3).strip() not in ("for", "web", "the web", "web for", "the web for", "internet", "online"):   # "search web for" …
            site = SEARCH_SITES.get(m.group(2) or ("google" if m.group(1) == "google" else ""), "")   # "web" -> the browser's engine
            # the query as it was written (keeps "C++", capitals), without the command words
            said = re.match(rf"(?i)\s*(?:search|google|look up)(?:\s+search)?(?:\s+(?:on|in))?(?:\s+the)?(?:\s+(?:{sites}|web|internet))?(?:\s+for)?[\s,:]+(.+)", raw)
            query = (said.group(1) if said else m.group(3)).strip(" .!?")
            if site and site in jev.SITE_SEARCH:
                url = jev.SITE_SEARCH[site].replace("%s", urllib.parse.quote_plus(query))
                return Decision(Action("browser", f"Search {site.replace('_', ' ')} “{query[:40]}”",
                                       {"action": {"type": "navigate_url", "url": url, "query": query}}, repeatable=False))
            return Decision(Action("omnibox", f"Search “{query[:40]}”", {"text": query, "search": True}, repeatable=False))

        # "figure out when the market opens" — a goal, worked towards step by step in the browser
        m = re.fullmatch(rf"(?:{'|'.join(GOAL_PREFIXES)})(?: me)?(?:,)? (.+)", s)
        if m and len(m.group(1).split()) >= 2:
            goal = re.sub(r"(?i)^(?:" + "|".join(GOAL_PREFIXES) + r")(?: me)?[\s,:]+", "", raw.strip(), count=1)
            return Decision(Action("autopilot", f"Goal: {goal.strip(' .!?')[:50]}",
                                   {"goal": goal.strip(" .!?")}, repeatable=False))

        # "go to example.com", "open example dot com" — a plain address, no site list needed
        m = re.fullmatch(r"(?:go to|open|navigate to|take me to|visit)(?: the)? "
                         r"((?:[a-z0-9][a-z0-9-]*)(?:(?:\.| dot )[a-z0-9-]+)+)(?:\.)?", s)
        if m:
            host = re.sub(r"\s+dot\s+", ".", m.group(1)).strip(". ")
            if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", host):
                url = f"https://{host}"
                return Decision(Action("browser", f"Open {url}", {"action": {"type": "navigate_url", "url": url}},
                                       repeatable=False))
        # the dot is lost in normalization, so "example.com" arrives as "example com"
        m = re.fullmatch(rf"(?:go to|open|navigate to|take me to|visit)(?: the)? ([a-z0-9][a-z0-9-]*) ({TLDS})", s)
        if m:
            url = f"https://{m.group(1)}.{m.group(2)}"
            return Decision(Action("browser", f"Open {url}", {"action": {"type": "navigate_url", "url": url}},
                                   repeatable=False))

        # "open chatgpt", "launch spotify", "open two browsers", "open a new terminal"
        m = re.fullmatch(r"(?:open|launch|start|run|bring up|switch to|show me|show)(?: (a|an|one|two|to|too|three|four|five|\d))?"
                         r"( new)?(?: the| my)? (.+?)(?: apps?| applications?| windows?)?", s)
        if m:
            count = 1 if m.group(1) in (None, "a", "an") else (parse_number(m.group(1)) or 1)
            app = self.app_by_name(m.group(3), plural=count > 1)
            if app:
                fresh = count > 1 or bool(m.group(2))
                label = f"Open {app.name}" + (f" ×{count}" if count > 1 else " (new window)" if fresh else "")
                return Decision(Action("launch", label, {"app": app, "count": min(count, 5), "new": fresh}, repeatable=False))
        return None

    def agent_kind(self, name: str) -> str | None:
        """A herdr agent kind from what was said; "" or "agent" means the default kind."""
        name = name.strip()
        installed = [k for k in AGENT_KINDS if shutil.which(k)]
        if name in ("", "agent", "coding"):
            preferred = self.settings.get("default_agent")
            if preferred in installed:
                return preferred
            try:   # the kind the user runs most right now
                there = self.herdr_window(anywhere=True)
                kinds = [a.kind for a in herdr_ctl.Herdr.load(there[1] if there else "default").agents if a.kind in installed]
                if kinds:
                    return max(set(kinds), key=kinds.count)
            except Exception:
                pass
            return installed[0] if installed else None
        scored = sorted(((fuzzy.similarity(name, alias), kind) for kind, aliases in AGENT_KINDS.items()
                         if kind in installed for alias in aliases), reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 80 else None

    # words that name something on screen, never an application
    NOT_APPS = {"settings", "menu", "options", "preferences", "help", "about", "details", "comments", "link",
                "result", "tab", "page", "file", "folder", "image", "video", "message", "email", "chat"}

    def app_by_name(self, name: str, plural: bool = False):
        """An installed app named by speech: sound-alike tolerant ("chatgbt"), or by kind ("browser")."""
        name = name.strip()
        if shutil.which("herdr") and fuzzy.similarity(name, "herdr") >= 78:
            return HERDR_APP   # a terminal program without a desktop entry
        if plural:
            name = re.sub(r"(?<=[a-z])(?:e?s)$", "", name)   # "browsers" -> "browser", "terminals" -> "terminal"
        if not name or name in self.NOT_APPS:
            return None
        if name in ("browser", "web browser", "internet"):
            desktop_id = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True).stdout.strip()
            if app := apps.by_id(desktop_id):
                return app
        if name in ("terminal", "console", "shell"):
            terms = [a for a in apps.installed() if "terminal" in a.generic.lower()]
            running = {w.cls.lower() for w in desktop.snapshot().windows}
            terms.sort(key=lambda a: not any(apps.window_matches(a, c, "") for c in running))
            if terms:
                return terms[0]
        scored = []
        for app in apps.installed():
            label = app.name.lower()
            score = max(fuzzy.similarity(name, label), fuzzy.similarity(name, label.split()[0]) - 4 if " " in label else 0)
            scored.append((score, app))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return None
        best, app = scored[0]
        # several desktop entries with the same name (e.g. an app and its web app): one app, not an ambiguity
        same = [a for sc, a in scored if sc == best and a.name.lower() == app.name.lower()]
        if len(same) > 1:
            running = {w.cls.lower() for w in desktop.snapshot().windows}
            app = next((a for a in same if any(apps.window_matches(a, c, "") for c in running)), same[0])
        rival = next((sc for sc, a in scored[1:] if a.name.lower() != app.name.lower()), 0)
        # clear winner: close match with some lead, or a looser sound-alike ("chatgbt") far ahead of the rest
        if (best >= 86 and best - rival >= 5) or (best >= 78 and best - rival >= 20):
            return app
        return None

    def herdr_window(self, anywhere: bool = False) -> tuple[desktop.Window, str] | None:
        """(window, session) of the herdr terminal the user is working in.

        Normally only the focused window counts (so "next tab" in a browser stays a browser
        command).  anywhere=True finds a herdr window on any workspace — for phrases that
        can only mean herdr ("next agent", "who needs me").
        """
        desk = desktop.snapshot()
        candidates = [desk.focused] + (sorted(desk.windows, key=lambda w: w.focus_rank) if anywhere else [])
        for win in candidates:
            if win is None:
                continue
            if win.pid not in self._herdr_pids:
                self._herdr_pids[win.pid] = herdr_ctl.session_of_window(win.pid)
            if self._herdr_pids[win.pid]:
                return win, self._herdr_pids[win.pid]
        return None

    def key_decision(self, key: str, times: int) -> Decision:
        label = KEYS[key][2] + (f" ×{times}" if times > 1 else "")
        return Decision(Action("key", label, {"key": key, "times": times}))

    def modal(self, s: str) -> Decision | None:
        """Numbers and verbs while hints, a choice, or the grid are on screen."""
        m = re.fullmatch(r"(?:(left |right |middle |double )?click |(?:select|pick|choose|open|option|take|go to) |move to |hover |point at |type into |number )?(?:number |option )?(.+?)", s)
        n = parse_number(m.group(2)) if m else None
        prefix = (m.group(1) or "").strip() if m else ""
        verb_word = s.split()[0] if s else ""
        if self.mode == "grid":
            if n and 1 <= n <= 9:
                return Decision(Action("grid_zoom", f"Grid {n}", {"cell": n}, repeatable=False))
            m2 = re.fullmatch(r"(left |right |middle |double )?click|(move|here|go|point|hover)", s)
            if m2:
                if m2.group(2):
                    return Decision(Action("grid_move", "Move pointer here", repeatable=False))
                kind = (m2.group(1) or "left ").strip()
                return Decision(Action("grid_click", f"{kind.title()} click", {"kind": kind}, repeatable=False))
            if s in ("back", "zoom out", "undo"):
                return Decision(Action("grid", "Mouse grid", {"screen": False}, repeatable=False))
            if s in ("cancel", "close", "stop", "never mind", "exit"):
                return Decision(Action("clear", "Grid closed", repeatable=False))
            return None
        if n and 1 <= n <= len(self.mode_items):
            el = self.mode_items[n - 1]
            if verb_word in ("move", "hover", "point"):
                return Decision(Action("point", f"Point at {el.text[:30] or n}", {"x": el.x, "y": el.y}, repeatable=False))
            if verb_word == "type":
                return None
            kind = prefix or "left"
            return Decision(Action("click", f"Click {el.text[:40] or n}", {"x": el.x, "y": el.y, "kind": kind, "element": el}, repeatable=False))
        if s in ("cancel", "never mind", "hide", "close", "stop", "clear"):
            return Decision(Action("clear", "Hints hidden", repeatable=False))
        return None

    def transcribing(self, hypotheses: list[str]) -> Decision:
        """voxtype is recording: only the stop phrases mean anything; everything else is the passage.

        The phrase is looked for at the *end* of what was heard ("…see you tomorrow, transcribe off"),
        in both recognizers' versions, and across a split ("…transcribe" | "off").
        """
        stop = {p: "off" for p in TRANSCRIBE_OFF} | {p: "cancel" for p in TRANSCRIBE_CANCEL} | {p: "send" for p in TRANSCRIBE_SEND}
        tails = []
        for h in hypotheses:
            words = h.split()
            tails += [" ".join(words[-n:]) for n in (2, 3, 4) if len(words) >= n]
            if self.transcribe_tail and len(words) <= 2:
                tails.append(" ".join((self.transcribe_tail + " " + h).split()[-3:]))
        match = fuzzy.best_match(tails, stop) if tails else None
        # said on its own the stop phrase may be misheard; at the end of a longer sentence only the
        # exact words count ("…when I say only transcribe it" is not "transcribe stop")
        alone = min(len(h.split()) for h in hypotheses) <= 4
        op = match.canonical if match and match.score >= (84 if alone else 95) else None
        if op and match.phrase.startswith("and ") and min(len(h.split()) for h in hypotheses) > 3:
            # "and transcribe" is how Whisper writes "end transcribe" said on its own; at the end of a
            # longer sentence ("we record it and transcribe") it is just text
            op = None
        heard = match.heard if op else ""
        self.transcribe_tail = hypotheses[0].split()[-1] if hypotheses and hypotheses[0] else ""
        if op in ("off", "send") and not self.transcribe_passage and all(len(h.split()) <= 3 for h in hypotheses):
            op = "cancel"   # nothing but the stop phrase was said: there is no text to type
        if op is None:
            self.transcribe_passage += 1
        if op == "off":
            return Decision(Action("voxtype", "Transcription done", {"op": "stop", "said": heard}, repeatable=False), source="voxtype")
        if op == "send":
            return Decision(Action("voxtype", "Transcription sent", {"op": "stop", "said": heard, "submit": True}, repeatable=False), source="voxtype")
        if op == "cancel":
            return Decision(Action("voxtype", "Transcription discarded", {"op": "cancel"}, repeatable=False), source="voxtype")
        return Decision(ignored=True, source="voxtype", hud="")

    def dictation(self, s: str, raw: str) -> Decision:
        if s in ("stop dictation", "end dictation", "dictation off", "stop dictating", "exit dictation"):
            return Decision(Action("mode", "Dictation off", {"mode": ""}, repeatable=False))
        if s in ("new line", "newline", "next line"):
            return self.key_decision("line_break", 1)   # Shift+Enter: a line break even in chat boxes
        if s in ("new paragraph",):
            return self.key_decision("line_break", 2)
        if s in ("scratch that", "delete that", "undo that"):
            return Decision(Action("key", "Scratch that", {"key": "backspace", "times": len(self.last_typed)}, repeatable=False))
        if s in ("press enter", "send", "submit"):
            return self.key_decision("enter", 1)
        text = raw.strip()
        text = re.sub(r"\s+", " ", text)
        return Decision(Action("type", "Dictate", {"text": text + " "}, repeatable=False))

    # -- 2. Jev ---------------------------------------------------------------
    _KIND_FALLBACK = {"foot": "terminal", "kitty": "terminal", "alacritty": "terminal", "helium": "web browser",
                      "chromium": "web browser", "firefox": "web browser", "eu.betterbird.betterbird": "email",
                      "org.gnome.nautilus": "file manager", "spotify": "music player"}

    def kind(self, cls: str) -> str:
        if cls not in self._kinds:
            self._kinds[cls] = self._KIND_FALLBACK.get(cls.lower()) or apps.kind_for_class(cls)
        return self._kinds[cls]

    def build(self, transcript: str, scene: perceive.Scene, pending_label: str):
        desk = scene.desk
        text = " ".join(transcript.split())[-jev.MAX_TRANSCRIPT_CHARS:]
        state: dict = {"transcript": text}
        focused = desk.focused
        state["focused_window"] = focused.id if focused else "none"
        state["windows"] = [desktop.encode_window(w, desk, self.kind(w.cls)) for w in desk.windows]
        if scene.target:
            where = {"web": "web page elements", "a11y": "accessible controls", "ocr": "text read near the mouse"}.get(scene.source, "nothing readable")
            screen = {"window": scene.target.id, "what": where}
            if scene.web:
                screen["page"] = f"{scene.web.get('title', '')[:80]} ({jev.host_of(scene.web.get('url', ''))})"
            screen["elements"] = [perceive.encode_element(e, scene) for e in scene.elements]
            state["screen"] = screen
        if self.recent:
            now = time.time()
            state["recent_actions"] = [{"said": r["said"][:100], "did": r["label"][:80], "seconds_ago": round(now - r["at"])}
                                       for r in reversed(self.recent)]
        if pending_label:
            state["pending_confirmation"] = pending_label
        herdr = self.herdr_window()
        self._jev_herdr = None
        self._agent_messages: list[str] = []
        if herdr:
            try:
                h = herdr_ctl.Herdr.load(herdr[1])
            except Exception:
                h = None
            if h and h.agents:
                self._jev_herdr = (h, herdr)
                state["herdr_agents"] = [a.describe() for a in h.agents]

        text_c = extract_text_candidates(text)
        # "type hello into your name" -> also offer "hello": the destination is an element, not text
        for cand in list(text_c):
            head = re.split(r"\s+(?:into|in|inside|on|to)\s+(?:the\s+|my\s+|your\s+|this\s+)?\S", cand, maxsplit=1)[0].strip()
            if head and head != cand and head.lower() not in (c.lower() for c in text_c):
                text_c.append(head)
        text_c = text_c[:10]
        url_c = extract_url_candidates(text)
        app_c = apps.candidates(text)

        window_criteria = {w.id: None for w in desk.windows}
        window_criteria["none"] = "No particular window is named or described (then the focused window is meant)"
        element_criteria = {e.id: None for e in scene.elements}
        element_criteria["none"] = "The command does not refer to anything in `screen.elements`"
        system_criteria = {a.id: a.description for a in self.catalog.values()}
        system_criteria["none"] = "Not a system control"
        key_criteria = dict(KEY_DESCRIPTIONS)
        key_criteria["none"] = "No key or shortcut is asked for"

        q: dict = {
            "intent": {"type": "choice", "instructions": {
                "question": "What does the user want the computer to do, according to `transcript`?",
                "focus": ("The user controls the whole desktop by voice. `transcript` comes from speech "
                          "recognition and often contains sound-alike errors ('select ride' = 'select right', "
                          "'skoll' = 'scroll'); `also_heard` is a second recognizer's version of the same words — "
                          "trust whichever reads as a sensible command. `windows` lists open windows, "
                          "`screen.elements` what is visible in the window under the mouse, `recent_actions` "
                          "what was just done ('again'/'more' repeat it). Pick none for speech not addressed to "
                          "the computer.")},
                "criteria": INTENTS},
            "window": {"type": "choice", "instructions": {
                "question": "Which window in `windows` does `transcript` refer to (by app name, title, content, position or workspace)?",
                "focus": "Each line starts with the window id. 'this' / 'it' without a name means the focused window: pick none then."},
                "criteria": window_criteria},
            "window_verb": {"type": "choice", "instructions": {
                "question": "Which window arrangement does `transcript` ask for?"}, "criteria": WINDOW_VERBS},
            "direction": {"type": "choice", "instructions": {"question": "Which direction does `transcript` mention?"},
                          "criteria": {"left": "left", "right": "right", "up": "up / above / top", "down": "down / below / bottom", "none": "no direction"}},
            "amount": {"type": "score", "instructions": {"question": "How much does the user want (scrolling, resizing, moving)?"},
                       "criteria": [{"what": "A little / a bit / slightly"}, {"what": "A normal step, nothing specified"}, {"what": "A lot / far / much"}]},
            "key": {"type": "choice", "instructions": {"question": "Which key or keyboard shortcut should be pressed for `transcript`?"},
                    "criteria": key_criteria},
            "system_action": {"type": "choice", "instructions": {"question": "Which system control does `transcript` ask for?"},
                              "criteria": system_criteria},
            "click_kind": {"type": "choice", "instructions": {"question": "What kind of click does `transcript` ask for?"},
                           "criteria": {"left": "A normal click / press / open / select", "right": "Right click / context menu / options",
                                        "double": "Double click", "middle": "Middle click / open in new tab"}},
            "is_command": {"type": "noul", "instructions": {
                "question": "Is `transcript` an instruction or question addressed to the computer?",
                "focus": "Talking to another person, narration, thinking aloud or a stray fragment is not. Asking the computer what is on screen is."},
                "criteria": {"true": {"what": "A command or question for the computer", "examples": ["scroll down", "close this", "open spotify", "click save", "again", "what does it say here", "read this", "which song is playing here"]},
                             "false": {"what": "Not addressed to the computer", "examples": ["I think we should eat", "yeah totally", "hmm let me see"]}}},
            "destructive": {"type": "noul", "instructions": {
                "question": "Would doing `transcript` delete something, send a message, post, buy, pay, submit a form, discard unsaved work, or otherwise be hard to undo?",
                "focus": "Opening, focusing, scrolling, moving, resizing, typing and normal navigation clicks are not."},
                "criteria": {"true": {"what": "Hard to undo"}, "false": {"what": "Reversible"}}},
        }
        if scene.elements:
            q["element"] = {"type": "choice", "instructions": {
                "question": "Which element in `screen.elements` does `transcript` refer to (to click, point at or type into)?",
                "focus": ("Each line starts with its id, then role and visible text, [under mouse]/[next to mouse] and "
                          "its @position in the window. 'this' / 'that' / 'here' mean the element under or next to "
                          "the mouse. Match spoken words to visible text loosely (speech recognition misspells). "
                          "Ordinals (first, second) follow reading order.")},
                "criteria": element_criteria}
        if text_c:
            c = {s: None for s in text_c}
            c["none"] = "Nothing should be typed or searched"
            q["text_span"] = {"type": "choice", "instructions": jev.QUESTIONS["text_span"]["instructions"], "criteria": c}
        if url_c:
            c = {s: None for s in url_c}
            c["none"] = "No web address is mentioned"
            q["url_span"] = {"type": "choice", "instructions": jev.QUESTIONS["url_span"]["instructions"], "criteria": c}
        q["site"] = {"type": "choice", "instructions": jev.QUESTIONS["site"]["instructions"], "criteria": jev.QUESTIONS["site"]["criteria"]}
        if self._jev_herdr:
            h = self._jev_herdr[0]
            crit = {a.id: None for a in h.agents}
            crit["none"] = "No particular agent is named or described"
            q["herdr_agent"] = {"type": "choice", "instructions": {
                "question": "Which coding agent in `herdr_agents` does `transcript` refer to (by project/workspace name, task title, agent kind or state)?",
                "focus": "Each line starts with the agent id. Speech recognition misspells project names; match loosely."},
                "criteria": crit}
            # "tell <agent> to <text>" / "ask <agent> <text>": offer the message itself as a span
            verb = r"(?i)^\s*(?:please\s+)?(?:tell|ask|prompt|instruct|message)\s+"
            messages = []
            if m := re.match(verb + r"(.+?)\s+to\s+(.+)$", text):          # tell the alpha agent to run the tests
                messages.append(m.group(2))
            if m := re.match(verb + r".*?\bagent\b[\s,:]+(.+)$", text):    # ask the alpha agent what it is doing
                messages.append(m.group(1))
            if m := re.match(verb + r"\S+[\s,:]+(.+)$", text):              # ask claude why the build fails
                messages.append(m.group(1))
            self._agent_messages = [m.strip().rstrip(".") for m in messages if m.strip()]
            for cand in reversed(messages):
                cand = cand.strip().rstrip(".")
                if cand and cand not in text_c:
                    text_c.insert(0, cand)
                c = {x: None for x in text_c[:10]}
                c["none"] = "Nothing should be typed or searched"
                q["text_span"] = {"type": "choice", "instructions": jev.QUESTIONS["text_span"]["instructions"], "criteria": c}
        if app_c:
            c = {a.id: f"{a.name}" + (f" ({a.generic})" if a.generic else "") for a in app_c}
            c["none"] = "None of these applications"
            q["app"] = {"type": "choice", "instructions": {"question": "Which application does the user want to open?"}, "criteria": c}
        return state, q, {"text": text_c, "url": url_c}

    def ask_jev(self, transcript: str, spoken: str, pending_label: str, others: list[str] = ()) -> Decision:
        if not load_api_key():
            # everything on this machine keeps working; only open-ended sentences need Jev
            return Decision(hud="Jev isn't set up — run: omarchy-voice key", say="", source="no-jev")
        t0 = time.perf_counter()
        scene = self.current_scene(need_elements=True)
        t1 = time.perf_counter()
        state, questions, cands = self.build(transcript, scene, pending_label)
        if others:
            state["also_heard"] = list(others)[:2]
        try:
            result = jev.decide(state, questions)
        except Exception as exc:
            return Decision(say="I can't reach the decision model.", hud=f"Jev unavailable: {exc}"[:120], source="jev-error")
        answers = result["answers"]
        decision = self.policy(answers, scene, cands, transcript, spoken)
        decision.source = f"jev/{scene.source or 'desk'}"
        decision.cost = float(result.get("cost_usd") or 0)
        intent = answers.get("intent") or {}
        decision.hud = decision.hud or ""
        decision.scene = (f"{scene.source or 'desktop'} · {len(scene.elements)} elements"
                          + (f" · {scene.target.app}" if scene.target else "")
                          + f" · intent {intent.get('choice', '?')} {float(intent.get('confidence', 0)):.2f}")
        decision.ms.update(scene_wait=round((t1 - t0) * 1000), jev=result["latency_ms"], **{k: v for k, v in scene.timings.items() if k.endswith("_ms")})
        return decision

    # -- 3. policy ------------------------------------------------------------
    def policy(self, a: dict, scene: perceive.Scene, cands: dict, transcript: str, spoken: str) -> Decision:
        def choice(q):
            return (a.get(q) or {}).get("choice", "none")

        def conf(q):
            return float((a.get(q) or {}).get("confidence", 0))

        def prob(q):
            ans = a.get(q) or {}
            return float((ans.get("probabilities") or {}).get(ans.get("choice"), ans.get("confidence", 0)))

        intent = choice("intent")
        is_cmd = float((a.get("is_command") or {}).get("noul", 0))
        destructive = float((a.get("destructive") or {}).get("noul", 0)) >= jev.T["destructive"]
        if intent == "none" or is_cmd < jev.T["is_command"]:
            return Decision(ignored=True, hud=f"(not a command) {transcript[:60]}")
        if prob("intent") < 0.5:
            return Decision(hud=f"Not sure: {intent}? ({prob('intent'):.2f})")

        desk = scene.desk
        named = desk.by_id.get(choice("window"))
        if named and prob("window") < 0.45:
            named = None
        deictic = bool(re.search(r"\b(this|it|that|current|here|window)\b", spoken))
        win = named or desk.focused
        window_intents = ("close_window", "move_window_to_workspace", "arrange_window", "swap_window")
        if intent in window_intents and not named and not deictic:
            # something was named that is not an open window ("move spotify…" with no Spotify open)
            return Decision(say="I don't see that window.", hud=f"No open window matches: {transcript[:50]}", source="jev")
        direction = choice("direction")
        score = float((a.get("amount") or {}).get("score", 1.0))
        amount = "little" if score < 0.5 else "lot" if score > 1.5 else "normal"
        element = scene.by_id.get(choice("element"))

        if intent == "confirm":
            return Decision(Action("confirm", "Confirm", repeatable=False))
        if intent == "cancel":
            return Decision(Action("cancel", "Cancel", repeatable=False))
        if intent == "repeat_last":
            return Decision(self.last_action) if self.last_action and self.last_action.repeatable else Decision(say="Nothing to repeat.")

        if intent == "focus_window":
            if not named:
                return self.maybe_launch(a, "Which window?")
            return Decision(Action("window", f"Focus {named.app}", {"verb": "focus", "address": named.address}, repeatable=False))
        if intent == "close_window":
            if not win:
                return Decision(say="Which window?")
            return Decision(Action("window", f"Close {win.app}", {"verb": "close", "address": win.address}, confirm=True, repeatable=False))
        if intent == "move_window_to_workspace":
            ws = workspace_in(spoken)
            if not ws or ws == "empty":
                return Decision(say="Which workspace?")
            return Decision(Action("window", f"Move {win.app if win else 'window'} to workspace {ws}",
                                   {"verb": "move_to_workspace", "address": win.address if win else "", "slots": {"workspace": ws}}))
        if intent == "arrange_window":
            verb = choice("window_verb")
            if verb == "none":
                return Decision(say="How should I arrange it?")
            slots = {"amount": amount}
            if verb.startswith("snap_"):
                slots = {"region": verb.removeprefix("snap_")}
                verb = "snap"
            elif verb in ("half_width", "third_width", "full_width"):
                slots = {"fraction": {"half_width": 0.5, "third_width": 0.333, "full_width": 1.0}[verb]}
                verb = "width"
            label = WINDOW_VERBS.get(choice("window_verb"), verb)
            return Decision(Action("window", f"{label}{' · ' + win.app if named else ''}",
                                   {"verb": verb, "address": win.address if win else "", "slots": slots},
                                   repeatable=verb in ("wider", "narrower", "taller", "shorter")))
        if intent == "swap_window":
            if direction == "none":
                return Decision(say="Which direction?")
            return Decision(Action("window", f"Move window {direction}", {"verb": "swap", "address": win.address if win else "", "slots": {"direction": direction}}))
        if intent == "focus_direction":
            if direction == "none":
                return Decision(say="Which direction?")
            return Decision(Action("focus_dir", f"Focus {direction}", {"direction": direction}))
        if intent == "go_to_workspace":
            ws = workspace_in(spoken)
            if not ws and self._jev_herdr and prob("herdr_agent") >= 0.45:
                # "switch to the gamma workspace": not a numbered desktop, a named herdr workspace
                h, (hwin, session) = self._jev_herdr
                agent = h.by_id(choice("herdr_agent"))
                return Decision(Action("herdr", f"Workspace · {agent.workspace}", {"session": session, "address": hwin.address,
                                                                                  "op": "focus_workspace", "arg": agent.workspace_id}, repeatable=False))
            if not ws:
                return Decision(say="Which workspace?")
            return Decision(Action("workspace", f"Workspace {ws}", {"target": ws}))
        if intent == "show_scratchpad":
            return Decision(Action("scratchpad", "Scratchpad"))

        if intent in ("click", "point_mouse"):
            kind = choice("click_kind") if choice("click_kind") != "none" else "left"
            if intent == "point_mouse" and not element and direction != "none":
                return Decision(Action("nudge", f"Mouse {direction}", {"direction": direction, "amount": amount}))
            if not element:
                if re.search(r"\b(here|there|this|that|it)\b", spoken) or not scene.elements:
                    return Decision(Action("click", f"{kind.title()} click here", {"here": True, "kind": kind}))
                return self.disambiguate(a, scene, "Which one? Say a number.")
            top = self.top_elements(a, scene)
            if prob("element") < jev.T["target_top_prob"] + 0.1 and len(top) > 1:
                return self.disambiguate(a, scene, "Which one? Say a number.")
            if intent == "point_mouse":
                return Decision(Action("point", f"Point at {element.text[:40]}", {"x": element.x, "y": element.y}, repeatable=False))
            return Decision(Action("click", f"{'' if kind == 'left' else kind.title() + ' '}Click “{element.text[:40]}”",
                                   {"x": element.x, "y": element.y, "kind": kind, "element": element},
                                   confirm=destructive or self._risky(element),
                                   repeatable=False))
        if intent == "scroll":
            d = direction if direction != "none" else "down"
            return Decision(Action("scroll", f"Scroll {d}", {"direction": d, "amount": amount}))
        if intent == "type_text":
            text = self.pick_span(a, "text_span", cands["text"])
            if not text:
                return Decision(say="Type what?")
            args = {"text": text}
            if element and element.editable:
                args.update(x=element.x, y=element.y)
            return Decision(Action("type", f"Type “{text[:40]}”", args, repeatable=False))
        if intent == "press_key":
            key = choice("key")
            if key == "none":
                return Decision(say="Which key?")
            return self.key_decision(key, times_in(spoken))

        if intent in ("navigate_url", "search_web"):
            return self.web_intent(intent, a, scene, cands, transcript)

        if intent in ("herdr_focus", "prompt_agent"):
            if not self._jev_herdr:
                return Decision(say="No herdr agents here.")
            h, (hwin, session) = self._jev_herdr
            agent = h.by_id(choice("herdr_agent")) if prob("herdr_agent") >= 0.45 else None
            if not agent:
                return Decision(say="Which agent?")
            base = {"session": session, "address": hwin.address, "arg": agent.pane_id}
            if intent == "herdr_focus":
                return Decision(Action("herdr", f"Agent · {agent.workspace}", {**base, "op": "focus_agent"}, repeatable=False))
            if not re.match(r"(?i)\s*(?:please\s+)?(?:tell|ask|prompt|instruct|message)\b", transcript):
                return Decision(say="Say tell, or ask, and the agent's name.")   # never message an agent by accident
            message = self.pick_span(a, "text_span", cands["text"])
            if not message and self._agent_messages:
                message = self._agent_messages[0]   # "tell X to <message>": the words after "to", split by code
            if not message:
                return Decision(say="What should I tell it?")
            return Decision(Action("herdr", f"Tell {agent.workspace}: “{message[:50]}”", {**base, "op": "prompt", "text": message},
                                   confirm=True, repeatable=False))
        if intent == "launch_app":
            return self.maybe_launch(a, "Which app?")
        if intent == "system_action":
            sid = choice("system_action")
            selected = self.catalog.get(sid)
            if not selected or conf("system_action") < 0.4:
                return Decision(say="Which setting?")
            return Decision(Action("system", selected.label, {"action": selected}, confirm=selected.confirm or destructive,
                                   repeatable=selected.group in ("audio", "display", "media")))
        if intent == "show_hints":
            return Decision(Action("hints", "Show hints", repeatable=False))
        if intent == "show_grid":
            return Decision(Action("grid", "Mouse grid", {"screen": "screen" in spoken}, repeatable=False))
        if intent == "hide_overlay":
            return Decision(Action("clear", "Hide overlay", repeatable=False))
        if intent == "read_screen":
            return Decision(Action("read", "Read screen", {"element": element}, repeatable=False))
        return Decision(say="I can't do that yet.", hud=f"No handler for {intent}")

    def _risky(self, element: perceive.Element) -> bool:
        return bool(re.search(r"\b(delete|remove|send|submit|buy|pay|purchase|order|post|publish|discard|erase|format|uninstall|log ?out|sign ?out)\b", element.text, re.I))

    def top_elements(self, a: dict, scene: perceive.Scene, n: int = 5) -> list[perceive.Element]:
        probs = (a.get("element") or {}).get("probabilities") or {}
        ranked = sorted(((p, k) for k, p in probs.items() if k != "none"), reverse=True)
        by_id = scene.by_id
        return [by_id[k] for p, k in ranked[:n] if p >= 0.08 and k in by_id]

    def disambiguate(self, a: dict, scene: perceive.Scene, prompt: str) -> Decision:
        top = self.top_elements(a, scene)
        if not top:
            return Decision(Action("hints", "Show hints", repeatable=False), say="I'll number what I can see.")
        return Decision(Action("choose", "Which one?", {"items": top}, repeatable=False), say=prompt)

    def pick_span(self, a: dict, q: str, fallback: list) -> str | None:
        ans = a.get(q) or {}
        c = ans.get("choice")
        if c == "none":
            return None
        if c and ans.get("confidence", 0) >= jev.T["span_confidence"]:
            return c
        return fallback[0] if fallback else c

    def web_intent(self, intent: str, a: dict, scene: perceive.Scene, cands: dict, transcript: str = "") -> Decision:
        site = (a.get("site") or {}).get("choice", "none")
        if intent == "navigate_url":
            url_span = self.pick_span(a, "url_span", cands["url"])
            url = to_http_url(url_span) if url_span else jev.SITE_HOME.get(site)
            if url:
                return Decision(Action("browser", f"Open {url}", {"action": {"type": "navigate_url", "url": url}}, repeatable=False))
            m = re.search(r"(?i)\b(?:go to|open|visit|take me to|navigate to)\s+(?:the\s+)?(.+?)[.!?]*$", transcript)
            if site == "other_named_site" and m:
                # a site we have no address for: let the browser resolve the name, as if typed
                return Decision(Action("omnibox", f"Open {m.group(1)[:40]}", {"text": m.group(1)}, repeatable=False))
            return Decision(say="Which site?")
        query = self.pick_span(a, "text_span", cands["text"])
        if not query:
            return Decision(say="Search for what?")
        tpl = jev.SITE_SEARCH.get(site) if site not in ("none", "the_web", "other_named_site") else None
        if not tpl:
            # a plain search: the address bar, so the user's own search engine answers
            return Decision(Action("omnibox", f"Search “{query[:40]}”", {"text": query, "search": True}, repeatable=False))
        url = tpl.replace("%s", urllib.parse.quote_plus(query))
        return Decision(Action("browser", f"Search {site.replace('_', ' ')} “{query[:40]}”",
                               {"action": {"type": "navigate_url", "url": url, "query": query}}, repeatable=False))

    def maybe_launch(self, a: dict, ask: str) -> Decision:
        ans = a.get("app") or {}
        app = apps.by_id(ans.get("choice", "none"))
        if not app or ans.get("confidence", 0) < 0.35:
            return Decision(say=ask)
        return Decision(Action("launch", f"Open {app.name}", {"app": app}, repeatable=False))

    # -- 4. execute ------------------------------------------------------------
    def execute(self, action: Action, said: str = "") -> tuple[bool, str]:
        try:
            ok, msg = self._execute(action)
        except Exception as exc:
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        if ok and action.kind not in ("confirm", "cancel"):
            self.recent.append({"said": said, "label": action.label, "at": time.time()})
            if action.repeatable:
                self.last_action = action
        if action.kind not in ("hints", "grid", "grid_zoom", "choose", "mode", "read", "point", "grid_move", "clear"):
            self.invalidate()  # the screen changed; look again next time
        return ok, msg

    def _window(self, address: str | None) -> desktop.Window | None:
        desk = desktop.snapshot()
        if address:
            return next((w for w in desk.windows if w.address == address), None)
        return desk.focused

    def _warp(self, x: int, y: int) -> None:
        desktop.move_cursor(x, y)
        pointer().jiggle()
        time.sleep(0.025)

    def _click(self, x: int | None, y: int | None, kind: str) -> None:
        if x is not None and y is not None:
            self._warp(x, y)
        else:
            x, y = desktop.cursor()
        button = {"right": "right", "middle": "middle"}.get(kind, "left")
        count = {"double": 2, "triple": 3}.get(kind, 1)
        pointer().click(button, count)
        # feedback only after the click: the overlay must never be on screen while the button goes down
        self.overlay.send(op="flash", x=x, y=y)

    def _execute(self, action: Action) -> tuple[bool, str]:
        k, args = action.kind, action.args
        if self.before_typing and (k in ("type", "key") or (k == "voxtype" and (args.get("op") == "stop" or args.get("prefix")))):
            self.before_typing()
        if k == "system":
            return self.run_system(args["action"])
        if k == "window":
            desk = desktop.snapshot()
            win = next((w for w in desk.windows if w.address == args.get("address")), None) if args.get("address") else desk.focused
            if args["verb"] == "focus":
                if not win:
                    return False, "window not found"
                ok, out = desktop.focus_window(win)
                if ok and not win.contains(*desk.cursor):
                    self._warp(win.x + win.w // 2, win.y + win.h // 2)
                return ok, out
            return desktop.window_verb(args["verb"], win, desk, **args.get("slots", {}))
        if k == "focus_dir":
            return desktop.focus_direction(args["direction"])
        if k == "workspace":
            target = args["target"]
            if target == "empty":
                return desktop.dispatch('hl.dsp.focus({ workspace = "empty" })')
            return desktop.go_workspace(target)
        if k == "scratchpad":
            return desktop.toggle_special("scratchpad")

        if k == "click":
            self.clear_modal()
            if args.get("here"):
                self._click(None, None, args.get("kind", "left"))
            else:
                self._click(args["x"], args["y"], args.get("kind", "left"))
            return True, action.label
        if k == "point":
            self.clear_modal()
            self._warp(args["x"], args["y"])
            self.overlay.send(op="flash", x=args["x"], y=args["y"])
            return True, action.label
        if k == "nudge":
            step = {"little": 25, "normal": 120, "lot": 480}[args["amount"]]
            dx, dy = {"left": (-step, 0), "right": (step, 0), "up": (0, -step), "down": (0, step)}[args["direction"]]
            x, y = desktop.cursor()
            self._warp(x + dx, y + dy)
            return True, action.label
        if k == "scroll":
            desk = desktop.snapshot()
            if not desk.hovered and desk.focused:
                f = desk.focused
                self._warp(f.x + f.w // 2, f.y + f.h // 2)
            notches = {"little": 3, "normal": 8, "lot": 25}[args["amount"]]
            horizontal = args["direction"] in ("left", "right")
            pointer().scroll(notches if args["direction"] in ("down", "right") else -notches, horizontal)
            return True, action.label
        if k == "key":
            mods, key, _ = KEYS[args["key"]]
            desk = desktop.snapshot()
            focused = desk.focused
            if focused and focused.cls.lower() in TERMINALS and args["key"] in ("copy", "paste"):
                mods = "CTRL SHIFT"
            ok = send_keys(mods, key, int(args.get("times", 1)))
            return ok, action.label if ok else f"could not press {key}"
        if k == "type":
            if "x" in args:
                self._click(args["x"], args["y"], "left")
                time.sleep(0.08)
            text = args["text"]
            ok = type_text(text)
            self.last_typed = text
            return ok, action.label if ok else "typing failed"
        if k == "browser":
            win = self.browser_window(start=True)
            if win is None:
                return False, "no browser window"
            if not win.focused:
                desktop.focus_window(win)
            if self.browser and browsers.is_chromium(win.cls) and self.browser.available():
                result = self.browser.execute(args["action"], win.title)
                if result.get("ok"):
                    return True, result.get("outcome", "")
            # no remote debugging (or a non-Chromium browser): the address bar does the same job
            url = args["action"].get("url")
            if not url:
                return False, "this needs a browser with remote debugging"
            return self.omnibox(win, url)
        if k == "omnibox":
            win = self.browser_window(start=True)
            if win is None:
                return False, "no browser window"
            return self.omnibox(win, args["text"])
        if k == "launch":
            app = args["app"]
            desk = desktop.snapshot()
            if app is HERDR_APP:
                there = self.herdr_window(anywhere=True)
                if there:
                    return desktop.focus_window(there[0])
                desktop.exec_detached(["xdg-terminal-exec", "herdr"] if shutil.which("xdg-terminal-exec") else ["foot", "herdr"])
                return True, "herdr"
            running = next((w for w in desk.windows if apps.window_matches(app, w.cls, w.title)), None)
            if args.get("new") or args.get("count", 1) > 1:
                for _ in range(args.get("count", 1)):
                    desktop.exec_detached(apps.launch_argv(app))
                    time.sleep(0.5)
                return True, action.label
            if running:
                if running.workspace_name.startswith("special:"):
                    desktop.window_verb("restore", running, desk)
                return desktop.focus_window(running)
            desktop.exec_detached(apps.launch_argv(app))
            return True, action.label

        if k == "hints":
            scene = self.current_scene(need_elements=True)
            items = scene.elements[:90]
            if not items:
                return False, "nothing readable here — try the grid"
            self.mode, self.mode_items, self.mode_since = "hints", items, time.monotonic()
            self.overlay.send(op="hints", items=[{"label": str(i), "x": e.x, "y": e.y, "w": e.w, "h": e.h} for i, e in enumerate(items, 1)])
            return True, f"{len(items)} hints"
        if k == "choose":
            items = args["items"]
            self.mode, self.mode_items, self.mode_since = "choose", items, time.monotonic()
            self.overlay.send(op="hints", items=[{"label": str(i), "x": e.x, "y": e.y, "w": e.w, "h": e.h} for i, e in enumerate(items, 1)])
            return True, "choose"
        if k == "grid":
            desk = desktop.snapshot()
            win = desk.hovered or desk.focused
            if args.get("screen") or not win:
                self.grid_rect = tuple(float(v) for v in desk.logical_monitor())
            else:
                self.grid_rect = (float(win.x), float(win.y), float(win.w), float(win.h))
            self.mode, self.mode_items, self.mode_since = "grid", [], time.monotonic()
            self.overlay.send(op="grid", rect=list(self.grid_rect))
            return True, "grid"
        if k == "grid_zoom":
            x, y, w, h = self.grid_rect
            n = args["cell"] - 1
            col, row = n % 3, n // 3
            self.grid_rect = (x + col * w / 3, y + row * h / 3, w / 3, h / 3)
            gx, gy, gw, gh = self.grid_rect
            self._warp(int(gx + gw / 2), int(gy + gh / 2))
            if gw < 24 or gh < 24:
                return self._execute(Action("grid_click", "Click", {"kind": "left"}))
            self.overlay.send(op="grid", rect=list(self.grid_rect))
            return True, f"cell {args['cell']}"
        if k == "grid_click":
            x, y, w, h = self.grid_rect
            self.clear_modal()
            self._click(int(x + w / 2), int(y + h / 2), args.get("kind", "left"))
            return True, action.label
        if k == "grid_move":
            x, y, w, h = self.grid_rect
            self.clear_modal()
            self._warp(int(x + w / 2), int(y + h / 2))
            return True, action.label
        if k == "clear":
            self.clear_modal()
            return True, action.label
        if k == "mode":
            self.clear_modal()
            self.mode = args["mode"]
            return True, action.label
        if k == "read":
            return self.read_screen(args.get("element"))
        if k == "herdr":
            return self.run_herdr(args)
        if k == "voxtype":
            op = args["op"]
            self.clear_modal()
            if not shutil.which("voxtype"):
                self.say("Transcription needs voxtype.")
                return False, "voxtype isn't installed — run: omarchy voxtype install"
            if op == "stop" and args.get("said"):
                marker = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-voice" / "voxtype-stop.json"
                import json

                marker.write_text(json.dumps({"said": args["said"], "at": time.time()}))
            self.transcribe_tail = ""
            result = subprocess.run(["voxtype", "record", op], capture_output=True, text=True, timeout=5)
            self.mode = "transcribe" if op == "start" and result.returncode == 0 else ""
            if op == "start":
                self.transcribe_kind = args.get("kind", "long")
            self.transcribe_started = time.monotonic()
            self.transcribe_passage = 0
            if op == "stop" and args.get("submit") and result.returncode == 0:
                threading.Thread(target=self._submit_after_voxtype, name="voxtype-submit", daemon=True).start()
            if op == "start" and args.get("prefix") and result.returncode == 0:
                self.transcribe_passage = 1
                if not type_text(args["prefix"] + " "):
                    return False, "typing failed"
            return result.returncode == 0, (result.stderr or result.stdout).strip() or action.label
        return False, f"unknown action kind {k}"

    def browser_window(self, start: bool = False) -> desktop.Window | None:
        """The browser the user means: the focused one, the one under the mouse, one on screen, any.

        With start=True and no browser open, the user's default browser is launched.
        """
        desk = desktop.snapshot()
        visible = [w for w in desk.windows if browsers.is_browser(w.cls)]
        for win in (desk.focused, desk.hovered):
            if win and browsers.is_browser(win.cls):
                return win
        here = [w for w in visible if w.workspace == desk.active_workspace]
        if here or visible:
            return sorted(here or visible, key=lambda w: w.focus_rank)[0]
        if not start:
            return None
        desktop_id = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True).stdout.strip()
        if not desktop_id:
            return None
        desktop.exec_detached(["uwsm-app", "--", desktop_id] if shutil.which("uwsm-app") else ["gtk-launch", desktop_id])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.3)
            win = next((w for w in desktop.snapshot().windows if browsers.is_browser(w.cls)), None)
            if win:
                time.sleep(0.6)   # let the first window finish mapping before typing into it
                return win
        return None

    def omnibox(self, win: desktop.Window, text: str) -> tuple[bool, str]:
        """Type into the browser's address bar and press Enter — works in every browser.

        A search goes to whatever engine the user chose in the browser.  Delete before
        Enter drops an inline autocompletion ("weather" -> "weather.com") so what was
        said is what gets searched or opened.
        """
        if not win.focused:
            desktop.focus_window(win)
            time.sleep(0.1)
        if self.before_typing:
            self.before_typing()
        endpoint = browsers.cdp_endpoint() if browsers.is_chromium(win.cls) else None
        before = browsers.tab_urls(endpoint) if endpoint else {}
        if not send_keys("CTRL", "l"):
            return False, "could not reach the address bar"
        time.sleep(0.12)                # the address bar selects its text before we type over it
        type_text(text)
        time.sleep(0.05)
        send_keys("", "Delete")
        send_keys("", "Return")
        if before:
            # with remote debugging we can confirm a tab actually went somewhere
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                time.sleep(0.25)
                changed = [u for tid, u in browsers.tab_urls(endpoint).items() if before.get(tid) != u]
                if changed:
                    return True, changed[0]
            # opening the page that is already shown is not a failure
            host = urllib.parse.urlparse(to_http_url(text) if "." in text and " " not in text.strip() else "").hostname
            if host and any(urllib.parse.urlparse(u).hostname in (host, "www." + host) for u in before.values()):
                return True, f"already on {host}"
            return False, "the browser did not navigate"
        return True, text

    def run_herdr(self, args: dict) -> tuple[bool, str]:
        if args["op"] == "new_agent":
            return self.new_agent(args["arg"])
        h = herdr_ctl.Herdr.load(args["session"])
        win = next((w for w in desktop.snapshot().windows if w.address == args.get("address")), None)
        if win and not win.focused:
            desktop.focus_window(win)
        op, arg = args["op"], args.get("arg")
        if op == "step_agent":
            ok, msg, agent = h.step_agent(int(arg))
            return ok, f"{agent.kind} · {agent.workspace} [{agent.status}]" if agent else msg
        if op == "nth_agent":
            n = len(h.agents) if int(arg) == -1 else int(arg)
            if not 1 <= n <= len(h.agents):
                return False, f"there are {len(h.agents)} agents"
            agent = h.agents[n - 1]
            ok, msg = h.focus_agent(agent)
            return ok, f"{agent.kind} · {agent.workspace} [{agent.status}]"
        if op == "focus_agent":
            agent = next((a for a in h.agents if a.pane_id == arg), None)
            if not agent:
                return False, "that agent is gone"
            ok, msg = h.focus_agent(agent)
            return ok, f"{agent.kind} · {agent.workspace} [{agent.status}]"
        if op == "attention":
            agent = h.attention_agent()
            if not agent:
                self.say("All agents are busy.")
                return True, "all agents are working"
            ok, msg = h.focus_agent(agent)
            self.say(f"{agent.workspace}, {agent.status}.")
            return ok, f"{agent.kind} · {agent.workspace} [{agent.status}]"
        if op == "summary":
            text = h.summary()
            self.say(text)
            self.hud("Agents", text[:110], ms=6000)
            return True, text
        if op == "step_tab":
            return h.step_tab(int(arg))
        if op == "step_workspace":
            return h.step_workspace(int(arg))
        if op == "focus_workspace":
            return h.focus_workspace(arg)
        if op == "pane":
            return h.focus_pane(arg)
        if op == "zoom":
            return h.zoom()
        if op == "interrupt":
            pane = h.focused_pane
            return h.send_keys(pane["pane_id"], "esc") if pane else (False, "no focused pane")
        if op == "prompt":
            agent = next((a for a in h.agents if a.pane_id == arg), None)
            if not agent:
                return False, "that agent is gone"
            if agent.status == "blocked":
                return False, f"{agent.workspace} is waiting for an approval — answer it first"
            return h.prompt(agent, args["text"])
        return False, f"unknown herdr op {op}"

    def new_agent(self, kind: str) -> tuple[bool, str]:
        """Start an agent in a new herdr tab; opens herdr first when no window shows it."""
        there = self.herdr_window(anywhere=True)
        session = there[1] if there else "default"
        if there and not there[0].focused:
            desktop.focus_window(there[0])
        if not there:
            desktop.exec_detached(["xdg-terminal-exec", "herdr"] if shutil.which("xdg-terminal-exec") else ["foot", "herdr"])
            time.sleep(1.5)
        self.hud(f"Starting {kind}…", "new herdr tab", tone="busy", ms=20000)

        def run():
            ok, msg = herdr_ctl.Herdr.load(session).start_agent(kind)
            self.hud(f"{'✓' if ok else '✗'} {msg}", tone="ok" if ok else "warn", ms=3000)
        threading.Thread(target=run, name="new-agent", daemon=True).start()   # agents take seconds to boot
        return True, f"starting {kind}"

    def _submit_after_voxtype(self) -> None:
        """"transcribe send": press Enter once voxtype has finished typing the passage."""
        state = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voxtype" / "state"
        deadline = time.monotonic() + 30
        time.sleep(0.3)
        while time.monotonic() < deadline:
            try:
                if state.read_text().strip() == "idle":
                    break
            except OSError:
                break
            time.sleep(0.1)
        time.sleep(0.35)   # the last characters are still on their way to the app
        if self.before_typing:
            self.before_typing()
        send_keys("", "Return")

    def clear_modal(self) -> None:
        if self.mode in ("hints", "choose", "grid"):
            self.mode, self.mode_items = "", []
            self.grid_rect = None
            self.overlay.send(op="clear")

    def run_system(self, selected) -> tuple[bool, str]:
        if selected.detached:
            desktop.exec_detached(list(selected.argv))
            return True, selected.label
        result = subprocess.run(selected.argv, capture_output=True, text=True, timeout=20)
        detail = (result.stderr or result.stdout).strip().splitlines()
        return result.returncode == 0, selected.label if result.returncode == 0 else (detail[-1] if detail else "failed")

    def read_screen(self, element: perceive.Element | None) -> tuple[bool, str]:
        scene = self.current_scene(need_elements=True, max_age=0)
        if element:
            text = element.text
        else:
            cx, cy = scene.desk.cursor
            near = sorted(scene.elements, key=lambda e: e.distance(cx, cy))[:6]
            near.sort(key=lambda e: (e.y // 14, e.x))
            text = ". ".join(e.text for e in near if e.text)
        if not text:
            win = scene.target
            text = f"{win.app}: {win.title}" if win else "Nothing readable here."
        self.say(text[:400])
        return True, text[:120]


WTYPE_MODS = {"CTRL": "ctrl", "SHIFT": "shift", "ALT": "alt", "SUPER": "logo"}


def send_keys(mods: str, key: str, times: int = 1) -> bool:
    """A shortcut through the kernel-level keyboard (every app accepts it); wtype if uinput is unavailable."""
    kb = keyboard.keyboard()
    if kb is not None and kb.shortcut(mods, key, times):
        return True
    return subprocess.run(wtype_keys(mods, key, times), capture_output=True, timeout=10).returncode == 0


def type_text(text: str) -> bool:
    kb = keyboard.keyboard()
    if kb is not None:
        return kb.type(text)
    return subprocess.run(["wtype", "--", text], capture_output=True, timeout=30).returncode == 0


def wtype_keys(mods: str, key: str, times: int = 1) -> list[str]:
    """One wtype call: press the modifiers, tap the key `times` times, release."""
    held = [WTYPE_MODS[m] for m in mods.split() if m in WTYPE_MODS]
    argv = ["wtype"]
    for m in held:
        argv += ["-M", m]
    for i in range(max(1, min(times, 200))):
        argv += ["-k", key]
        if i + 1 < times:
            argv += ["-s", "12"]
    for m in reversed(held):
        argv += ["-m", m]
    return argv


AGENT_KINDS = {   # herdr agent kind -> ways people say it (Whisper spellings included)
    "claude": ("claude", "cloud", "clod", "claud", "clawed", "claude code"),
    "codex": ("codex", "codecs", "code x"),
    "omp": ("oh my pi", "oh my pie", "omp", "o m p", "oh my py"),
    "pi": ("pi", "pie", "pi agent"),
    "opencode": ("open code", "opencode", "open coat"),
    "gemini": ("gemini", "jiminy", "gemini cli"),
    "copilot": ("copilot", "co pilot"), "cursor": ("cursor",), "amp": ("amp",), "grok": ("grok", "grog"),
    "hermes": ("hermes",), "qwen": ("qwen", "quen"), "kimi": ("kimi",), "droid": ("droid",), "cline": ("cline",),
}
HERDR_APP = apps.App(id="herdr", name="herdr", generic="Terminal workspace for coding agents", keywords="", wm_class="")
AGENT_OPS = ("step_agent", "nth_agent", "attention", "summary", "new_agent")   # only herdr has agents: works from any window
HERDR_PHRASES = {
    "next agent": ("step_agent", 1), "previous agent": ("step_agent", -1),
    "select next agent": ("step_agent", 1), "select the next agent": ("step_agent", 1),
    "select previous agent": ("step_agent", -1), "select the previous agent": ("step_agent", -1),
    "go to the next agent": ("step_agent", 1), "go to the previous agent": ("step_agent", -1),
    "last agent": ("nth_agent", -1), "the last agent": ("nth_agent", -1), "select the last agent": ("nth_agent", -1),
    "select last agent": ("nth_agent", -1),
    "who needs me": ("attention", 0), "which agent needs me": ("attention", 0), "next waiting agent": ("attention", 0),
    "next done agent": ("attention", 0), "go to the waiting agent": ("attention", 0), "agent that needs me": ("attention", 0),
    "agent status": ("summary", 0), "agents status": ("summary", 0), "status of the agents": ("summary", 0),
    "what are the agents doing": ("summary", 0), "how are the agents": ("summary", 0),
    "next tab": ("step_tab", 1), "previous tab": ("step_tab", -1),
    "next project": ("step_workspace", 1), "previous project": ("step_workspace", -1),
    "next herdr workspace": ("step_workspace", 1), "previous herdr workspace": ("step_workspace", -1),
    "pane left": ("pane", "left"), "pane right": ("pane", "right"), "pane up": ("pane", "up"), "pane down": ("pane", "down"),
    "zoom": ("zoom", 0), "zoom pane": ("zoom", 0), "unzoom": ("zoom", 0), "maximize pane": ("zoom", 0),
    "interrupt": ("interrupt", 0), "stop agent": ("interrupt", 0), "stop the agent": ("interrupt", 0),
    "interrupt the agent": ("interrupt", 0), "cancel agent": ("interrupt", 0),
}

FAST_KEYS = {
    "enter": "enter", "return": "enter", "submit": "enter", "escape": "escape", "tab": "tab",
    "shift tab": "shift_tab", "backspace": "backspace", "back space": "backspace", "delete": "delete",
    "space": "space", "spacebar": "space", "arrow up": "up", "arrow down": "down", "arrow left": "left",
    "arrow right": "right", "page up": "page_up", "page down": "page_down", "home": "home", "end": "end",
    "copy": "copy", "copy that": "copy", "paste": "paste", "paste that": "paste", "cut": "cut", "cut that": "cut",
    "undo": "undo", "undo that": "undo", "redo": "redo", "select all": "select_all", "save": "save", "save it": "save",
    "find": "find", "new tab": "new_tab", "close tab": "close_tab", "close this tab": "close_tab",
    "reopen tab": "reopen_tab", "next tab": "next_tab", "previous tab": "previous_tab", "refresh": "refresh",
    "reload": "refresh", "zoom in": "zoom_in", "zoom out": "zoom_out", "reset zoom": "zoom_reset",
    "go back": "back", "back": "back", "go forward": "forward", "forward": "forward", "delete word": "delete_word",
    "send": "enter", "send it": "enter", "hit enter": "enter", "submit it": "enter", "press return": "enter",
    **{f"press {w}": f"digit_{n}" for n, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))},
    **{f"press {n}": f"digit_{n}" for n in range(10)},
}

