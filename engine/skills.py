"""Skills: a goal that worked once is not worked out again.

The first time "go to settings and find dark mode" is asked in Hermes, Jev works it out step
by step.  If it succeeds, the steps it took — which elements, in which order, what was typed —
are kept as a skill.  The next time, the skill replays them directly: no model, a fraction of
the time, and no cost.  If the app has changed and a step's element is not there, the replay
stops where it diverged and the goal loop takes over from that screen, like it would have
anyway.

Skills record where to click, never what was on screen: a label path through an app, the same
for every user of that app.  That is what makes shipping a few with Omarchy, and sharing them
later, possible.  Shipped skills live in the plugin's skills/ folder; learned ones in
~/.local/share/omarchy-voice/skills/.

The safety gates still apply on replay: a step whose label is risky is not replayed alone —
the loop, which asks, takes over instead.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "skills"
LEARNED = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "omarchy-voice" / "skills"

REPLAYABLE = {"click_link", "click_button", "type_text", "press_enter", "scroll_down", "scroll_up", "go_back"}
MATCH = 88.0               # how close what was said must be to a skill's phrasing
SETTLE_S = 0.8


def normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9{} ]+", " ", text.lower())
    text = re.sub(r"\b(please|could you|can you|would you|for me|and then|then)\b", " ", text)
    return " ".join(text.split())


@dataclass
class Skill:
    id: str
    app: str                            # window class it belongs to, e.g. "hermes"
    phrases: list[str]                  # how it has been asked; "{text}" marks a slot
    steps: list[dict]                   # {"kind", "role", "text", "typed"?}
    answer: str = ""                    # the line it ended on, if the goal was to find something
    source: str = "learned"             # learned · shipped
    uses: int = 0
    ok: int = 0
    created: int = field(default_factory=lambda: int(time.time()))

    @property
    def title(self) -> str:
        return self.phrases[0] if self.phrases else self.id


# -- the library --------------------------------------------------------------------------
class Library:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}
        for folder, source in ((SHIPPED, "shipped"), (LEARNED, "learned")):
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                try:
                    rows = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                for row in rows if isinstance(rows, list) else []:
                    try:
                        skill = Skill(**{**row, "source": row.get("source", source)})
                    except TypeError:
                        continue
                    # a learned copy of a shipped skill carries its use counts
                    self.skills[skill.id] = skill

    def for_app(self, app: str) -> list[Skill]:
        return [s for s in self.skills.values() if s.app == app]

    def save(self, app: str) -> None:
        LEARNED.mkdir(parents=True, exist_ok=True)
        rows = [asdict(s) for s in self.skills.values() if s.app == app]
        (LEARNED / f"{app}.json").write_text(json.dumps(rows, indent=1))

    def forget(self, skill_id: str) -> bool:
        skill = self.skills.pop(skill_id, None)
        if skill is None:
            return False
        self.save(skill.app)
        return True

    # -- matching ---------------------------------------------------------------------------
    def match(self, goal: str, app: str) -> tuple[Skill, dict] | None:
        """The skill that does what was just asked in this app, and the slot values it needs."""
        from rapidfuzz import fuzz

        said = normalize(goal)
        best: tuple[float, Skill, dict] | None = None
        for skill in self.for_app(app):
            if skill.uses >= 4 and skill.ok / max(1, skill.uses) < 0.5:
                continue                       # it keeps failing: let the loop work it out again
            for phrase in skill.phrases:
                wanted = normalize(phrase)
                slots: dict = {}
                if "{text}" in wanted:
                    pattern = "^" + re.escape(wanted).replace(r"\{text\}", "(?P<text>.+)") + "$"
                    m = re.match(pattern, said)
                    if not m:
                        continue
                    slots = {"text": m.group("text")}
                    score = 100.0
                else:
                    score = max(fuzz.ratio(said, wanted), fuzz.token_set_ratio(said, wanted) - 4)
                if score >= MATCH and (best is None or score > best[0]):
                    best = (score, skill, slots)
        return (best[1], best[2]) if best else None

    # -- learning ---------------------------------------------------------------------------
    def compile(self, goal: str, run, app: str) -> Skill | None:
        """Keep a run that worked. Only clean runs: nothing asked, nothing refused, every step ok."""
        if run.status != "done":
            return None
        steps = []
        for step in run.steps:
            if step.kind == "done":
                continue
            if step.kind not in REPLAYABLE or not step.ok or step.asked:
                return None                   # it needed judgement or approval: not a replay
            if step.kind in ("click_link", "click_button", "type_text") and not step.target:
                return None
            row = {"kind": step.kind}
            if step.target:
                row.update(role=step.target.get("role", ""), text=step.target.get("text", ""),
                           placeholder=step.target.get("placeholder", ""))
            if step.kind == "type_text":
                row["typed"] = step.typed
            steps.append(row)
        if not steps:
            return None

        phrase = " ".join(goal.split())
        typed = next((s["typed"] for s in steps if s.get("typed")), "")
        if typed and typed.lower() in phrase.lower():
            # what was typed came from what was said: that part is a slot next time
            start = phrase.lower().index(typed.lower())
            phrase = phrase[:start] + "{text}" + phrase[start + len(typed):]
            for s in steps:
                if s.get("typed") == typed:
                    s["typed"] = "{text}"

        same = next((s for s in self.for_app(app) if s.steps == steps), None)
        if same is not None:
            # the same path worked again: that is evidence for the skill, not a second skill
            same.ok += 1
            same.uses = max(same.uses, same.ok)
            new_phrase = phrase not in same.phrases
            if new_phrase:
                same.phrases.append(phrase)    # another way of asking for the same thing
            self.save(app)
            return same if new_phrase else None

        key = re.sub(r"[^a-z0-9]+", "-", normalize(phrase.replace("{text}", "")))[:40].strip("-")
        skill = Skill(id=f"{app}.{key}.{int(time.time()) % 100000}", app=app, phrases=[phrase],
                      steps=steps, answer=run.answer[:200])
        self.skills[skill.id] = skill
        self.save(app)
        return skill


# -- replaying ----------------------------------------------------------------------------
def _locate(snapshot: dict, step: dict) -> dict | None:
    """The element a step clicks, found by what it says — ids change from screen to screen."""
    from rapidfuzz import fuzz

    want = " ".join(str(step.get("text", "")).split()).lower()
    role = step.get("role", "")
    elements = snapshot.get("elements") or []
    exact = [e for e in elements if " ".join(str(e.get("text", "")).split()).lower() == want]
    if exact:
        return next((e for e in exact if e.get("role") == role), exact[0])
    if not want and step.get("placeholder"):
        return next((e for e in elements if e.get("placeholder") == step["placeholder"]), None)
    scored = [(fuzz.ratio(want, " ".join(str(e.get("text", "")).split()).lower()), e) for e in elements]
    scored = [x for x in scored if x[0] >= 92]
    return max(scored, key=lambda x: x[0])[1] if scored else None


def replay(skill: Skill, bridge, slots: dict, *, on_step=None, stop=None, recorder=None):
    """Run a skill. Returns (run, diverged_at) — diverged_at is None when it finished.

    `run` is an autopilot.Run so the caller treats a replay exactly like a worked-out goal.
    """
    import autopilot

    run = autopilot.Run(goal=skill.title)
    on_step = on_step or (lambda step, run: None)
    stop = stop or (lambda: False)
    for n, row in enumerate(skill.steps, 1):
        if stop():
            run.status = "stopped"
            return run, None
        snapshot = bridge.snapshot_for(None, timeout=8.0)
        if not snapshot:
            run.status = "error"
            return run, n
        kind = row["kind"]
        typed = str(row.get("typed", "")).replace("{text}", slots.get("text", ""))
        step = autopilot.Step(n=n, kind=kind, label="", source="skill",
                              target={k: row.get(k, "") for k in ("role", "text", "placeholder")} if row.get("text") or row.get("placeholder") else None,
                              typed=typed)
        if kind in ("click_link", "click_button", "type_text"):
            element = _locate(snapshot, row)
            if element is None:
                return run, n                  # the app looks different: the loop takes over here
            if autopilot.RISKY_WORDS.search(element.get("text", "")) or autopilot.SECRET_FIELD.search(
                    f"{element.get('text', '')} {element.get('placeholder', '')}"):
                return run, n                  # never replayed alone: the loop asks
            if kind == "type_text":
                action = {"type": "type_into_field", "targetId": element["id"], "text": typed}
                step.label = f"Type “{typed[:40]}”"
            else:
                action = {"type": "click_element", "targetId": element["id"]}
                step.label = f"Click “{element.get('text', '')[:40]}”"
        elif kind == "press_enter":
            action, step.label = {"type": "press_enter"}, "Press Enter"
        elif kind in ("scroll_down", "scroll_up"):
            direction = "down" if kind == "scroll_down" else "up"
            action, step.label = {"type": "scroll", "direction": direction, "amount": "page"}, f"Scroll {direction}"
        else:
            action, step.label = {"type": "go_back"}, "Go back"

        started = time.perf_counter()
        result = bridge.execute(action)
        time.sleep(SETTLE_S if kind != "type_text" else 0.2)
        step.ok = bool(result.get("ok"))
        step.outcome = str(result.get("outcome", ""))[:120]
        step.ms = int((time.perf_counter() - started) * 1000)
        run.steps.append(step)
        on_step(step, run)
        if recorder is not None and step.ok:
            after = bridge.snapshot_for(None, timeout=8.0)
            if after:
                try:
                    recorder(snapshot, step, after)
                except Exception:
                    pass
        if not step.ok:
            return run, n

    # the answer is read again from the screen now — prices change, labels do not
    if skill.answer:
        from rapidfuzz import fuzz

        control = skill.answer.split(" — ")[0].strip().lower()
        final = bridge.snapshot_for(None, timeout=8.0) or {}
        shown = next((e for e in final.get("elements") or []
                      if " ".join(str(e.get("text", "")).split()).lower() == control), None)
        if shown is not None:
            # "where is dark mode": the answer is the control, and it is on screen again
            run.answer = shown.get("text", "") + (f" — {final.get('title')}" if final.get("title") else "")
        else:
            text = bridge.execute({"type": "page_text"}).get("text", "")
            lines = autopilot.sentences(text, limit=120)
            found = max(lines, key=lambda l: fuzz.ratio(l.lower(), skill.answer.lower()), default="")
            if found and fuzz.ratio(found.lower(), skill.answer.lower()) >= 70:
                run.answer = found
            else:
                return run, len(skill.steps) + 1   # got there, but the answer is not where it was
    run.status = "done"
    done = autopilot.Step(n=len(run.steps) + 1, kind="done", label="Done", ok=True, outcome=run.answer[:120], source="skill")
    run.steps.append(done)
    on_step(done, run)
    return run, None
