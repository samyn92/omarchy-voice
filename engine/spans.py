"""Candidate extraction (code, not Jev). Jev never generates text: we over-generate
candidate spans from the transcript here, and Jev only *picks* one.
The chosen option is copied verbatim into the browser."""

import re

TLDS = "com|org|net|io|ai|dev|co|edu|gov|de|uk|us|app|xyz|info|me|tv|ch|at|fr|nl|es|it"

FILLER_RE = re.compile(r"\b(please|thanks|thank you|now|okay|ok|um|uh|and then)\b", re.I)

TEXT_VERBS = [
    re.compile(r"\b(?:search|look)\s+(?:for|up)\s+", re.I),
    re.compile(
        r"\bsearch\s+(?:on\s+)?(?:google|duckduckgo|wikipedia|youtube|github|amazon|reddit|twitter|x|hacker news|the web)\s+for\s+",
        re.I,
    ),
    re.compile(r"\bsearch\s+", re.I),
    re.compile(r"\bgoogle\s+", re.I),
    re.compile(r"\bfind\s+", re.I),
    re.compile(r"\btype\s+(?:in\s+)?", re.I),
    re.compile(r"\benter\s+", re.I),
    re.compile(r"\bwrite\s+", re.I),
    re.compile(r"\bput\s+", re.I),
    re.compile(r"\bfill\s+(?:in\s+)?", re.I),
]

TRAILING_DEST_RE = re.compile(
    r"\s+(?:in|into|on|inside|to)\s+(?:the\s+)?(?:[\w-]+\s+){0,4}?"
    r"(?:box|field|input|bar|form|textarea|search|wikipedia|youtube|google|duckduckgo|github|amazon|reddit|twitter|x|web)\b.*$",
    re.I,
)

LEADING_SITE_RE = re.compile(
    r"^(?:on\s+|in\s+)?(?:google|duckduckgo|wikipedia|youtube|github|amazon|reddit|twitter|x|hacker news|the web)\s+(?:for\s+)?",
    re.I,
)

URL_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(" + TLDS + r")\b", re.I)

SPOKEN_DOT_RE = re.compile(r"\b([a-z0-9-]+)\s+dot\s+(" + TLDS + r")\b", re.I)

NUMBER_WORDS = {
    "one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def clean_transcript(text) -> str:
    return " ".join(str(text or "").split()).strip()


def strip_filler(s: str) -> str:
    return re.sub(r"\s+", " ", FILLER_RE.sub(" ", s)).strip().rstrip(".,!?")


def push_unique(lst, value):
    v = strip_filler(value)
    if not v or len(v) > 120:
        return
    if any(x.lower() == v.lower() for x in lst):
        return
    lst.append(v)


def extract_text_candidates(transcript) -> list:
    t = clean_transcript(transcript)
    if not t:
        return []
    out = []

    for m in re.finditer(r'["\u201c\u201d\']([^"\u201c\u201d\']{1,120})["\u201c\u201d\']', t):
        push_unique(out, m.group(1))

    verb_matches = sorted(
        (m for m in (p.search(t) for p in TEXT_VERBS) if m),
        key=lambda m: (m.start(), -len(m.group(0))),
    )
    for m in verb_matches:
        tail = t[m.end():]
        tail = LEADING_SITE_RE.sub("", tail)
        stripped = TRAILING_DEST_RE.sub("", tail)
        push_unique(out, stripped)
        if stripped != tail:
            push_unique(out, tail)

    low = t.lower()
    for_idx = low.find(" for ")
    if for_idx >= 0:
        push_unique(out, TRAILING_DEST_RE.sub("", t[for_idx + 5:]))

    first_space = t.find(" ")
    if first_space > 0:
        push_unique(out, t[first_space + 1:])

    return out[:8]


def extract_url_candidates(transcript) -> list:
    t = clean_transcript(transcript)
    if not t:
        return []
    out = []
    for m in SPOKEN_DOT_RE.finditer(t):
        out.append(f"{m.group(1)}.{m.group(2)}")
    for m in URL_RE.finditer(t):
        out.append(m.group(0))
    return list(dict.fromkeys(out))[:4]


def spoken_number(transcript):
    t = clean_transcript(transcript).lower()
    for word, n in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            return n
    return None


def to_http_url(domain: str) -> str:
    d = (domain or "").strip().rstrip(".")
    if not d:
        return ""
    return d if d.startswith("http") else f"https://{d}"