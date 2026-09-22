"""Sound-alike matching of what Whisper heard against what the system can do.

Speech recognizers fail in sound-alike ways: "select right" comes back as
"Select Ride" / "Select Write", "scroll down" as "Skoll down".  Each hypothesis
(the small and the large Whisper transcript) is compared with every known
command phrase by spelling *and* by Metaphone code; a clear winner is mapped
to the canonical phrase the fast path understands.  Screen elements get the
same treatment for "click / select / press <text>".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import jellyfish
from rapidfuzz import fuzz

NUMBERS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
           "eighteen", "nineteen", "twenty"]
DIRECTIONS = ("left", "right", "up", "down")
ELEMENT_VERB = re.compile(r"^(?:please )?(?:click|select|press|tap|hit|choose|pick|open)(?: on)?(?: the)? (.+?)(?: button| link| tab| icon)?$")


@lru_cache(maxsize=4096)
def sound(text: str) -> str:
    return " ".join(jellyfish.metaphone(w) or w for w in text.split())


def similarity(heard: str, phrase: str) -> float:
    """0–100: spelling and sound, penalizing a different number of words."""
    spelled = fuzz.ratio(heard, phrase)
    sounded = fuzz.ratio(sound(heard), sound(phrase))
    score = 0.4 * spelled + 0.6 * sounded
    score -= 12 * abs(len(heard.split()) - len(phrase.split()))
    return score


@dataclass
class Match:
    heard: str
    phrase: str
    canonical: str
    score: float
    margin: float


def best_match(hypotheses: list[str], grammar: dict[str, str]) -> Match | None:
    """Best (hypothesis, phrase) pair; margin is the gap to the best phrase with a different meaning."""
    best: Match | None = None
    for heard in hypotheses:
        if not heard or len(heard.split()) > 6:
            continue
        ranked = sorted(((similarity(heard, p), p) for p in grammar), reverse=True)
        top_score, top_phrase = ranked[0]
        rival = next((s for s, p in ranked[1:] if grammar[p] != grammar[top_phrase]), 0.0)
        cand = Match(heard, top_phrase, grammar[top_phrase], top_score, top_score - rival)
        if best is None or cand.score > best.score:
            best = cand
    return best


def element_target(spoken: str) -> str | None:
    m = ELEMENT_VERB.match(spoken)
    if not m:
        return None
    target = m.group(1).strip()
    return target if len(target) >= 3 else None


def element_score(target: str, text: str) -> float:
    t = re.sub(r"[^a-z0-9äöüß ]+", " ", text.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return 0.0
    whole = fuzz.ratio(target, t)
    # a short spoken name inside a longer label: "sign in" in "Sign in to GitHub" — compared
    # against whole-word windows, so "text" never matches inside "context"
    words, n = t.split(), len(target.split())
    windows = [" ".join(words[i:i + n]) for i in range(max(1, len(words) - n + 1))] if len(words) > n else []
    part = max((fuzz.ratio(target, w) for w in windows), default=0) - 6
    sounded = fuzz.ratio(sound(target), sound(t[: len(target) + 8]))
    return max(whole, part, 0.95 * sounded)


def best_elements(target: str, elements, limit: int = 4) -> list[tuple[float, object]]:
    scored = sorted(((element_score(target, e.text), i, e) for i, e in enumerate(elements) if e.text), reverse=True, key=lambda x: (x[0], -x[1]))
    return [(s, e) for s, _, e in scored[:limit]]
