"""Offline regression test: what real speech (as Whisper wrote it) must turn into.

Every case here was once said by a user and handled wrongly.  Runs without a
microphone and without Jev — only decisions made on this machine are checked
(fast path, sound-alike matching); None means "must not be acted on locally".

    uv run python phrases_test.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import sys

import omarchy_voice as sv
from brain import Brain

# (small-model transcript, large-model transcript, expected action label substring or None)
CASES = [
    # sound-alike mishearings of commands
    ("Squirt on.", "Skoll down.", "Scroll down"),
    ("Select round.", "Select Ride.", "Focus right"),
    ("Select right.", "Select Write.", "Focus right"),
    ("Select lift.", "Select lift.", "Focus left"),
    ("Scroll dawn a bid", "Scroll down a bit.", "Scroll down"),
    ("Volume op", "Volume up.", "Volume up"),
    ("pace", "Paste.", "Paste"),
    # phrasings people actually use
    ("Go to second workspace.", "Go to second workspace.", "Workspace 2"),
    ("Switch to the 3rd desktop", "Switch to the 3rd desktop", "Workspace 3"),
    ("Expand.", "Expand.", "Wider"),
    ("Expand window", "Expand window", "Wider"),
    ("Open ChatGBT", "Open ChatGBT", "Open ChatGPT"),
    ("Open two browsers.", "Open to browsers.", "×2"),
    ("Search for Hermes agent.", "Search for Hermes agent.", "Search “Hermes agent”"),
    ("Search web for flights", "Search web for flights to Zurich", "Search “flights to Zurich”"),
    ("search youtube for stone techno", "search youtube for stone techno", "Search youtube"),
    ("New Claude.", "New Claude.", "New claude agent"),
    ("New oh my pie", "New oh my pie", "New omp agent"),
    ("New open code.", "New open code.", "New opencode agent"),
    ("Select next agent.", "Select next agent.", "Select next agent"),
    ("Select the third agent.", "Select the thirt agent.", "Select the third agent"),
    ("transcribe", "Transcribe.", "Transcribe (quick)"),
    ("start transcribe", "Start transcribe.", "Transcribe (long)"),
    ("start transcribing", "Start transcribing.", "Transcribe (long)"),
    ("Hit Enter.", "Hit enter.", "Enter"),
    # phrasings that used to need Jev (found with `omarchy-voice misses` / the history)
    ("Select above.", "Select above.", "Focus up"),
    ("Select below.", "Select below.", "Focus down"),
    ("Go to example.com.", "Go to example.com.", "Open https://example.com"),
    ("go to example dot com", "Go to example dot com.", "Open https://example.com"),
    ("Google search for Hermes agent.", "Google search for Hermes agent.", "Search google “Hermes agent”"),
    ("Double press escape.", "Double press escape.", "Escape ×2"),
    ("what is under the mouse", "What is under the mouse?", "Read screen"),
    ("what is this page about", "What is this page about?", "Read screen"),
    ("figure out when the market opens", "Figure out when the market opens.", "Goal: when the market opens"),
    ("autopilot book a table for two", "Autopilot book a table for two.", "Goal: book a table for two"),
    # what recognizers actually wrote for real speech (tools/asr_bench.py, recorded voice)
    ("And do.", "I do.", "Undo"),
    ("Pace", "Peace.", "Paste"),
    ("Select or", "Select on", "Select all"),
    ("Work space three.", "Workspace three.", "Workspace 3"),
    ("halfworth.", "half worth", "Half"),
    ("Move this window to the Force workspace", "Move this window to the fourth workspace", "Move to workspace 4"),
    ("Fourth screen", "Full stream", "fullscreen"),
    ("scrolling down", "Scrolling down.", "Scroll down"),
    # a mishearing that must stay unacted: "play" comes out as "Hello!", which is chatter
    ("Hello!", "Helly", None),
    # never acted on locally: chatter, fragments, unfinished sentences
    ("Alright.", "It's alright.", None), ("Hello Hello", "Hello, hello.", None), ("okay", "Okay.", None),
    ("right", "Right.", None), ("go on", "Go on.", None), ("close it", "Close it.", None),
    ("search", "Search.", None), ("search web for", "Search web for", None), ("Thank you.", "Thank you.", None),
    ("select text", "Select text.", None), ("open the settings", "open the settings", None),
    ("new banana agent", "new banana agent", None), ("Transcribe off.", "Transcribe off.", None),
    ("End transcribe.", "End transcribe.", None),   # nothing to end outside a session
]


# inside a transcription session: (what was said, expected op or None = part of the text)
IN_SESSION = [
    ("End transcribe.", "stop"), ("And transcribe.", "stop"), ("see you tomorrow, end transcribe", "stop"),
    ("End transcribe and send.", "stop"), ("Cancel transcription.", "cancel"),
    ("Please transcribe the meeting notes.", None), ("Transcribe.", None), ("We record it and transcribe.", None),
    ("Now I want to end the call.", None), ("Send it to the team.", None), ("Stop.", None), ("Done.", None),
    ("Still when I say only transcribe it.", None), ("Transcribe off.", "stop"),
]


# in front of an app that can be operated (a stand-in Hermes, with an "Open settings" button)
APP_CASES = [
    ("go to settings and find dark mode", "Goal in Hermes"),
    ("open settings", "Goal in Hermes"),                    # its own button, not the Settings app
    ("find where to enable dark mode", "Goal in Hermes"),
    ("in this app, go to settings", "Goal in this app"),
    ("in hermes, go to settings and find the archived chats", "Goal in hermes"),
    ("in hermes go to settings", "Goal in hermes"),
    ("in the morning I will call him", None),               # "in" that names no open app is just speech
    ("open spotify", "Open Spotify"),                       # another app is still another app
    ("give voice access to signal", "Voice access for Signal"),
    ("scroll down", "Scroll down"),                         # commands stay commands
    ("go to the second workspace", "Workspace 2"),
]


class _Hermes:
    cls = title = "Hermes"


class _HermesBridge:
    def snapshot_for(self, *a, **k):
        return {"elements": [{"text": "Open settings"}, {"text": "New session"}]}


def main() -> int:
    brain = Brain(sv.ACTIONS, sv.exact_match, sv.normalize, {"hud": False, "speak": False, "jev_only": False})
    brain.fuzzy_element = lambda hypotheses: None   # depends on what is on screen; covered by the e2e tests
    brain.operable_app = lambda: None               # which window is focused must not change these answers
    failures = 0
    for fast, accurate, expected in CASES:
        spoken = sv.normalize(accurate)
        others = [o for o in [sv.normalize(fast)] if o != spoken]
        decision = brain.fast_path(spoken, accurate)
        if decision is None or decision.action is None:
            for other in others:
                alt = brain.fast_path(other, fast)
                if alt is not None and alt.action is not None:
                    decision = alt
                    break
        if decision is None or decision.action is None:
            decision = brain.fuzzy_command([spoken, *others], threshold=84) or decision
        got = decision.action.label if decision is not None and decision.action is not None else None
        ok = (expected is None and got is None) or (expected is not None and got is not None and expected in got)
        failures += not ok
        print(("ok    " if ok else "FAIL  ") + f"{accurate!r:40} -> {got}")
    for said, expected in IN_SESSION:
        brain.mode, brain.transcribe_passage, brain.transcribe_tail = "transcribe", 1, ""
        d = brain.transcribing([sv.normalize(said)])
        got = d.action.args.get("op") if d.action else None
        ok = got == expected
        failures += not ok
        print(("ok    " if ok else "FAIL  ") + f"[transcribing] {said!r:34} -> {got or 'text'}")
    brain.mode = ""
    front = (_Hermes(), _HermesBridge(), "hermes")
    brain.operable_app = lambda: front
    brain._app_open = lambda name: name in ("hermes", "this app", "here")
    for said, expected in APP_CASES:
        d = brain.fast_path(sv.normalize(said), said)
        got = d.action.label if d is not None and d.action is not None else None
        ok = (expected is None and (got is None or "Goal" not in got)) or (got is not None and expected is not None and expected in got)
        failures += not ok
        print(("ok    " if ok else "FAIL  ") + f"[in Hermes] {said!r:36} -> {got}")
    total = len(CASES) + len(IN_SESSION) + len(APP_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
