"""Skills and maps, end to end, offline.

A small "app" — a settings pane with sections, like Hermes — runs in a throwaway browser.
Jev is a script, so every case is exactly what it describes, and the scripted Jev refuses to
be asked at all where the point is that no model is needed.  Skills and maps go to a
temporary folder: nothing here touches what this machine has learned.

    uv run python tests/harness_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import http.server
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import appmap
import autopilot
import browser as browser_mod
import harness
import skills

PORT = 9338
APP = """<html><head><title>App</title></head><body>
<nav><button onclick="show('settings')">Open settings</button> <button>Sessions</button></nav>
<div id="settings" style="display:none">
  <button onclick="pane('general')">General</button>
  <button onclick="pane('appearance')">{APPEARANCE}</button>
  <button onclick="hide('settings')">Close settings</button>
  <div id="general" class="pane" style="display:none"><p>Language is English here.</p></div>
  <div id="appearance" class="pane" style="display:none">
    <p>Dark mode can be switched on here.</p>
    <button role="switch" aria-checked="false">Dark</button>
  </div>
</div>
<input placeholder="Search notes" onkeydown="if(event.key==='Enter'){document.getElementById('r').textContent='Results for ' + this.value + ' are listed below.'}">
<p id="r"></p>
<button>Delete account</button>
<script>
function show(i){document.getElementById(i).style.display='block'}
function hide(i){document.getElementById(i).style.display='none'}
function pane(i){for (const p of document.querySelectorAll('.pane')) p.style.display='none'; show(i)}
</script></body></html>"""
PAGES = {"/": APP.replace("{APPEARANCE}", "Appearance"),
         "/v2": APP.replace("{APPEARANCE}", "Look and feel")}   # the same app after an update

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                      # noqa: N802
        body = PAGES.get(self.path.split("?")[0], "<html><body>no</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def element(state, needle):
    for line in state["elements"]:
        if needle.lower() in line.lower():
            return _id(line)
    return "none"


def _id(line: str) -> str:
    for word in line.split():
        if len(word) == 3 and word[0] == "e" and word[1:].isdigit():
            return word
    return "none"


def line_with(state, needle):
    for line in state.get("page_text", []):
        if needle.lower() in line.lower():
            return line.split()[0]
    return "none"


def scripted(steps):
    """Jev, as a script. `None` means: this test must not reach Jev at all."""
    calls = {"n": 0}

    def decide(state, questions):
        if "verdict" in questions:
            return {"answers": {"verdict": {"choice": "safe"}, "reason": {"choice": "nothing_committed"}}, "usage": {}}
        if steps is None:
            raise AssertionError("Jev was asked, but a skill or the map should have done this")
        i = min(calls["n"], len(steps) - 1)
        calls["n"] += 1
        answer = steps[i](state)
        return {"answers": {k: {"choice": v} for k, v in answer.items()}, "usage": {}}

    decide.calls = calls
    return decide


def main() -> int:
    binary = next((shutil.which(b) for b in ("helium", "helium-browser", "chromium", "google-chrome-stable")
                   if shutil.which(b)), None)
    if not binary:
        print("No Chromium-family browser installed — skipped.")
        return 0

    data = _Path(tempfile.mkdtemp(prefix="omarchy-voice-harness-"))
    skills.LEARNED = data / "skills"
    skills.SHIPPED = data / "shipped"            # nothing shipped: the test starts from nothing
    appmap.LEARNED = data / "maps"
    appmap.SHIPPED = data / "shipped-maps"

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    proc = subprocess.Popen([binary, f"--user-data-dir={tempfile.mkdtemp()}", f"--remote-debugging-port={PORT}",
                             "--no-first-run", "--no-default-browser-check", base + "/"],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    bridge = browser_mod.BrowserBridge((f"http://127.0.0.1:{PORT}",))
    real = autopilot.jev.decide
    executor = None

    def open_page(path="/"):
        bridge.execute({"type": "navigate_url", "url": base + path})
        time.sleep(0.6)

    def work(goal, jev_steps, path="/"):
        open_page(path)
        autopilot.jev.decide = scripted(jev_steps)
        h = harness.Harness(skills.Library())
        return h.work(goal, harness.Target(bridge=bridge, app="testapp"))

    try:
        executor = bridge._call(lambda: bridge._ex())
        time.sleep(1)

        # 1. the first time, Jev works it out — and it is kept
        goal = "go to settings and find where to enable dark mode"
        first = work(goal, [
            lambda s: {"step": "click_button", "element": element(s, "Open settings"), "text": "none", "answer": "none"},
            lambda s: {"step": "click_button", "element": element(s, '"Appearance"'), "text": "none", "answer": "none"},
            lambda s: {"step": "done", "element": "none", "text": "none", "answer": line_with(s, "Dark mode")},
        ])
        check("the first time Jev works it out", first.run.status == "done" and first.used == "jev",
              f"{first.run.status} via {first.used}: {first.run.answer[:40]}")
        check("and it is kept as a skill", first.learned is not None and len(first.learned.steps) == 2,
              f"{first.learned.steps if first.learned else None}")

        # 2. the second time, no model at all
        t0 = time.perf_counter()
        second = work(goal, None)
        check("the second time the skill replays it without Jev",
              second.used == "skill" and second.run.status == "done" and "Dark mode" in second.run.answer,
              f"{second.used}, {second.run.status}, {time.perf_counter() - t0:.1f}s")

        # 3. said differently, still the same skill
        third = work("go to the settings and find where I can enable dark mode", None)
        check("a different wording finds the same skill", third.used == "skill", third.used)

        # 4. the map learned the way: "go to appearance" is a path, not a question
        fourth = work("go to appearance", None)
        check("the map routes to a place it has seen, without Jev",
              fourth.used == "map" and fourth.run.status == "done",
              f"{fourth.used}: {[s.label for s in fourth.run.steps]}")

        # 5. the app changed: the skill diverges and Jev takes over from there, then it relearns
        fifth = work(goal, [
            lambda s: {"step": "click_button", "element": element(s, "Look and feel"), "text": "none", "answer": "none"},
            lambda s: {"step": "done", "element": "none", "text": "none", "answer": line_with(s, "Dark mode")},
        ], path="/v2")
        check("after an app update the skill hands over to Jev and finishes",
              fifth.used == "skill+jev" and fifth.run.status == "done",
              f"{fifth.used}: {[s.label for s in fifth.run.steps]}")
        check("and what worked now replaces the old skill",
              fifth.learned is not None and any(st.get("text") == "Look and feel" for st in fifth.learned.steps)
              and len(skills.Library().for_app("testapp")) == 1,
              f"{len(skills.Library().for_app('testapp'))} skill(s)")

        # 6. what was typed becomes a slot
        open_page()
        work("search my notes for invoices", [
            lambda s: {"step": "type_text", "element": element(s, "Search notes"), "text": "invoices", "answer": "none"},
            lambda s: {"step": "press_enter", "element": "none", "text": "none", "answer": "none"},
            lambda s: {"step": "done", "element": "none", "text": "none", "answer": line_with(s, "Results for")},
        ])
        slot = work("search my notes for travel plans", None)
        check("what was typed became a slot: a new search replays without Jev",
              slot.used == "skill" and "travel plans" in slot.run.answer, f"{slot.used}: {slot.run.answer[:50]}")

        # 7. a replay never clicks something risky alone
        lib = skills.Library()
        lib.skills["testapp.risky"] = skills.Skill(id="testapp.risky", app="testapp", phrases=["clean up my account"],
                                                   steps=[{"kind": "click_button", "role": "button", "text": "Delete account"}])
        lib.save("testapp")
        open_page()
        run, diverged = skills.replay(lib.skills["testapp.risky"], bridge, {})
        check("a replay does not click a risky label on its own", diverged == 1 and not run.steps,
              f"diverged at {diverged}")
    finally:
        autopilot.jev.decide = real
        try:
            bridge._call(lambda: executor._browser.close())
            bridge._call(lambda: executor._pw.stop())
        except Exception:
            pass
        proc.terminate()
        server.shutdown()
        shutil.rmtree(data, ignore_errors=True)

    print(f"\n{9 - failures}/9 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
