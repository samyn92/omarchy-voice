"""Jev decision engine for voice browser control.

One request per transcript update: state + a speculative fan-out of typed
questions, answered in parallel. Jev never generates text — candidates for
typed text / URLs are extracted by code (spans.py) and Jev only *picks*.

Questions and thresholds are ported from the MIT-licensed reference
implementation (moritzkremb/jev-voice-browser), tuned on jev-1.13.0.
"""

import os
import time
from urllib.parse import urlparse

import httpx

MODEL = os.environ.get("JEV_VOICE_MODEL", "~typesafe/jev-latest")
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
PRICE_PER_M_INPUT_TOKENS_USD = 0.042

MAX_ELEMENTS = 100
MAX_ELEMENT_TEXT = 60
MAX_TRANSCRIPT_CHARS = 400
MAX_CONTEXT_ACTIONS = 3

T = {
    "intent_confidence": 0.55,
    "complete": 0.60,
    "is_command": 0.50,
    "destructive": 0.50,
    "target_confidence": 0.45,
    "target_top_prob": 0.35,
    "span_confidence": 0.35,
    "candidate_count": 3,
    "correction": 0.60,
}

TARGET_INTENTS = {"click_element", "type_into_field", "select_option"}
PAYLOAD_INTENTS = {"search_web", "type_into_field", "select_option"}

INTENT_CRITERIA = {
    "navigate_url": {
        "what": "Open a specific website or URL by name (go to / open / visit / take me to <site>)",
        "not_for": "Searching for a topic; clicking something already on the page",
        "examples": ["go to wikipedia", "open youtube", "take me to github.com", "visit example dot com"],
    },
    "search_web": {
        "what": "Search for a topic or phrase (search for / look up / google / find <query>), on the web or on a named site",
        "not_for": "Typing into a specific named field without searching; opening a site's homepage",
        "examples": ["search for alan turing", "look up typesafe jev", "google cheap flights", "search wikipedia for cats"],
    },
    "click_element": {
        "what": "Click / press / open / select / choose a link, button, tab, result or item that is on the current page",
        "not_for": "Opening a website by name; typing text",
        "examples": ["click the first result", "click sign in", "open the second link", "press the more information link"],
    },
    "type_into_field": {
        "what": "Type or enter specific text into an input box, search box or text field on the page",
        "not_for": "Running a search on a search engine (that is search_web); pressing enter alone",
        "examples": ["type hello world into the search box", "enter my email", "write good morning in the comment box"],
    },
    "select_option": {
        "what": "Choose an option from a dropdown / select menu",
        "not_for": "Clicking a link or button",
        "examples": ["select english from the language dropdown", "choose the large size"],
    },
    "press_enter": {
        "what": "Press the Enter / Return key, or submit what was typed",
        "not_for": "Typing text; clicking a named button",
        "examples": ["press enter", "hit enter", "submit"],
    },
    "scroll_down": {
        "what": "Scroll / move down the page",
        "not_for": "Scrolling up; navigating",
        "examples": ["scroll down", "scroll down a bit", "go to the bottom", "page down"],
    },
    "scroll_up": {
        "what": "Scroll / move up the page",
        "not_for": "Scrolling down",
        "examples": ["scroll up", "back to the top", "page up"],
    },
    "go_back": {
        "what": "Go back to the previous page in history (back / go back / undo that / previous page)",
        "not_for": "Scrolling up; closing a tab",
        "examples": ["go back", "undo", "back", "previous page"],
    },
    "go_forward": {
        "what": "Go forward in history",
        "not_for": "Scrolling down",
        "examples": ["go forward", "forward"],
    },
    "reload": {
        "what": "Reload / refresh the current page",
        "not_for": "Navigating elsewhere",
        "examples": ["reload", "refresh the page"],
    },
    "open_new_tab": {
        "what": "Open a new empty tab",
        "not_for": "Opening a website by name in the current tab",
        "examples": ["open a new tab", "new tab"],
    },
    "close_tab": {
        "what": "Close the current tab",
        "not_for": "Going back",
        "examples": ["close this tab", "close tab"],
    },
    "switch_tab": {
        "what": "Switch to another / the next / the previous tab",
        "not_for": "Opening or closing tabs",
        "examples": ["next tab", "switch tab", "go to the other tab"],
    },
    "confirm": {
        "what": "Approve a pending action the browser asked to confirm (yes / confirm / do it / go ahead)",
        "not_for": "New commands",
        "examples": ["confirm", "yes do it", "go ahead"],
    },
    "cancel": {
        "what": "Cancel / never mind / stop the pending action",
        "not_for": "Going back in history",
        "examples": ["cancel", "never mind", "stop"],
    },
    "none": {
        "what": "Not a browser command, or nothing recognizable yet (fragment, chit-chat, silence, filler)",
        "not_for": "Anything that clearly matches another option",
        "examples": ["um", "okay so", "what do you think", "the weather is nice"],
    },
}

