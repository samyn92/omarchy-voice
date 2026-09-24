"""The harness: one goal, worked the cheapest way that is known to work.

    1. a skill        — this goal was done here before: replay it, no model at all
    2. a map route    — "go to appearance": the map knows the clicks, no model at all
    3. the goal loop  — Jev works it out, one chosen step at a time

Whatever the loop works out is kept: every click feeds the app's map, and a goal that finished
cleanly becomes a skill for next time.  In the browser — the open web, different on every
visit — only the loop runs; skills and maps are for apps, which are the same every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import appmap
import autopilot
import skills

ROUTE_GOAL = re.compile(
    r"^(?:go to|open|show(?: me)?|navigate to|take me to|switch to)(?: the)? (?P<target>[\w' -]{2,32}?)"
    r"(?: settings| page| tab| section| screen| panel)?$", re.I)


@dataclass
class Target:
    """Where a goal runs: a bridge to read and operate it, and what it is."""
    bridge: object
    app: str | None              # the app's key for skills and maps; None for the open web
    title: str = ""              # what the user calls it ("Hermes", "the browser")
    window_title: str | None = None


@dataclass
class Outcome:
    run: autopilot.Run
    used: str = "jev"            # skill · map · jev · skill+jev — how it got done
    skill: skills.Skill | None = None
    learned: skills.Skill | None = None


class Harness:
    def __init__(self, library: skills.Library | None = None) -> None:
        self.library = library or skills.Library()
        self._maps: dict[str, appmap.AppMap] = {}

    def amap(self, app: str) -> appmap.AppMap:
        if app not in self._maps:
            self._maps[app] = appmap.AppMap(app)
        return self._maps[app]

    def work(self, goal: str, target: Target, *, stage: int = autopilot.FILL_IN, ask=None, on_step=None,
             stop=None, trusted: tuple[str, ...] = (), max_steps: int = autopilot.MAX_STEPS) -> Outcome:
        on_step = on_step or (lambda step, run: None)
        stop = stop or (lambda: False)
        done_before: list[autopilot.Step] = []
        used = "jev"
        tried: skills.Skill | None = None

        if target.app:
            amap = self.amap(target.app)

            # 1. a skill that did this before
            found = self.library.match(goal, target.app)
            if found:
                tried, slots = found
                tried.uses += 1
                run, diverged = skills.replay(tried, target.bridge, slots, on_step=on_step, stop=stop,
                                              recorder=amap.record)
                if diverged is None:
                    if run.status == "done":
                        tried.ok += 1
                    self.library.save(target.app)
                    amap.save()
                    return Outcome(run=run, used="skill", skill=tried)
                self.library.save(target.app)
                done_before = run.steps              # the loop carries on from where it diverged
                used = "skill+jev"

            # 2. the map knows the way
            elif (m := ROUTE_GOAL.match(" ".join(goal.split()))):
                snapshot = target.bridge.snapshot_for(target.window_title, timeout=8.0)
                path = amap.route(snapshot, m.group("target")) if snapshot else None
                if path is not None:
                    route = skills.Skill(id="route", app=target.app, phrases=[goal],
                                         steps=[{"kind": "click_button", **p} for p in path])
                    run, diverged = skills.replay(route, target.bridge, {}, on_step=_as(on_step, "map"),
                                                  stop=stop, recorder=amap.record)
                    if diverged is None:
                        run.goal = goal
                        self._arrive(target, m.group("target"), run, _as(on_step, "map"), amap)
                        run.answer = run.answer or m.group("target").title()
                        amap.save()
                        return Outcome(run=run, used="map")
                    done_before = run.steps
                    used = "map+jev"

        # 3. Jev works it out — numbering on from whatever a skill or the map already did
        amap = self.amap(target.app) if target.app else None
        offset, numbered = len(done_before), set()

        def continued(step, run):
            if offset and id(step) not in numbered:   # a step can be reported twice: asked, then answered
                step.n += offset
                numbered.add(id(step))
            on_step(step, run)

        pilot = autopilot.Autopilot(
            target.bridge, stage=stage, ask=ask, on_step=continued, stop=stop, trusted_hosts=trusted,
            max_steps=max(3, max_steps - len(done_before)), window_title=target.window_title,
            recorder=amap.record if amap else None, hints=amap.hints if amap else None,
            navigation=amap.leads_somewhere if amap else None, open_web=target.app is None)
        run = pilot.run(goal)
        run.steps = done_before + run.steps
        for i, step in enumerate(run.steps, 1):
            step.n = i
        outcome = Outcome(run=run, used=used, skill=tried)
        if amap is not None:
            amap.save()
            if run.status == "done":
                outcome.learned = self.library.compile(goal, run, target.app)   # None when nothing new
                if tried is not None and outcome.learned is not None and outcome.learned is not tried:
                    self.library.forget(tried.id)     # the app changed; what worked now replaces it
        return outcome


    @staticmethod
    def _arrive(target: Target, place: str, run, on_step, amap) -> None:
        """"Go to appearance" ends on Appearance, not merely where its button can be seen."""
        import time

        snapshot = target.bridge.snapshot_for(target.window_title, timeout=8.0) or {}
        want = " ".join(place.lower().split())
        el = next((e for e in snapshot.get("elements") or []
                   if " ".join(str(e.get("text", "")).lower().split()) == want and appmap.navigational(e)), None)
        if el is None:
            return
        result = target.bridge.execute({"type": "click_element", "targetId": el["id"]})
        time.sleep(skills.SETTLE_S)
        done = run.steps.pop() if run.steps and run.steps[-1].kind == "done" else None
        step = autopilot.Step(n=len(run.steps) + 1, kind="click_button", label=f"Click “{el.get('text', '')}”",
                              ok=bool(result.get("ok")), source="map",
                              target={"role": el.get("role", ""), "text": el.get("text", ""), "placeholder": ""})
        run.steps.append(step)
        on_step(step, run)
        after = target.bridge.snapshot_for(target.window_title, timeout=8.0)
        if after:
            amap.record(snapshot, step, after)
        run.answer = el.get("text", "") or run.answer
        if done is not None:
            done.n = len(run.steps) + 1
            done.outcome = run.answer
            run.steps.append(done)
            on_step(done, run)


def _as(on_step, source: str):
    def wrapped(step, run):
        step.source = source
        on_step(step, run)
    return wrapped
