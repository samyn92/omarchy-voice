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


def main() -> int:
    brain = Brain(sv.ACTIONS, sv.exact_match, sv.normalize, {"hud": False, "speak": False, "jev_only": False})
    brain.fuzzy_element = lambda hypotheses: None   # depends on what is on screen; covered by the e2e tests
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
    total = len(CASES) + len(IN_SESSION)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
