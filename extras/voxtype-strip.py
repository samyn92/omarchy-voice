#!/usr/bin/python3
"""voxtype post-process filter: drop the spoken stop phrase from the end of a transcription.

When voice control ends a voxtype recording ("transcribe off"), those words are
still in voxtype's audio.  voxtype pipes its text through this script before
typing it; anything else passes through unchanged.
"""

import json
import os
import re
import sys
import time

STOP = re.compile(
    r"[\s,]*\b(?:(?:ok(?:ay)?|alright|so|and)[\s,]+)?"
    r"(?:transcri(?:be|ption|bing)[\s,]+(?:(?:off[\s,]+)?and[\s,]+)?(?:off|of|stop|stopped|done|finished|finish|end|ended|over|complete|send|submit|enter)"
    r"|send[\s,]+(?:the[\s,]+)?transcri(?:be|ption)"
    r"|(?:stop|end|finish|done|exit|quit)[\s,]+(?:the[\s,]+)?transcri(?:be|bing|ption)(?:[\s,]+(?:and[\s,]+)?(?:send|submit|enter))?)"
    r"[\s.!?,]*$",
    re.IGNORECASE,
)

STOP_WORDS_TAIL = re.compile(
    r"(?:[\s,]*\b(?:transcri(?:be|ption|bing)|off|of|stop|stopped|done|finished|finish|end|ended|send|submit|and)[.!?,]*){1,5}\s*$",
    re.IGNORECASE,
)
MARKER = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "omarchy-voice", "voxtype-stop.json")


def said_by_voice_control() -> str:
    """The exact words voice control heard as the stop phrase (fresh, one-shot)."""
    try:
        with open(MARKER) as f:
            marker = json.load(f)
        os.unlink(MARKER)
    except (OSError, ValueError):
        return ""
    return marker.get("said", "") if time.time() - marker.get("at", 0) < 60 else ""


def strip(text: str, said: str) -> str:
    out = text
    # 1. the explicit phrase, possibly said twice ("…Transcribe off. Transcribe off.")
    while True:
        new = STOP.sub("", out)
        if new == out:
            break
        out = new
    # 2. voice control ended this session: its stop words, however voxtype split or misheard
    #    them ("…short. Transcribe. Auth."), are commands, not text — applied exactly once
    if said:
        new = STOP_WORDS_TAIL.sub("", out)
        cut = [m.start() for m in re.finditer(r"\btranscri", new, re.IGNORECASE)]
        if cut and len(new) - cut[-1] <= 28:
            new = new[: cut[-1]]
        elif len(said.split()) == 1 and new == text:
            # a lone stop word that voxtype spelled differently is still the last word
            new = re.sub(r"[\s,]*\b[\w']{1,6}[.!?,]*\s*$", "", new)
        out = new
    return out.rstrip(" ,") if out != text else text


if __name__ == "__main__":
    raw = sys.stdin.read()
    said = said_by_voice_control()
    result = strip(raw, said)
    try:  # what voxtype got back, for the voice-control trace
        with open(os.path.join(os.path.dirname(MARKER), "strip.log"), "a") as log:
            log.write(json.dumps({"at": time.time(), "in": raw, "said": said, "out": result}) + "\n")
    except OSError:
        pass
    sys.stdout.write(result)
