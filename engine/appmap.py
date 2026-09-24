"""App maps: what each click in an app leads to, learned once and used every time after.

Every Omarchy user runs the same apps.  Which button opens which screen is the same knowledge
for all of them, so it is worth learning once — by exploring the app, or simply by watching
goals being worked — and keeping.  With a map, Jev sees next to "Open settings" that it leads
to "General, Appearance, Model", and "go to appearance" needs no model at all: it is a path.

A map is a graph.  A node is a screen, recognised by the labels on it (two screens that share
most of their labels are the same screen, whatever the session list happens to show).  An
edge is a click, and what that click revealed.

On a user's machine maps are learned passively, from goals — every click there already passed
the safety gates.  The explorer, which clicks what looks like navigation and backs out again,
is a maintainer's tool for building the maps that ship, run against a disposable instance.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "maps"
LEARNED = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "omarchy-voice" / "maps"

SAME_SCREEN = 0.9          # share of labels two screens must share to be one screen (a pane switch is not)
NAV_ROLES = {"button", "a", "link", "tab", "menuitem", "treeitem", "li"}
CHANGES = re.compile(      # a label that does something rather than going somewhere
    r"\b(new|create|add|delete|remove|send|start|stop|run|reset|clear|archive|pin|unpin|reorder|rename|"
    r"duplicate|copy|export|import|install|uninstall|update|upgrade|enable|disable|toggle|switch|turn|"
    r"dark|light|system|hide|show|collapse|expand|close|minimi[sz]e|maximi[sz]e|quit|exit|log ?out|"
    r"log ?in|sign|subscribe|buy|pay|save|apply|confirm|submit|share|upload|download|record|mute|"
    r"play|pause|refresh|reload|retry|edit|filter|sort|swap)\b", re.I)


def labels(snapshot: dict) -> set[str]:
    """What identifies a screen: its short, stable labels (not the rows of a list)."""
    out = set()
    for e in snapshot.get("elements") or []:
        text = " ".join(str(e.get("text", "")).split())
        if 0 < len(text) <= 40:
            out.add(text.lower())
    return out


def worth_naming(found: set[str]) -> list[str]:
    """What a click revealed, as a person would name it: "appearance", not "90%" or "on"."""
    words = [l for l in found if 4 <= len(l) <= 26 and re.search(r"[a-z]{3}", l) and not re.search(r"\d%|^\d", l)]
    return sorted(words, key=lambda l: (len(l.split()) > 3, l))[:12]


def action_key(target: dict | None) -> str:
    if not target:
        return ""
    return f"{target.get('role', '')}|{' '.join(str(target.get('text', '')).split()).lower()}"


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class AppMap:
    def __init__(self, app: str) -> None:
        self.app = app
        self.nodes: list[dict] = []          # {"id", "labels": [...], "seen": int}
        self.edges: list[dict] = []          # {"from", "action", "role", "text", "to", "reveals": [...]}
        self.dirty = False
        for path in (SHIPPED / f"{app}.json", LEARNED / f"{app}.json"):
            self._merge(path)

    # -- storage --------------------------------------------------------------------------
    def _merge(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        remap = {}
        for node in data.get("nodes", []):
            remap[node["id"]] = self._node(set(node.get("labels", [])))
        for edge in data.get("edges", []):
            a, b = remap.get(edge["from"]), remap.get(edge["to"])
            if a is not None and b is not None:
                self._edge(a, edge["action"], edge.get("role", ""), edge.get("text", ""), b, edge.get("reveals", []),
                           asked=edge.get("asked", False))
        self.dirty = False

    def save(self) -> None:
        if not self.dirty:
            return
        LEARNED.mkdir(parents=True, exist_ok=True)
        (LEARNED / f"{self.app}.json").write_text(json.dumps(
            {"app": self.app, "nodes": self.nodes, "edges": self.edges, "saved": int(time.time())}, indent=1))
        self.dirty = False

    # -- building -------------------------------------------------------------------------
    def _node(self, found: set[str]) -> int:
        best, score = None, 0.0
        for node in self.nodes:
            s = _similar(found, set(node["labels"]))
            if s > score:
                best, score = node, s
        if best is not None and score >= SAME_SCREEN:
            best["seen"] = best.get("seen", 0) + 1   # labels stay as first seen, or every screen "shows" everything
            return best["id"]
        node = {"id": len(self.nodes), "labels": sorted(found)[:400], "seen": 1}
        self.nodes.append(node)
        self.dirty = True
        return node["id"]

    def _edge(self, a: int, action: str, role: str, text: str, b: int, reveals: list[str], asked: bool = False) -> None:
        for edge in self.edges:
            if edge["from"] == a and edge["action"] == action:
                edge["to"], edge["reveals"] = b, reveals[:12] or edge.get("reveals", [])
                edge["asked"] = edge.get("asked", False) or asked   # once it needed approval, it always does
                self.dirty = True
                return
        self.edges.append({"from": a, "action": action, "role": role, "text": text, "to": b,
                           "reveals": reveals[:12], "asked": asked})
        self.dirty = True

    def node_for(self, snapshot: dict) -> int:
        return self._node(labels(snapshot))

    def record(self, before: dict, step, after: dict) -> None:
        """One click and what it did — the goal loop and the explorer both report here."""
        if not step.target or step.kind not in ("click_link", "click_button"):
            return
        was, now = labels(before), labels(after)
        if _similar(was, now) > 0.97:
            return                           # nothing changed: not a way anywhere
        a, b = self._node(was), self._node(now)
        revealed = worth_naming(now - was)
        if a == b and not revealed:
            return
        self._edge(a, action_key(step.target), step.target.get("role", ""), step.target.get("text", ""), b, revealed,
                   asked=bool(getattr(step, "asked", False)))

    # -- using ----------------------------------------------------------------------------
    def hints(self, snapshot: dict) -> dict[str, list[str]]:
        """For each element on this screen: what clicking it is known to reveal."""
        here = self._find(labels(snapshot))
        if here is None:
            return {}
        known = {e["action"]: e for e in self.edges if e["from"] == here}
        out = {}
        for el in snapshot.get("elements") or []:
            edge = known.get(action_key(el))
            if edge and edge.get("reveals"):
                out[el["id"]] = [r for r in edge["reveals"] if r][:6]
        return out

    def leads_somewhere(self, snapshot: dict, element: dict) -> bool:
        """Has clicking this, on this screen, only ever opened another screen or pane?"""
        here = self._find(labels(snapshot))
        if here is None:
            return False
        key = action_key(element)
        # a click that once needed approval is never "just navigation", whatever it opened
        return any(e["from"] == here and e["action"] == key and not e.get("asked")
                   and (e["to"] != here or e.get("reveals")) for e in self.edges)

    def _find(self, found: set[str]) -> int | None:
        best, score = None, 0.0
        for node in self.nodes:
            s = _similar(found, set(node["labels"]))
            if s > score:
                best, score = node["id"], s
        return best if score >= SAME_SCREEN else None

    def route(self, snapshot: dict, target: str, max_depth: int = 4) -> list[dict] | None:
        """The clicks from this screen to one that shows `target` — or None if the map does not know.

        Returns [{"role", "text"}, …]; empty when the target is already on this screen.
        """
        want = " ".join(target.lower().split())
        if not want:
            return None
        here = self._find(labels(snapshot))
        if here is None:
            return None

        def has(found) -> bool:
            return any(want == l or (len(want) > 3 and want in l.split()) or l.startswith(want + " ")
                       for l in found)

        def path_to(node: int) -> list[dict]:
            path = []
            while came[node] is not None:
                parent, e = came[node]
                path.append({"role": e["role"], "text": e["text"]})
                node = parent
            return list(reversed(path))

        if has(labels(snapshot)):
            return []
        queue, came = deque([(here, 0)]), {here: None}
        while queue:
            node, depth = queue.popleft()
            for edge in (e for e in self.edges if e["from"] == node):
                if has(edge.get("reveals", [])):
                    return path_to(node) + [{"role": edge["role"], "text": edge["text"]}]
                if edge["to"] in came or depth + 1 >= max_depth:
                    continue
                came[edge["to"]] = (node, edge)
                if has(self.nodes[edge["to"]]["labels"]):
                    return path_to(edge["to"])
                queue.append((edge["to"], depth + 1))
        return None

    def summary(self) -> str:
        return f"{len(self.nodes)} screens, {len(self.edges)} ways between them"


# -- exploring ---------------------------------------------------------------------------
def navigational(element: dict) -> bool:
    """Would clicking this only go somewhere? Anything that might change something is left alone."""
    text = " ".join(str(element.get("text", "")).split())
    if not text or len(text) > 32 or len(text.split()) > 4:
        return False                         # rows of a list and long sentences are content
    if element.get("is_input") or str(element.get("state", "")) not in ("", "None", "null"):
        return False                         # fields, switches, checkboxes, selected options
    if str(element.get("role", "")).lower() not in NAV_ROLES:
        return False
    return not CHANGES.search(text)


def explore(bridge, app: str, limit: int = 30, dry_run: bool = True, log=print) -> AppMap:
    """Click every navigational element once, record where it leads, and back out again.

    Dry by default, and meant for a disposable instance only: on the real, connected Hermes the
    "navigational" buttons included "Restore checkpoint" and suggested prompts that start agent
    tasks, and the app marked its chat history — not its settings — as navigation. No label
    heuristic makes exploring a live app safe; on a user's machine maps are learned from goals,
    whose every click already passed the safety gates.

    One level deep from the screen it starts on, then one level from each screen it found —
    enough to learn "settings → appearance" without wandering into content.
    """
    amap = AppMap(app)
    start = bridge.snapshot_for(None, timeout=10)
    if not start:
        raise RuntimeError("the app did not answer")
    home = labels(start)
    clicks = 0

    def back_to(want: set[str]) -> bool:
        now = bridge.snapshot_for(None, timeout=8)
        if now and _similar(labels(now), want) >= SAME_SCREEN:
            return True                    # a pane switch: still on the same screen
        for attempt in ({"type": "press_key", "key": "Escape"}, {"type": "go_back"}):
            bridge.execute(attempt)
            time.sleep(0.5)
            now = bridge.snapshot_for(None, timeout=8)
            if now and _similar(labels(now), want) >= SAME_SCREEN:
                return True
        now = bridge.snapshot_for(None, timeout=8) or {}
        for el in now.get("elements") or []:                 # a visible way out
            if re.fullmatch(r"(close|back|done|cancel)( \w+)?", str(el.get("text", "")).strip(), re.I):
                bridge.execute({"type": "click_element", "targetId": el["id"]})
                time.sleep(0.5)
                now = bridge.snapshot_for(None, timeout=8)
                if now and _similar(labels(now), want) >= SAME_SCREEN:
                    return True
        return False

    def visit(snapshot: dict, depth: int) -> None:
        nonlocal clicks
        here = labels(snapshot)
        candidates = [e for e in snapshot.get("elements") or [] if navigational(e)]
        seen = set()
        for el in candidates:
            key = action_key(el)
            if key in seen or clicks >= limit:
                continue
            seen.add(key)
            if dry_run:
                log(f"  would click {el.get('role')} “{el.get('text')}”")
                clicks += 1
                continue
            before = bridge.snapshot_for(None, timeout=8)
            if not before or _similar(labels(before), here) < SAME_SCREEN:
                log("  lost the screen it started from — stopping this branch")
                return
            fresh = next((e for e in before.get("elements") or [] if action_key(e) == key), None)
            if fresh is None:
                continue
            bridge.execute({"type": "click_element", "targetId": fresh["id"]})
            clicks += 1
            time.sleep(0.8)
            after = bridge.snapshot_for(None, timeout=8)
            if not after:
                continue
            step = type("Step", (), {"target": {"role": fresh.get("role", ""), "text": fresh.get("text", "")},
                                     "kind": "click_button"})()
            amap.record(before, step, after)
            moved = _similar(labels(before), labels(after)) <= 0.97
            log(f"  {'→' if moved else '·'} {fresh.get('text')}" + (f"  reveals {', '.join(worth_naming(labels(after) - labels(before))[:5])}" if moved else ""))
            if moved and depth < 1:
                visit(after, depth + 1)
            if moved and not back_to(here):
                log("  could not find the way back — stopping this branch")
                return

    visit(start, 0)
    if not dry_run:
        amap.save()
    return amap