SITE_HOME = {
    "google": "https://www.google.com/",
    "duckduckgo": "https://duckduckgo.com/",
    "youtube": "https://www.youtube.com/",
    "wikipedia": "https://en.wikipedia.org/wiki/Main_Page",
    "github": "https://github.com/",
    "amazon": "https://www.amazon.com/",
    "reddit": "https://www.reddit.com/",
    "twitter_x": "https://x.com/",
    "hacker_news": "https://news.ycombinator.com/",
    "example_com": "https://example.com/",
}

SITE_SEARCH = {
    "google": "https://www.google.com/search?q=%s",
    "duckduckgo": "https://duckduckgo.com/?q=%s",
    "the_web": "https://duckduckgo.com/?q=%s",
    "youtube": "https://www.youtube.com/results?search_query=%s",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=%s",
    "github": "https://github.com/search?q=%s&type=repositories",
    "amazon": "https://www.amazon.com/s?k=%s",
    "reddit": "https://www.reddit.com/search/?q=%s",
    "twitter_x": "https://x.com/search?q=%s",
    "hacker_news": "https://hn.algolia.com/?q=%s",
}

DEFAULT_SEARCH_ENGINE = "duckduckgo"

QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": {
            "question": "Which browser action does the user ask for in `transcript`?",
            "focus": (
                "Judge the words said so far. If the sentence is unfinished, pick the action the words "
                "already commit to; if no action is recognizable pick none. `page` and `elements` describe "
                "what is currently on screen. `context.previous_page` and `context.recent_actions` (most "
                "recent first) say where the user just came from and what was just done: 'back to the "
                "results' after clicking a search result is go_back; 'the other one' or 'not that one' "
                "after a click is click_element on a different element."
            ),
        },
        "criteria": INTENT_CRITERIA,
    },
    "target": {
        "type": "choice",
        "instructions": {
            "question": (
                "Which element in `elements` is the one the user refers to in `transcript` (the thing to "
                "click, type into or select)? Each line of `elements` starts with the element id (e.g. e07), "
                "then its role and visible text; the options are those ids."
            ),
            "focus": (
                "Match by the element's visible text, role and position words like first/second/top (lines "
                "are in visual order, top of page first). Use `context.recent_actions` for relative "
                "references: 'the other one' / 'not that one' mean an element other than the target of the "
                "most recent action; 'open its documentation' means the docs of the page or item just "
                "opened. Pick none if the command does not refer to any element on this page, or if the "
                "referenced element is not in the list."
            ),
        },
    },
    "site": {
        "type": "choice",
        "instructions": {
            "question": "Which website or search engine does the user name in `transcript`?",
            "focus": "Only what is explicitly said. Pick none if no site is named.",
        },
        "criteria": {
            "google": "Google (google, google it)",
            "duckduckgo": "DuckDuckGo",
            "the_web": "A general web search with no site named (search the web, look it up online)",
            "youtube": "YouTube (videos)",
            "wikipedia": "Wikipedia (the encyclopedia)",
            "github": "GitHub (code, repositories)",
            "amazon": "Amazon (shopping)",
            "reddit": "Reddit",
            "twitter_x": "Twitter / X",
            "hacker_news": "Hacker News (news.ycombinator.com, hn)",
            "example_com": "example.com / example dot com",
            "other_named_site": "Some other website named explicitly in `transcript` (a domain or brand not listed above)",
            "none": "No website or search engine is mentioned in `transcript`",
        },
    },
    "complete": {
        "type": "noul",
        "instructions": {
            "question": "Has the user finished saying the command in `transcript`, so it can be executed now without waiting for more words?",
            "focus": (
                "Speech arrives word by word. A command is complete when its verb and any required object "
                "are present (a site for go to, a query for search for, an element for click, text for type)."
            ),
        },
        "criteria": {
            "true": {
                "what": "Complete, actionable command",
                "examples": ["scroll down", "go back", "go to wikipedia", "search for alan turing", "click the first result"],
            },
            "false": {
                "what": "Cut off before the required object; more words are clearly coming",
                "examples": ["go to", "search for", "click the", "type", "open the", "scroll"],
            },
        },
    },
    "is_command": {
        "type": "noul",
        "instructions": {
            "question": "Is `transcript` an instruction addressed to a web browser (navigate, search, click, type, scroll, tabs, confirm/cancel)?",
            "focus": (
                "Chit-chat, narration, talking to another person, or a stray fragment is not a command. A "
                "reaction to what the browser just did in `context.recent_actions` ('no, not that one', "
                "'undo that', 'wrong link', 'yes confirm') IS addressed to the browser."
            ),
        },
        "criteria": {
            "true": {
                "what": "An imperative aimed at the browser, or a correction / confirmation of its last action",
                "examples": ["scroll down", "go to youtube", "click sign in", "no not that one", "undo that", "confirm"],
            },
            "false": {
                "what": "Not directed at the browser",
                "examples": ["I think we should get lunch", "um so yeah", "this is the demo", "what did you say"],
            },
        },
    },
    "destructive": {
        "type": "noul",
        "instructions": {
            "question": "Would carrying out the action in `transcript` on this `page` submit a form, place an order, pay, delete, send a message, post publicly, log out, or otherwise do something hard to undo?",
            "focus": "Navigating, scrolling, reading, clicking links and typing into a box are NOT destructive.",
        },
        "criteria": {
            "true": {
                "what": "Irreversible side effect",
                "examples": ["click buy now", "delete this repository", "send the message", "post the comment", "click checkout"],
            },
            "false": {
                "what": "Reversible / read-only",
                "examples": ["scroll down", "go to wikipedia", "click the first result", "type hello in the search box"],
            },
        },
    },
    "scroll_amount": {
        "type": "score",
        "instructions": {
            "question": "How far does the user want to scroll according to `transcript`?",
            "focus": "Only relevant when scrolling; default is one screen when nothing is specified.",
        },
        "criteria": [
            {"what": "A little: a few lines (a bit, slightly, a little)"},
            {"what": "One screen / one page, or no amount specified"},
            {"what": "All the way to the end: the very top or the very bottom"},
        ],
    },
    "text_span": {
        "type": "choice",
        "instructions": {
            "question": "Which option is exactly the text the user wants typed or searched, as spoken in `transcript`? Options are verbatim candidate spans.",
            "focus": "Choose the span that contains the payload text only, without the command words (type, search for, into the search box). Pick none if nothing should be typed.",
        },
    },
    "url_span": {
        "type": "choice",
        "instructions": {
            "question": "Which option is the web address (domain) the user wants to open, as spoken in `transcript`?",
            "focus": "Pick none if no address is mentioned.",
        },
    },
    "is_correction": {
        "type": "noul",
        "instructions": {
            "question": "Is the user in `transcript` saying that the most recent action in `context.recent_actions` was wrong and should be reversed or redirected?",
            "focus": (
                "A correction reacts to what just happened ('no', 'not that one', 'wrong link', 'undo that', "
                "'the other one', 'I meant the second one'). A fresh command that merely follows the "
                "previous action is not a correction."
            ),
        },
        "criteria": {
            "true": {
                "what": "Rejects or redirects the previous action",
                "examples": ["no not that one", "wrong one, go back", "undo that", "I meant the other link", "not that, the second one"],
            },
            "false": {
                "what": "A new command or a continuation, satisfied with the previous action",
                "examples": ["scroll down", "now click the comments", "open the documentation", "search for cats"],
            },
        },
    },
    "tab_direction": {
        "type": "choice",
        "instructions": {"question": "When switching tabs, which tab does `transcript` refer to?"},
        "criteria": {
            "next": "The next tab / the other tab / switch tab with no direction",
            "previous": "The previous tab / the tab before / last tab",
            "first": "The first tab",
            "none": "Not about switching tabs",
        },
    },
}

