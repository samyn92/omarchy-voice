"""End-to-end test of herdr voice control, through the real microphone path.

Builds an isolated herdr session ("voice-e2e") with three stand-in agents — copies
of `cat` named `claude`, so herdr detects them natively and every byte they are
sent lands in a file — then speaks commands into a virtual microphone (see
e2e.py).  The user's own herdr session is only read, never changed.

    uv run python e2e_herdr.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import desktop
import e2e
import herdr_ctl as H

SESSION = "voice-e2e"
WORK = e2e.OUT / "herdr"
AGENTS = (("alpha", "idle"), ("beta", "working"), ("gamma", "blocked"))


def received(label: str) -> str:
    try:
        return (WORK / f"{label}.txt").read_text().replace("\x00", "")
    except OSError:
        return ""


def focused_agent() -> str | None:
    a = H.Herdr.load(SESSION).focused_agent
    return a.workspace if a else None


def build_session() -> subprocess.Popen:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "bin").mkdir(exist_ok=True)
    shutil.copy("/usr/bin/cat", WORK / "bin" / "claude")
    for label, _ in AGENTS:
        (WORK / f"{label}.txt").unlink(missing_ok=True)
    # panes of the test session find the stand-in `claude` first, so starting an agent never runs the real one
    env = {**H.CLEAN_ENV, "PATH": f"{WORK / 'bin'}:{H.CLEAN_ENV.get('PATH', '')}"}
    subprocess.run(["herdr", "session", "stop", SESSION], env=H.CLEAN_ENV, capture_output=True)
    window = subprocess.Popen(["foot", "--app-id=jev.herdr-e2e", "herdr", "session", "attach", SESSION],
                              env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    e2e.wait_for(lambda: "error" not in H.cli(SESSION, "workspace", "list"), 10)
    for w in H.cli(SESSION, "workspace", "list").get("result", {}).get("workspaces", []):
        H.cli(SESSION, "workspace", "close", w["workspace_id"])
    for label, _ in AGENTS:
        pane = H.cli(SESSION, "workspace", "create", "--label", label)["result"]["root_pane"]["pane_id"]
        time.sleep(0.8)
        H.cli(SESSION, "pane", "run", pane, f"stty -icanon -echo; exec {WORK}/bin/claude > {WORK}/{label}.txt")
    e2e.wait_for(lambda: len(H.Herdr.load(SESSION).agents) == len(AGENTS), 10)
    for agent, (_, state) in zip(H.Herdr.load(SESSION).agents, AGENTS):
        H.cli(SESSION, "pane", "report-agent", "--source", "voice-e2e", "--agent", "claude", "--state", state, agent.pane_id)
    return window


def main() -> int:
    real_focus = [p["pane_id"] for p in H.cli("default", "pane", "list").get("result", {}).get("panes", []) if p.get("focused")]
    orig_source = e2e.sh("pactl", "get-default-source")
    desk0 = desktop.snapshot()
    was_listening = (e2e.RUN / "omarchy-voice" / "listening").exists()
    module = e2e.sh("pactl", "load-module", "module-null-sink", f"sink_name={e2e.SINK}", check=True)
    window = None
    (e2e.RUN / 'omarchy-voice' / 'testing').touch()   # the daemon marks what it hears now as test speech
    try:
        window = build_session()
        e2e.sh("pactl", "set-default-source", f"{e2e.SINK}.monitor", check=True)
        e2e.sh("systemctl", "--user", "restart", "voxtype.service")
        e2e.sh("systemctl", "--user", "restart", "omarchy-voice.service")
        time.sleep(9)
        e2e.sh(str(Path.home() / ".local/bin/omarchy-voice"), "start")
        time.sleep(1)
        win = next(w for w in desktop.snapshot().windows if w.cls == "jev.herdr-e2e")
        desktop.focus_window(win)
        time.sleep(0.4)
        H.Herdr.load(SESSION).focus_agent(H.Herdr.load(SESSION).agents[0])

        e2e.say("Next agent.")
        e2e.check("mic → 'next agent'", e2e.wait_for(lambda: focused_agent() == "beta", 4), focused_agent())
        e2e.say("Who needs me?")
        e2e.check("mic → 'who needs me' finds the blocked agent", e2e.wait_for(lambda: focused_agent() == "gamma", 4), focused_agent())
        e2e.say("Agent one.")
        e2e.check("mic → 'agent one'", e2e.wait_for(lambda: focused_agent() == "alpha", 4), focused_agent())

        # the agent rows of herdr's sidebar, by position
        e2e.say("Select the third agent.")
        e2e.check("mic → 'select the third agent'", e2e.wait_for(lambda: focused_agent() == "gamma", 4), focused_agent())
        e2e.say("Select the second agent.")
        e2e.check("mic → 'select the second agent'", e2e.wait_for(lambda: focused_agent() == "beta", 4), focused_agent())
        e2e.say("Select the last agent.")
        e2e.check("mic → 'select the last agent'", e2e.wait_for(lambda: focused_agent() == "gamma", 4), focused_agent())
        e2e.say("Select first agent.")
        e2e.check("mic → 'select first agent'", e2e.wait_for(lambda: focused_agent() == "alpha", 4), focused_agent())

        # dictate to the focused agent with voxtype, and submit it in the same breath
        e2e.say("Transcribe.", settle=1.8)
        e2e.wait_for(lambda: e2e.voxtype() == "recording", 5)
        e2e.say("Please run the unit tests.", settle=1.2)
        e2e.say("End transcribe send.", settle=1.0)
        got = e2e.wait_for(lambda: received("alpha") if received("alpha").endswith("\n") else None, 15) or received("alpha")
        e2e.check("'transcribe … end transcribe send' types the prompt and presses Enter",
                  e2e.words(got) == "please run the unit tests" and got.endswith("\n"), repr(got))

        e2e.say("Tell the beta agent to check the logs.", settle=2.5)
        got = e2e.wait_for(lambda: received("beta") if received("beta").endswith("\n") else None, 8) or received("beta")
        e2e.check("'tell the beta agent to …' prompts it without switching", e2e.words(got) == "check the logs"
                  and focused_agent() == "alpha", f"{got!r} focused={focused_agent()}")

        e2e.say("Tell the gamma agent to continue.", settle=2.5)
        e2e.check("a blocked agent is not prompted", received("gamma") == "", repr(received("gamma")))

        # the voice launcher: a new tab running a new agent
        before = {p["pane_id"] for p in H.cli(SESSION, "pane", "list")["result"]["panes"]}
        e2e.say("New Claude.", settle=2.0)

        def launched():
            for p in H.cli(SESSION, "pane", "list")["result"]["panes"]:
                if p["pane_id"] not in before:
                    info = H.cli(SESSION, "pane", "process-info", "--pane", p["pane_id"]).get("result", {}).get("process_info", {})
                    names = [x.get("name") for x in info.get("foreground_processes", [])]
                    if "claude" in names:
                        return p["pane_id"]
            return None
        pane = e2e.wait_for(launched, 15)
        e2e.check("'New Claude.' opens a new tab running a claude agent", pane, pane or "no new claude pane")
        if pane:
            H.cli(SESSION, "pane", "close", pane)
        H.Herdr.load(SESSION).focus_agent(next(a for a in H.Herdr.load(SESSION).agents if a.workspace == "alpha"))

        e2e.say("The alpha agent is really slow today.", settle=2.5)
        e2e.check("chatter about an agent sends nothing", received("alpha").count("\n") == 1, repr(received("alpha")))

        now = [p["pane_id"] for p in H.cli("default", "pane", "list").get("result", {}).get("panes", []) if p.get("focused")]
        e2e.check("the real herdr session was not touched", now == real_focus, f"{real_focus} -> {now}")
    finally:
        (e2e.RUN / 'omarchy-voice' / 'testing').unlink(missing_ok=True)
        e2e.sh("pactl", "set-default-source", orig_source)
        e2e.sh("pactl", "unload-module", module)
        subprocess.run(["herdr", "session", "stop", SESSION], env=H.CLEAN_ENV, capture_output=True)
        if window:
            window.terminate()
        e2e.sh("systemctl", "--user", "restart", "voxtype.service")
        e2e.sh("systemctl", "--user", "restart", "omarchy-voice.service")
        time.sleep(3)
        e2e.sh(str(Path.home() / ".local/bin/omarchy-voice"), "start" if was_listening else "stop")
        if desk0.focused:
            desktop.focus_window(desk0.focused)
        desktop.move_cursor(*desk0.cursor)
    passed = sum(ok for _, ok, _ in e2e.results)
    print(f"\n{passed}/{len(e2e.results)} passed")
    return 0 if passed == len(e2e.results) else 1


if __name__ == "__main__":
    sys.exit(main())
