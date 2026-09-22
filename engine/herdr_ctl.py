"""herdr (terminal workspace manager for coding agents) as voice-controllable things.

When the focused window runs a herdr client, its session is controlled through
herdr's socket API (`herdr --session <name> …`): agents, tabs, panes and
workspaces are focused, zoomed, prompted and sent keys by id — no simulated
prefix-key chords, and it works whatever keybindings are configured.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# the daemon may itself have been started from inside herdr: never inherit that session's pane context
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
ATTENTION = {"blocked": 0, "done": 1, "idle": 2, "unknown": 3, "working": 4}


def _children(pid: int) -> list[int]:
    try:
        return [int(c) for c in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except OSError:
        return []


def session_of_window(pid: int, depth: int = 4) -> str | None:
    """The herdr session a terminal window shows ("default" or a name), or None if it isn't herdr."""
    todo = [(pid, 0)]
    while todo:
        p, d = todo.pop()
        try:
            argv = Path(f"/proc/{p}/cmdline").read_bytes().split(b"\0")
            comm = Path(f"/proc/{p}/comm").read_text().strip()
        except OSError:
            continue
        if comm == "herdr":
            args = [a.decode(errors="replace") for a in argv[1:] if a]
            if "server" in args[:1]:
                continue
            if "--session" in args and args.index("--session") + 1 < len(args):
                return args[args.index("--session") + 1]
            if args[:2] == ["session", "attach"] and len(args) > 2:
                return args[2]
            return "default"
        if d < depth:
            todo += [(c, d + 1) for c in _children(p)]
    return None


def cli(session: str, *args: str, timeout: float = 5.0) -> dict:
    argv = ["herdr"] + (["--session", session] if session and session != "default" else []) + list(args)
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=CLEAN_ENV)
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        data = {}
    if out.returncode and "error" not in data:
        data["error"] = {"message": (out.stderr or out.stdout).strip()[:200] or f"herdr exited {out.returncode}"}
    return data


def ok(reply: dict) -> tuple[bool, str]:
    if "error" in reply:
        return False, reply["error"].get("message", "herdr error")
    return True, "ok"


@dataclass
class Agent:
    id: str               # a1, a2 … for the decision model
    pane_id: str
    kind: str             # claude, omp, codex …
    status: str
    title: str
    workspace: str        # workspace label
    workspace_id: str
    focused: bool

    def describe(self) -> str:
        flags = ", ".join(f for f in (self.status, "focused" if self.focused else "") if f)
        title = self.title.strip()[:60]
        return f"{self.id} {self.kind} agent" + (f" “{title}”" if title else "") + f" in workspace “{self.workspace}” [{flags}]"


