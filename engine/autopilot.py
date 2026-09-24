"""Work towards a goal on a page or in an app: one small step at a time, judged before it acts.

The loop is deliberately dull: look at the page, ask Jev for the single next step from the
elements that actually exist, judge whether that step commits anything, do it, look again.
Jev never writes a command — it picks an element id and a step kind from lists built here.

Three stages, set per run (`stage`):

  1  look only      follow links, scroll, search, read.  Nothing on any page changes.
  2  fill in        may type and press buttons, but everything that commits is judged and
                    handed to the user to confirm.
  3  trusted        like 2, and on hosts the user listed it may commit by itself — never
                    money, deletions, account changes or signing in, which always ask.

Two independent checks stand between the loop and anything irreversible: a word list here
in code, and a separate Jev call that sees the page.  Either one is enough to stop and ask,
and anything that fails — no key, a timeout, an answer that makes no sense — counts as
"ask".  The safe direction is always to stop.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field

import jev

LOOK = 1
FILL_IN = 2
TRUSTED = 3

MAX_STEPS = 14
MAX_SECONDS = 240.0
MAX_ELEMENTS = 80          # per step; keeps a request small enough to stay fast
SETTLE_S = 0.9             # after a click or Enter: let the next page draw before looking

# steps that only move the eyes, never change anything on a page
SAFE_KINDS = {"scroll_down", "scroll_up", "go_back", "navigate", "search_web", "click_link", "done", "stuck"}
COMMITTING_KINDS = {"click_button", "type_text", "press_enter"}

# the word list: crude, but it does not depend on a model agreeing with us
RISKY_WORDS = re.compile(
    r"\b(delete|remove|destroy|erase|buy|purchase|order|pay|payment|checkout|subscribe|donate|"
    r"send|post|publish|submit|confirm|book|reserve|apply|sign up|sign out|log ?out|log ?in|sign in|"
    r"cancel subscription|deactivate|close account|transfer|withdraw|bid|place order|"
    r"restore|reset|revert|roll ?back|discard|overwrite|factory|wipe|format)\b", re.I)
SECRET_FIELD = re.compile(r"\b(password|passwort|passcode|pin|card number|kreditkarte|cvv|cvc|iban|"
                          r"security code|secret|token|api key)\b", re.I)

# reasons the judge may give, in the words the user hears
REASONS = {
    "nothing_committed": "nothing is committed",
    "sends_or_posts": "this sends or posts something",
    "spends_money": "this spends money or places an order",
    "deletes_data": "this deletes or removes something",
    "changes_preference": "this changes a preference you can switch back",
    "changes_settings": "this changes account or security settings",
    "signs_in_or_out": "this signs in or out",
    "off_goal": "this does not serve the goal",
    "unclear": "it is not clear what this does",
}
# never done without the user, not even on a trusted site
NEVER_ALONE = {"spends_money", "deletes_data", "changes_settings", "signs_in_or_out", "unclear"}


@dataclass
class Step:
    n: int
    kind: str
    label: str
    ok: bool | None = None
    outcome: str = ""
    verdict: str = ""        # "", safe, ask, refuse
    reason: str = ""
    asked: bool = False
    cost: float = 0.0
    ms: int = 0
    target: dict | None = None   # what it acted on: {"role", "text", "placeholder"} — for skills and maps
    typed: str = ""              # what it typed, if it typed
    source: str = "jev"          # who chose this step: jev, skill or map


@dataclass
class Run:
    goal: str
    status: str = "running"   # running | done | stuck | stopped | refused | declined | limit | error
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    cost: float = 0.0

    @property
    def summary(self) -> str:
        return {"done": "Done", "stuck": "Got stuck", "stopped": "Stopped",
                "refused": "Refused — that would commit something", "declined": "You said no",
                "limit": "Gave up (too many steps)", "error": "Failed"}.get(self.status, self.status)


def sentences(text: str, limit: int = 40) -> list[str]:
    """The page as a reader sees it, cut into sentences an answer can be picked from."""
    out: list[str] = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        line = " ".join(raw.split())
        if 8 <= len(line) <= 220:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def text_candidates(goal: str) -> list[str]:
    """What the loop may type: spans of the goal itself, never anything invented."""
    g = " ".join(goal.split()).strip(" .!?")
    out = []
    for pattern in (r"(?:search|look) (?:for|up) (.+)", r"(?:find|find out|figure out|research) (?:out )?(.+)",
                    r"\"([^\"]+)\"", r"(?:about|for|on) (.+)"):
        m = re.search(pattern, g, re.I)
        if m and m.group(1).strip():
            out.append(m.group(1).strip(" .!?"))     # the payload, without the command words
    out.append(g)
    seen, uniq = set(), []
    for c in out:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c[:120])
    return uniq[:6]


class Autopilot:
    """One goal, one browser, one step at a time.

    `browser`   a BrowserBridge (snapshot_for / execute)
    `ask`       callback(label, reason) -> bool: put it to the user and wait
    `on_step`   callback(step, run): for the panel, the HUD and the trace
    `stop`      callback() -> bool: the user said stop
    """

    def __init__(self, browser, *, stage: int = FILL_IN, ask=None, on_step=None, stop=None,
                 trusted_hosts: tuple[str, ...] = (), max_steps: int = MAX_STEPS,
                 max_seconds: float = MAX_SECONDS, window_title: str | None = None,
                 recorder=None, hints=None, open_web: bool = True, navigation=None) -> None:
        self.browser = browser
        self.stage = stage
        self.ask = ask or (lambda label, reason: False)
        self.on_step = on_step or (lambda step, run: None)
        self.stop = stop or (lambda: False)
        self.trusted_hosts = tuple(h.lower() for h in trusted_hosts)
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.window_title = window_title
        self._typed_into_search = False   # Enter in a search box submits a search, not a commitment
        self._seen: set[str] = set()          # what was on screen last step, to find what is new
        # recorder(before, step, after): every action with the screen before and after it — the
        #   raw material of app maps.  hints(snapshot) -> {element id: [labels it leads to]}: what
        #   the map already knows, shown to Jev next to each element.
        self.recorder = recorder
        self.hints = hints
        # navigation(snapshot, element) -> True when the map knows this click only goes somewhere
        self.navigation = navigation
        # an app is not the web: searching the internet from inside Hermes is not a step
        self.open_web = open_web

    # -- the loop ------------------------------------------------------------
    def run(self, goal: str) -> Run:
        run = Run(goal=goal)
        deadline = time.monotonic() + self.max_seconds
        last_signature = ""
        stale = 0
        pending = None            # (screen before, step) of the last action, until the next screen is read

        for n in range(1, self.max_steps + 1):
            if self.stop():
                run.status = "stopped"
                return run
            if time.monotonic() > deadline:
                run.status = "limit"
                return run

            snapshot = self.browser.snapshot_for(self.window_title, timeout=8.0)
            if not snapshot:
                run.status = "error"
                run.answer = "I can't read the page or the app any more."
                return run
            if pending is not None and self.recorder is not None:
                try:
                    self.recorder(pending[0], pending[1], snapshot)
                except Exception:
                    pass                  # a map that cannot be written must not end the goal
            pending = None

            signature = f"{snapshot.get('url')}|{len(snapshot.get('elements') or [])}"
            stale = stale + 1 if signature == last_signature else 0
            last_signature = signature
            if stale >= 3:            # three steps and the page has not moved: stop guessing
                run.status = "stuck"
                return run

            started = time.perf_counter()
            try:
                choice = self._next_step(goal, snapshot, run)
            except Exception as exc:
                run.status = "error"
                run.answer = f"Jev could not decide: {exc}"[:160]
                return run

            element = choice.get("element") or {}
            step = Step(n=n, kind=choice["kind"], label=choice["label"], cost=choice["cost"],
                        target={k: element.get(k, "") for k in ("role", "text", "placeholder")} if element else None,
                        typed=choice.get("text", "") if choice["kind"] == "type_text" else "")
            run.cost += choice["cost"]

            if step.kind == "done":
                # "done" without a line of the page behind it is a guess, not an answer
                run.status = "done" if choice.get("answer") else "stuck"
                run.answer = choice.get("answer") or ""
                step.ok, step.outcome, step.ms = True, run.answer[:120], int((time.perf_counter() - started) * 1000)
                run.steps.append(step)
                self.on_step(step, run)
                return run
            if step.kind == "stuck":
                run.status = "stuck"
                step.ok, step.outcome = False, "no way forward from this page"
                run.steps.append(step)
                self.on_step(step, run)
                return run

            blocked = self._stage_block(step, choice)
            if blocked:
                run.status = "refused"
                step.ok, step.outcome, step.verdict, step.reason = False, blocked, "refuse", blocked
                run.steps.append(step)
                self.on_step(step, run)
                return run

            if step.kind == "press_enter" and self._typed_into_search:
                step.verdict, step.reason = "safe", "nothing_committed"   # submitting a search
            elif (step.kind == "click_button" and self.navigation is not None and choice.get("element")
                  and not RISKY_WORDS.search(step.label) and self._known_way(snapshot, choice["element"])):
                step.verdict, step.reason = "safe", "nothing_committed"   # the map knows it only goes somewhere
            elif step.kind in COMMITTING_KINDS:
                verdict, reason, cost = self._judge(goal, snapshot, step, choice)
                if verdict == "safe" and reason in NEVER_ALONE:
                    # "safe, because it changes settings" is a contradiction; the stricter half wins.
                    # Seen in Hermes: clicking "Dark" came back safe/changes_settings and went through.
                    verdict = "ask"
                run.cost += cost
                step.verdict, step.reason, step.cost = verdict, reason, step.cost + cost
                if verdict == "refuse":
                    run.status = "refused"
                    step.ok, step.outcome = False, REASONS.get(reason, reason)
                    run.steps.append(step)
                    self.on_step(step, run)
                    return run
                if not self._may_act_alone(verdict, reason, step, snapshot):
                    step.asked = True
                    self.on_step(step, run)
                    if not self.ask(step.label, REASONS.get(reason, "")):
                        run.status = "declined"
                        step.ok, step.outcome = False, "you said no"
                        run.steps.append(step)
                        self.on_step(step, run)
                        return run

            if step.kind == "type_text":
                self._typed_into_search = self._is_search_field(choice.get("element") or {}, snapshot)
            elif step.kind != "press_enter":
                self._typed_into_search = False

            result = self.browser.execute(choice["action"], self.window_title)
            if step.kind in ("click_link", "click_button", "press_enter", "search_web", "go_back"):
                time.sleep(SETTLE_S)      # a page that is still drawing has nothing to decide from
            step.ok = bool(result.get("ok"))
            step.outcome = str(result.get("outcome", ""))[:120]
            step.ms = int((time.perf_counter() - started) * 1000)
            run.steps.append(step)
            self.on_step(step, run)
            if step.ok:
                pending = (snapshot, step)

        run.status = "limit"
        return run

    # -- the two gates -------------------------------------------------------
    def _known_way(self, snapshot: dict, element: dict) -> bool:
        try:
            return bool(self.navigation(snapshot, element))
        except Exception:
            return False

    def _stage_block(self, step: Step, choice: dict) -> str:
        """Hard rules, before any model has a say."""
        if self.stage <= LOOK and step.kind in COMMITTING_KINDS:
            return "only looking (stage 1)"
        element = choice.get("element") or {}
        target = f"{element.get('text', '')} {element.get('placeholder', '')}"
        if step.kind == "type_text" and SECRET_FIELD.search(target):
            return "that field asks for a secret"
        if step.kind == "type_text" and SECRET_FIELD.search(choice.get("text", "")):
            return "that text looks like a secret"
        return ""

    def _may_act_alone(self, verdict: str, reason: str, step: Step, snapshot: dict) -> bool:
        """Both checks must be happy — and on a trusted host, still never the big ones."""
        if verdict == "safe" and not RISKY_WORDS.search(step.label):
            return True     # both checks happy
        # stage 3: on a host the user trusts, go on anyway — but never the big ones
        return self.stage >= TRUSTED and self._trusted(snapshot) and reason not in NEVER_ALONE

    @staticmethod
    def _is_search_field(element: dict, snapshot: dict) -> bool:
        if not element:
            return False
        if element.get("id") and element["id"] == snapshot.get("search_box_id"):
            return True
        text = f"{element.get('role', '')} {element.get('text', '')} {element.get('placeholder', '')}"
        return bool(re.search(r"\bsearch|suchen|suche\b", text, re.I))

    def _trusted(self, snapshot: dict) -> bool:
        host = jev.host_of(snapshot.get("url", "")).lower()
        return any(host == h or host.endswith("." + h) for h in self.trusted_hosts)

    # -- the two Jev calls ---------------------------------------------------
    def _page_lines(self) -> list[str]:
        result = self.browser.execute({"type": "page_text"}, self.window_title)
        return sentences(result.get("text", "") if isinstance(result, dict) else "")

    def _describe(self, e: dict, host: str, leads: dict) -> str:
        mark = "(in dialog) " if e.get("in_dialog") else "(new) " if e.get("new") else ""
        line = mark + jev.encode_element(e, host)
        if leads.get(e["id"]):
            line += " → leads to: " + ", ".join(leads[e["id"]][:6])
        return line

    def _state(self, goal: str, snapshot: dict, run: Run, lines: list[str] | None = None,
               elements: list[dict] | None = None) -> dict:
        host = jev.host_of(snapshot.get("url", ""))
        leads = {}
        if self.hints is not None:
            try:
                leads = self.hints(snapshot) or {}
            except Exception:
                leads = {}
        elements = elements if elements is not None else (snapshot.get("elements") or [])[:MAX_ELEMENTS]
        return {
            "page_text": [f"t{i:02d} {line}" for i, line in enumerate(lines or [])],
            "goal": goal,
            "page": {"url": str(snapshot.get("url", ""))[:200], "title": str(snapshot.get("title", ""))[:120]},
            "elements": [self._describe(e, host, leads) for e in elements],
            "steps_so_far": [{"did": s.label, "outcome": s.outcome, "worked": s.ok} for s in run.steps[-6:]],
            "steps_left": self.max_steps - len(run.steps),
        }

    def _prioritise(self, snapshot: dict) -> list[dict]:
        """What just appeared goes first.

        An app shows most of the same things after every click — a sidebar, a toolbar — and
        what the click opened (a settings pane, a dialog, a menu) lands at the end of the
        list, past what a request can carry. Clicking "Open settings" in Hermes put
        "Appearance" at position 81 of 100, behind the whole session list.
        """
        key = lambda e: f"{e.get('role')}|{e.get('text')}"   # noqa: E731
        elements, seen_here = [], set()
        for e in snapshot.get("elements") or []:
            if key(e) in seen_here:
                continue          # a list repeats itself ("Session actions" on every row)
            seen_here.add(key(e))
            elements.append(e)
        new = lambda e: bool(self._seen) and key(e) not in self._seen   # noqa: E731
        for e in elements:
            e["new"] = new(e)
        self._seen = seen_here
        # an open dialog is where the user's attention is; then whatever just appeared
        rank = lambda e: (not e.get("in_dialog"), not e["new"])   # noqa: E731
        return sorted(elements, key=rank)[:MAX_ELEMENTS]

    def _next_step(self, goal: str, snapshot: dict, run: Run) -> dict:
        elements = self._prioritise(snapshot)
        lines = self._page_lines()
        ids = {e["id"]: e for e in elements}
        kinds = {
            "click_link": "Follow a link or a search result to another page",
            "scroll_down": "Scroll further down this page to see more",
            "scroll_up": "Scroll back up this page",
            "go_back": "Go back to the previous page",
            **({"search_web": "Search the web for the goal (use when this page cannot help)"} if self.open_web else {}),
            "done": "The goal is reached: the answer is visible on this page",
            "stuck": "This cannot be done from here (a login is needed, nothing matches, a dead end)",
        }
        if self.stage >= FILL_IN:
            kinds |= {
                "click_button": "Press a button or control on this page",
                "type_text": "Type into a field on this page",
                "press_enter": "Press Enter to submit what was typed",
            }
        texts = text_candidates(goal)
        questions = {
            "step": {"type": "choice",
                     "instructions": {"question": "What is the single next step towards `goal` on this page?",
                                      "focus": ("Elements marked (new) appeared after the last step — usually what that "
                                                "step opened, and usually where to look next. "
                                                "Judge only from `elements` (what can be acted on) and `page_text` (what "
                                                "is written). Prefer the smallest step that makes progress. To look "
                                                "something up on the web, choose search_web: it opens a results page "
                                                "directly and is more reliable than typing into whatever box this page "
                                                "happens to show. Type into a field only for this site's own search or a "
                                                "form the goal needs. Pick done as soon as `page_text` answers the goal, "
                                                "and stuck rather than guessing. When the goal is to find, show or "
                                                "locate something — where a setting is, what a value is — it is done "
                                                "the moment that thing is visible: finding where dark mode is does not "
                                                "mean switching it on. Only operate a control when the goal asks for "
                                                "the change itself.")},
                     "criteria": kinds},
            "element": {"type": "choice",
                        "instructions": {"question": "Which element does that step act on? Options are the element ids in `elements`.",
                                         "focus": ("For typing, pick the field itself — a textbox, searchbox or combobox, "
                                                   "often recognizable by its placeholder. Pick none only for scrolling, "
                                                   "going back or searching the web.")},
                        "criteria": {e["id"]: jev.encode_element(e, jev.host_of(snapshot.get("url", ""))) for e in elements}
                                    | {"none": "no element"}},
            "answer": {"type": "choice",
                       "instructions": {"question": "If the step is done: which line of the page answers `goal`?",
                                        "focus": ("Options are lines of `page_text`. Pick none unless the step is done. "
                                                  "When the goal was to find where a control is, pick none here and "
                                                  "choose that control as the element instead.")},
                       "criteria": {f"t{i:02d}": line for i, line in enumerate(lines)} | {"none": "no answer here"}},
            "text": {"type": "choice",
                     "instructions": {"question": "If something is typed or searched: which option is the text to use?",
                                      "focus": ("Options are verbatim spans of the goal. Prefer the one that is the payload "
                                                "alone, without words like 'find out' or 'search for'. Pick none when "
                                                "nothing is typed.")},
                     "criteria": {t: t for t in texts} | {"none": "nothing to type"}},
        }
        result = jev.decide(self._state(goal, snapshot, run, lines, elements), questions)
        answers = result["answers"]
        cost = jev.cost_usd(result.get("usage", {}))

        def pick(name: str) -> str:
            return str((answers.get(name) or {}).get("choice") or "none")

        kind = pick("step")
        if kind not in kinds:
            # a stage cannot offer it, so a real model cannot pick it; keep committing kinds
            # recognizable so the stage rule refuses them out loud instead of silently stalling
            kind = kind if kind in COMMITTING_KINDS else "stuck"
        element = ids.get(pick("element"))
        text = pick("text")
        text = "" if text == "none" else text
        out: dict = {"kind": kind, "cost": cost, "element": element, "text": text, "action": {}, "label": ""}

        if kind in ("click_link", "click_button"):
            if not element:
                # nothing named: look further down the page rather than give up here
                out["kind"] = "scroll_down"
                out["action"] = {"type": "scroll", "direction": "down", "amount": "page"}
                out["label"] = "Scroll down (nothing to click yet)"
                return out
            out["action"] = {"type": "click_element", "targetId": element["id"]}
            out["label"] = f"Click “{element.get('text', '')[:40]}”"
        elif kind == "type_text":
            if not element:
                # the page's own search box, which the snapshot already identified
                element = ids.get(str(snapshot.get("search_box_id") or ""))
                out["element"] = element
            if (not element or not text) and not self.open_web:
                out["kind"] = "stuck"          # inside an app there is no web to fall back to
                out["label"] = "nothing to type into"
                return out
            if not element or not text:
                # no field to type into: search the web for the goal rather than give up
                query = text or goal
                url = jev.SITE_SEARCH[jev.DEFAULT_SEARCH_ENGINE].replace("%s", urllib.parse.quote_plus(query))
                out["kind"] = "search_web"
                out["action"] = {"type": "navigate_url", "url": url}
                out["label"] = f"Search the web for “{query[:40]}”"
                return out
            out["action"] = {"type": "type_into_field", "targetId": element["id"], "text": text}
            out["label"] = f"Type “{text[:40]}” into {element.get('text') or element.get('placeholder') or 'the field'}"
        elif kind == "press_enter":
            out["action"] = {"type": "press_enter"}
            out["label"] = "Press Enter"
        elif kind in ("scroll_down", "scroll_up"):
            out["action"] = {"type": "scroll", "direction": "down" if kind == "scroll_down" else "up", "amount": "page"}
            out["label"] = "Scroll " + ("down" if kind == "scroll_down" else "up")
        elif kind == "go_back":
            out["action"] = {"type": "go_back"}
            out["label"] = "Go back"
        elif kind == "search_web":
            query = text or goal
            url = jev.SITE_SEARCH[jev.DEFAULT_SEARCH_ENGINE].replace("%s", urllib.parse.quote_plus(query))
            out["action"] = {"type": "navigate_url", "url": url}
            out["label"] = f"Search the web for “{query[:40]}”"
        elif kind == "done":
            picked = pick("answer")
            index = int(picked[1:]) if re.fullmatch(r"t\d+", picked) and int(picked[1:]) < len(lines) else -1
            out["answer"] = lines[index] if index >= 0 else ""
            if not out["answer"] and element is not None and element.get("text"):
                # "where is dark mode": the answer is the control itself, and where it is
                where = snapshot.get("title") or ""
                out["answer"] = f"{element['text']}" + (f" — {where}" if where else "")
            out["label"] = "Done"
        else:
            out["kind"] = "stuck"
            out["label"] = "stuck"
        return out

    def _judge(self, goal: str, snapshot: dict, step: Step, choice: dict) -> tuple[str, str, float]:
        """A second, separate Jev call: does this step commit anything? Anything unclear means ask."""
        element = choice.get("element") or {}
        state = {
            "goal": goal,
            "page": {"url": str(snapshot.get("url", ""))[:200], "title": str(snapshot.get("title", ""))[:120]},
            "about_to_do": step.label,
            "on_element": jev.encode_element(element, jev.host_of(snapshot.get("url", ""))) if element else "",
        }
        questions = {
            "verdict": {"type": "choice",
                        "instructions": {"question": "Is `about_to_do` safe to do on this page without asking the user first?",
                                         "focus": ("Safe means nothing leaves the browser and nothing changes for anyone "
                                                   "else: reading, scrolling, following links, and searching or filtering — "
                                                   "including typing a query and submitting it, which only fetches results. "
                                                   "Ask when it commits something a person would want to see first: sending a "
                                                   "message, posting, buying, booking, changing an account. Changing a setting "
                                                   "is also a commitment when `goal` only asked to find or show it — finding "
                                                   "where dark mode is is not switching it on. Refuse when it is irreversible "
                                                   "or clearly not what `goal` asked for. Opening a screen, a panel, a "
                                                   "menu or a settings page to look at it is safe — only changing a value "
                                                   "inside it counts as changing settings.")},
                        "criteria": {"safe": "Nothing is committed: go ahead",
                                     "ask": "It commits something: put it to the user first",
                                     "refuse": "Irreversible, or not what the goal asked for: do not do it"}},
            "reason": {"type": "choice",
                       "instructions": {"question": "Why? Pick the closest.", "focus": "One reason, the most important one."},
                       "criteria": REASONS},
        }
        try:
            result = jev.decide(state, questions)
        except Exception:
            return "ask", "unclear", 0.0          # no judge, no unattended action
        answers = result["answers"]
        verdict = str((answers.get("verdict") or {}).get("choice") or "ask")
        reason = str((answers.get("reason") or {}).get("choice") or "unclear")
        if verdict not in ("safe", "ask", "refuse"):
            verdict = "ask"
        return verdict, reason, jev.cost_usd(result.get("usage", {}))