_client = None


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("Set OPENROUTER_API_KEY (from https://openrouter.ai/settings/keys)")
    return key


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(http2=True, timeout=15)
    return _client


def cost_usd(usage: dict) -> float:
    return (usage.get("input_tokens", 0) / 1_000_000) * PRICE_PER_M_INPUT_TOKENS_USD


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def encode_element(el: dict, page_host: str = "") -> str:
    s = f"{el['id']} {el['role']}"
    text = str(el.get("text", ""))[:MAX_ELEMENT_TEXT]
    if text:
        s += f' "{text}"'
    if el.get("placeholder") and el["placeholder"] != text:
        s += f" (placeholder: {str(el['placeholder'])[:40]})"
    if el.get("href"):
        host = str(el["href"]).split("/")[0]
        if host and host != page_host:
            s += f" -> {host}"
    if el.get("below_fold"):
        s += " [below fold]"
    return s


def encode_context(context) -> dict | None:
    if not context:
        return None
    out = {}
    prev = context.get("previous_page") or {}
    if prev.get("url"):
        out["previous_page"] = {
            "url": str(prev["url"])[:200],
            "title": str(prev.get("title", ""))[:120],
        }
    actions = (context.get("recent_actions") or [])[-MAX_CONTEXT_ACTIONS:]
    if actions:
        now = time.time()
        out["recent_actions"] = []
        for a in reversed(actions):
            e = {"said": str(a.get("said", ""))[:120], "action": a.get("type", "")}
            if a.get("target_label"):
                e["target"] = str(a["target_label"])[:80]
            if a.get("text"):
                e["text"] = str(a["text"])[:80]
            if a.get("url"):
                e["url"] = str(a["url"])[:200]
            e["outcome"] = a.get("outcome") or ("failed" if a.get("ok") is False else "done")
            if a.get("at"):
                e["seconds_ago"] = max(0, round(now - a["at"]))
            out["recent_actions"].append(e)
    return out or None


