"""What `omarchy-voice learn` may and may not propose.

The filters matter more than the matching: a correction is a permanent rule about how this
user's speech is read, and a wrong one acts on a throat-clear.

    uv run python tests/learn_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import json
import sys
import tempfile
import time

import learn
import omarchy_voice as sv
from brain import Brain

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))


def history(rows: list[dict]) -> _Path:
    path = _Path(tempfile.mkdtemp()) / "history.jsonl"
    now = time.time()
    path.write_text("\n".join(json.dumps({"at": now, **r}) for r in rows) + "\n")
    return path


def said(text: str, action: str = "") -> dict:
    return {"heard": text, "fast": text, "action": action, "route": "local" if action else "ignored"}


def main() -> int:
    brain = Brain(sv.ACTIONS, sv.exact_match, sv.normalize, {"hud": False, "speak": False, "jev_only": False})
    brain.fuzzy_element = lambda hypotheses: None
    grammar = brain.grammar()

    def decides(text: str) -> bool:
        d = brain.fast_path(text, text) or brain.fuzzy_command([text], threshold=84)
        return bool(d is not None and d.action is not None)

    def find(rows, **kw):
        return learn.proposals(history(rows), grammar, sv.normalize, decides=decides, **kw)

    # a real one: said twice, never acted on, and the command it resembles is one they use
    rows = [said("volume up", "Volume up")] * 3 + [said("volume app")] * 2
    found = find(rows)
    check("a repeated mishearing of a command they use is proposed",
          any(p["heard"] == "volume app" and "volume up" in p["means"] for p in found), str(found))

    # once is an accident
    check("a mishearing heard only once is not proposed",
          not find([said("volume up", "Volume up")] * 3 + [said("volume app")]), "")

    # the dangerous one: "hmm" scores 83% against "home"
    rows = [said("home", "Home")] * 3 + [said("hmm")] * 4 + [said("uh")] * 3
    found = find(rows)
    check("thinking noises are never corrected into commands",
          not any(p["heard"] in ("hmm", "uh") for p in found), str(found))

    # a command they have never used is not what they meant
    rows = [said("scroll down", "Scroll down")] * 3 + [said("screen lock")] * 3
    found = find(rows)
    check("it only proposes commands this user actually uses",
          not any("lock" in p["means"] for p in found), str(found))

    # gaps the engine has since closed must not be frozen into a correction
    rows = [said("open two browsers", "Open browser ×2")] * 2 + [said("open to browsers")] * 3
    found = find(rows)
    check("what the engine understands today is left alone",
          not any(p["heard"] == "open to browsers" for p in found), str(found))

    # dictation is not command speech
    rows = [said("scroll down", "Scroll down")] * 3 + [
        {"heard": "scroll down the page please", "fast": "", "action": "", "route": "voxtype"}] * 3
    check("what was dictated is not mined for commands",
          not any("scroll down the page" in p["heard"] for p in find(rows)), "")

    # round trip through the file the engine reads
    path = _Path(tempfile.mkdtemp()) / "corrections.json"
    learn.save(path, {"volume app": "volume up"})
    check("accepted corrections survive a round trip", learn.load(path) == {"volume app": "volume up"})

    print(f"\n{7 - failures}/7 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