@dataclass
class Herdr:
    session: str
    workspaces: list[dict] = field(default_factory=list)
    panes: list[dict] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)

    @property
    def focused_pane(self) -> dict | None:
        return next((p for p in self.panes if p.get("focused")), None)

    @property
    def focused_agent(self) -> Agent | None:
        return next((a for a in self.agents if a.focused), None)

    def by_id(self, agent_id: str) -> Agent | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    # -- reading ----------------------------------------------------------------
    @classmethod
    def load(cls, session: str) -> "Herdr":
        h = cls(session)
        h.workspaces = cli(session, "workspace", "list").get("result", {}).get("workspaces", [])
        h.panes = cli(session, "pane", "list").get("result", {}).get("panes", [])
        agents = cli(session, "agent", "list").get("result", {}).get("agents", [])
        labels = {w["workspace_id"]: w.get("label", "") for w in h.workspaces}
        number = {w["workspace_id"]: w.get("number", 99) for w in h.workspaces}
        # herdr lists agents in sidebar order; only group by workspace number, never re-sort pane ids as text
        agents.sort(key=lambda a: number.get(a["workspace_id"], 99))
        for i, a in enumerate(agents, 1):
            h.agents.append(Agent(
                id=f"a{i}", pane_id=a["pane_id"], kind=a.get("agent", "agent"), status=a.get("agent_status", "unknown"),
                title=re.sub(r"^(?:\S{1,2}\s*>\s*|\W+\s*)", "", a.get("terminal_title_stripped") or a.get("terminal_title") or ""),
                workspace=labels.get(a["workspace_id"], a["workspace_id"]), workspace_id=a["workspace_id"],
                focused=bool(a.get("focused")),
            ))
        return h

    def summary(self) -> str:
        if not self.agents:
            return "No agents are running."
        parts = []
        for status in ("blocked", "done", "working", "idle", "unknown"):
            names = [a.workspace for a in self.agents if a.status == status]
            if names:
                parts.append(f"{len(names)} {status}: " + ", ".join(names))
        return ". ".join(parts) + "."

    # -- acting ---------------------------------------------------------------
    def focus_agent(self, agent: Agent) -> tuple[bool, str]:
        return ok(cli(self.session, "agent", "focus", agent.pane_id))

    def step_agent(self, delta: int) -> tuple[bool, str, Agent | None]:
        if not self.agents:
            return False, "no agents", None
        current = next((i for i, a in enumerate(self.agents) if a.focused), -1 if delta > 0 else 0)
        target = self.agents[(current + delta) % len(self.agents)]
        good, msg = self.focus_agent(target)
        return good, msg, target

    def attention_agent(self) -> Agent | None:
        """The agent that most needs the user: blocked, then done, then idle — never the focused one if avoidable."""
        waiting = [a for a in self.agents if a.status in ("blocked", "done", "idle")]
        waiting.sort(key=lambda a: (a.focused, ATTENTION.get(a.status, 9)))
        return waiting[0] if waiting else None

    def step_tab(self, delta: int) -> tuple[bool, str]:
        pane = self.focused_pane
        if not pane:
            return False, "no focused pane"
        tabs = cli(self.session, "tab", "list", "--workspace", pane["workspace_id"]).get("result", {}).get("tabs", [])
        if len(tabs) < 2:
            return False, "only one tab"
        current = next((i for i, t in enumerate(tabs) if t.get("tab_id") == pane.get("tab_id")), 0)
        return ok(cli(self.session, "tab", "focus", tabs[(current + delta) % len(tabs)]["tab_id"]))

    def step_workspace(self, delta: int) -> tuple[bool, str]:
        spaces = sorted(self.workspaces, key=lambda w: w.get("number", 99))
        if len(spaces) < 2:
            return False, "only one workspace"
        current = next((i for i, w in enumerate(spaces) if w.get("focused")), 0)
        return ok(cli(self.session, "workspace", "focus", spaces[(current + delta) % len(spaces)]["workspace_id"]))

    def focus_workspace(self, workspace_id: str) -> tuple[bool, str]:
        return ok(cli(self.session, "workspace", "focus", workspace_id))

    def focus_pane(self, direction: str) -> tuple[bool, str]:
        pane = self.focused_pane
        args = ["pane", "focus", "--direction", direction] + (["--pane", pane["pane_id"]] if pane else [])
        return ok(cli(self.session, *args))

    def zoom(self) -> tuple[bool, str]:
        pane = self.focused_pane
        return ok(cli(self.session, "pane", "zoom", *([pane["pane_id"]] if pane else [])))

    def prompt(self, agent: Agent, text: str) -> tuple[bool, str]:
        """Text + Enter to an agent without switching to it (bracketed paste aware)."""
        return ok(cli(self.session, "agent", "prompt", agent.pane_id, text, timeout=15))

    def start_agent(self, kind: str, env: dict | None = None, timeout_ms: int = 45000) -> tuple[bool, str]:
        """A new tab in the focused workspace (same folder as the focused pane) running a new agent."""
        import time as _time

        pane = self.focused_pane
        workspace = pane["workspace_id"] if pane else next((w["workspace_id"] for w in self.workspaces if w.get("focused")), None)
        args = ["tab", "create", "--focus", "--label", kind]
        if workspace:
            args += ["--workspace", workspace]
        if pane and pane.get("cwd"):
            args += ["--cwd", pane["cwd"]]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        created = cli(self.session, *args)
        if "error" in created:
            return False, created["error"].get("message", "could not create a tab")
        new_pane = created["result"]["root_pane"]["pane_id"]
        name = f"{kind}-{int(_time.time()) % 100000}"
        _time.sleep(0.8)   # let the shell reach its prompt
        started = cli(self.session, "agent", "start", name, "--kind", kind, "--pane", new_pane,
                      "--timeout", str(timeout_ms), timeout=timeout_ms / 1000 + 5)
        if "error" in started:
            code = started["error"].get("code", "")
            if code == "agent_not_ready":      # e.g. a first-run trust dialog: the agent is there, waiting for the user
                return True, f"{kind} started — it is asking something"
            return False, started["error"].get("message", f"{kind} did not start")
        return True, f"{kind} ready"

    def send_keys(self, pane_id: str, *keys: str) -> tuple[bool, str]:
        return ok(cli(self.session, "pane", "send-keys", pane_id, *keys))