def build_request(transcript, snapshot, context=None, pending_confirmation=None, tab_count=None):
    """Build (state, questions, candidates) for one decision."""
    from spans import extract_text_candidates, extract_url_candidates

    text = " ".join(str(transcript or "").split())[-MAX_TRANSCRIPT_CHARS:]
    text_candidates = extract_text_candidates(text)
    url_candidates = extract_url_candidates(text)

    elements = (snapshot or {}).get("elements", [])[:MAX_ELEMENTS]
    page_host = host_of((snapshot or {}).get("url", ""))
    state = {
        "transcript": text,
        "page": {
            "url": str((snapshot or {}).get("url", "about:blank"))[:200],
            "title": str((snapshot or {}).get("title", ""))[:120],
            "site": (snapshot or {}).get("site", "blank"),
        },
        "elements": [encode_element(el, page_host) for el in elements],
    }
    ctx = encode_context(context)
    if ctx:
        state["context"] = ctx
    if pending_confirmation:
        state["pending_confirmation"] = pending_confirmation
    if tab_count and tab_count > 1:
        state["open_tabs"] = tab_count

    target_criteria = {el["id"]: None for el in elements}
    target_criteria["none"] = "No element on this page is referred to"

    questions = {}
    for qid in ("intent", "target", "site", "complete", "is_command", "destructive", "scroll_amount", "tab_direction"):
        q = QUESTIONS[qid]
        entry = {"type": q["type"], "instructions": q["instructions"]}
        if qid == "target":
            entry["criteria"] = target_criteria
        elif "criteria" in q:
            entry["criteria"] = q["criteria"]
        questions[qid] = entry

    if ctx and ctx.get("recent_actions"):
        q = QUESTIONS["is_correction"]
        questions["is_correction"] = {"type": q["type"], "instructions": q["instructions"], "criteria": q["criteria"]}

    if text_candidates:
        c = {s: None for s in text_candidates}
        c["none"] = "Nothing should be typed or searched"
        questions["text_span"] = {"type": "choice", "instructions": QUESTIONS["text_span"]["instructions"], "criteria": c}
    if url_candidates:
        c = {s: None for s in url_candidates}
        c["none"] = "No web address is mentioned"
        questions["url_span"] = {"type": "choice", "instructions": QUESTIONS["url_span"]["instructions"], "criteria": c}

    return state, questions, {"text": text_candidates, "url": url_candidates}


def decide(state, questions):
    """One Jev call via OpenRouter's Decisions API. Returns answers + latency + usage + cost."""
    body = {"model": MODEL, "state": state, "questions": questions}
    started = time.perf_counter()
    response = client().post(DECISIONS_URL, json=body, headers={"Authorization": f"Bearer {api_key()}"})
    latency_ms = round((time.perf_counter() - started) * 1000)
    if response.is_error:
        raise RuntimeError(f"Jev HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    return {
        "answers": data["answers"],
        "latency_ms": latency_ms,
        "usage": data.get("usage", {}),
        "cost_usd": cost_usd(data.get("usage", {})),
        "model": data.get("model", MODEL),
    }