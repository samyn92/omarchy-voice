"""Learn how this microphone, this room and this voice come out in writing.

Every recognizer mishears the same words the same way for the same person: "undo" arrives
as "And do", "paste" as "Peace".  The history already records what was heard and whether
anything happened, so the corrections can be read out of it rather than guessed.

A correction is proposed only when the mishearing is close to a real command, happened more
than once, and was never acted on — three filters that keep ordinary speech out of it.  The
user sees the list and decides; nothing is applied on its own.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import fuzzy

MIN_SCORE = 68.0          # below this it is a different sentence, not a mishearing
DECIDED_SCORE = 84.0      # at or above this the sound-alike matching already handles it
MAX_WORDS = 6             # corrections are for commands, not for dictated sentences

# Noises a person makes while thinking.  They sound like something to a recognizer — "hmm"
# scores 83% against "home" — and correcting them would act on a throat-clear.
CHATTER = {"hmm", "hm", "mm", "mhm", "uh", "um", "ah", "oh", "eh", "huh", "yeah", "yep", "okay",
           "ok", "right", "sure", "well", "so", "and", "the", "what", "hello", "hi", "thanks",
           "thank you", "bye", "wow", "nice", "good", "cool", "yes", "no"}


def _rows(history: Path, hours: float | None):
    cutoff = time.time() - hours * 3600 if hours else 0
    try:
        lines = history.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("test") or float(row.get("at", 0)) < cutoff:
            continue
        out.append(row)
    return out


def proposals(history: Path, grammar: dict[str, str], normalize, hours: float | None = None,
              min_count: int = 2, decides=None) -> list[dict]:
    """What this user says that the engine keeps failing to understand.

    `grammar` maps a phrase the engine knows to the label it produces, exactly as the
    sound-alike matcher uses it.  `decides(text)` answers what the engine would do with that
    text *today*: history is full of gaps that later releases already closed, and correcting
    those would override working behaviour — "open to browsers" is understood as "open two
    browsers" now, and a correction to "open browser" would quietly drop the second one.
    """
    heard_counts: Counter[str] = Counter()
    matches: dict[str, tuple[str, float]] = {}
    examples: dict[str, list[str]] = defaultdict(list)

    rows = _rows(history, hours)
    # What this user actually says.  A correction may only point at a command they have used
    # before — otherwise "hmm" becomes "home" for someone who has never once said "home".
    used: Counter[str] = Counter()
    for row in rows:
        if row.get("action"):
            spoken = normalize(str(row.get("heard", "")))
            if spoken:
                used[spoken] += 1
    known = set(used)

    for row in rows:
        acted = bool(row.get("action"))
        route = str(row.get("route", ""))
        if acted or route in ("voxtype", "autopilot"):
            continue                                  # it worked, or it was dictation
        for text in {str(row.get("heard", "")), str(row.get("fast", ""))}:
            spoken = normalize(text)
            if not spoken or len(spoken.split()) > MAX_WORDS:
                continue
            if spoken in CHATTER or all(w in CHATTER for w in spoken.split()):
                continue                              # a noise, not a missed command
            match = fuzzy.best_match([spoken], grammar)
            if not match or match.score < MIN_SCORE or match.score >= DECIDED_SCORE:
                continue                              # nothing like it, or already handled
            if spoken == match.phrase or match.phrase not in known:
                continue                              # never said that command: not a mishearing of it
            if decides is not None and decides(spoken):
                continue                              # the engine handles it now; the gap is closed
            heard_counts[spoken] += 1
            best = matches.get(spoken)
            if best is None or match.score > best[1]:
                matches[spoken] = (match.phrase, match.score)
            if text not in examples[spoken]:
                examples[spoken].append(text)

    out = []
    for spoken, count in heard_counts.most_common():
        if count < min_count:
            continue
        phrase, score = matches[spoken]
        out.append({"heard": spoken, "means": phrase, "times": count,
                    "score": round(score, 1), "examples": examples[spoken][:3]})
    return out


def load(path: Path) -> dict[str, str]:
    """The corrections this user has accepted."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(data, dict)}


def save(path: Path, corrections: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(corrections, indent=1, sort_keys=True) + "\n")
