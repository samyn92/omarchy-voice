"""The browser goal loop, offline: a local site, a throwaway browser, a scripted Jev.

Jev is replaced by a script, so every case here is exactly the situation it describes —
including the ones a real model would rarely produce (a judge that calls "Delete account"
safe).  Nothing is sent anywhere and nothing costs money.

    uv run python tests/autopilot_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import functools
import http.server
import json
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import autopilot
import browser as browser_mod

PORT_BROWSER = 9335
SITE = {
    "/": """<html><head><title>Shop</title></head><body>
        <h1>Shop</h1>
        <a href="/opening.html">Opening hours</a>
        <a href="/cart.html">Cart</a>
        <form action="/search.html"><input type="text" placeholder="Search the shop"></form>
        <button>Delete account</button>
        <label>PIN <input type="text" placeholder="PIN code"></label>
        </body></html>""",
    "/opening.html": """<html><head><title>Opening hours</title></head><body>
        <h1>Opening hours</h1><p id="answer">The market opens on 25 November 2026</p>
        <a href="/">Back</a></body></html>""",
    "/cart.html": """<html><head><title>Cart</title></head><body>
        <h1>Cart</h1><button>Pay 49 euro now</button><a href="/">Back</a></body></html>""",
}

failures = 0
site_port = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                      # noqa: N802
        body = SITE.get(self.path.split("?")[0], "<html><body>not here</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):             # keep the test output clean
        pass


def scripted(steps, judge=("safe", "nothing_committed")):
    """Stand in for jev.decide: hand back the next scripted answer, ignore the state."""
    calls = {"step": 0, "judge": 0}

    def decide(state, questions):
        if "verdict" in questions:         # the risk judge
            calls["judge"] += 1
            verdict, reason = judge(state) if callable(judge) else judge
            return {"answers": {"verdict": {"choice": verdict}, "reason": {"choice": reason}},
                    "latency_ms": 1, "usage": {}}
        i = min(calls["step"], len(steps) - 1)
        calls["step"] += 1
        answer = steps[i](state) if callable(steps[i]) else steps[i]
        return {"answers": {k: {"choice": v} for k, v in answer.items()}, "latency_ms": 1, "usage": {}}

    decide.calls = calls
    return decide


def line_named(state, needle):
    """The id of the first line of page text that mentions `needle` (t00, t01 …)."""
    for line in state.get("page_text", []):
        if needle.lower() in line.lower():
            return line.split()[0]
    return "none"


def element_named(state, needle):
    """The id of the first element in the state whose line mentions `needle`."""
    for line in state["elements"]:
        if needle.lower() in line.lower():
            return line.split()[0]
    return "none"


def main() -> int:
    global site_port
    binary = next((shutil.which(b) for b in ("helium", "helium-browser", "chromium", "brave", "google-chrome-stable")
                   if shutil.which(b)), None)
    if not binary:
        print("No Chromium-family browser installed — skipped.")
        return 0

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    site_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = f"http://127.0.0.1:{site_port}/"

    profile = tempfile.mkdtemp(prefix="omarchy-voice-autopilot-")
    proc = subprocess.Popen([binary, f"--user-data-dir={profile}", f"--remote-debugging-port={PORT_BROWSER}",
                             "--no-first-run", "--no-default-browser-check", home],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT_BROWSER}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    bridge = browser_mod.BrowserBridge((f"http://127.0.0.1:{PORT_BROWSER}",))
    real_decide = autopilot.jev.decide
    executor = None

    def go_home():
        bridge.execute({"type": "navigate_url", "url": home})

    def pilot(**kw):
        return autopilot.Autopilot(bridge, **kw)

    try:
        executor = bridge._call(lambda: bridge._ex())
        time.sleep(1)
        go_home()

        # 1. a read-only goal: follow the link, read the answer off the page
        autopilot.jev.decide = scripted([
            lambda s: {"step": "click_link", "element": element_named(s, "Opening hours"), "text": "none", "answer": "none"},
            lambda s: {"step": "done", "element": "none", "text": "none", "answer": line_named(s, "market opens")},
        ])
        run = pilot(stage=autopilot.LOOK).run("when does the market open")
        check("stage 1 reaches the answer", run.status == "done" and "25 November" in run.answer,
              f"{run.status}: {run.answer[:60]}")
        check("stage 1 took the steps it reported", len(run.steps) == 2, str([s.kind for s in run.steps]))

        # 2. stage 1 must not press buttons, even when told to
        go_home()
        autopilot.jev.decide = scripted([
            lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"},
        ])
        run = pilot(stage=autopilot.LOOK).run("delete my account")
        check("stage 1 refuses to press a button", run.status == "refused", run.status)

        # 3. the word list catches a risky click even when the judge says it is safe
        go_home()
        asked = []
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"}],
            judge=("safe", "nothing_committed"))
        run = pilot(stage=autopilot.FILL_IN,
                    ask=lambda label, reason: (asked.append(label), False)[1]).run("delete my account")
        check("a wrong 'safe' from the judge is caught by the word list", bool(asked) and run.status == "declined",
              f"asked={asked} status={run.status}")

        # 4. the judge alone can stop a step the word list would let through
        go_home()
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Cart"), "text": "none", "answer": "none"}],
            judge=("refuse", "spends_money"))
        run = pilot(stage=autopilot.FILL_IN, ask=lambda label, reason: True).run("empty my cart")
        check("the judge can refuse on its own", run.status == "refused", run.status)

        # 5. never type into a field that asks for a secret
        go_home()
        autopilot.jev.decide = scripted([
            lambda s: {"step": "type_text", "element": element_named(s, "PIN"), "text": "1234", "answer": "none"},
        ])
        run = pilot(stage=autopilot.FILL_IN, ask=lambda label, reason: True).run("type 1234 into the pin field")
        check("refuses to type into a secret field", run.status == "refused",
              f"{run.status}: {run.steps[-1].outcome if run.steps else ''}")

        # 6. saying stop ends the run before anything happens
        go_home()
        autopilot.jev.decide = scripted([{"step": "scroll_down", "element": "none", "text": "none", "answer": "none"}])
        run = pilot(stage=autopilot.FILL_IN, stop=lambda: True).run("scroll around")
        check("stop ends the run", run.status == "stopped" and not run.steps, run.status)

        # 7. a page that never changes is a dead end, not an endless loop
        go_home()
        autopilot.jev.decide = scripted([{"step": "scroll_up", "element": "none", "text": "none", "answer": "none"}])
        run = pilot(stage=autopilot.FILL_IN, max_steps=10).run("go nowhere")
        check("a page that stops changing ends the run", run.status == "stuck" and len(run.steps) <= 4,
              f"{run.status} after {len(run.steps)} steps")

        # 8. stage 3 on a trusted host: benign commits go through, money never does
        go_home()
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"},
             {"step": "stuck", "element": "none", "text": "none", "answer": "none"}],
            judge=("ask", "sends_or_posts"))
        asked = []
        run = pilot(stage=autopilot.TRUSTED, trusted_hosts=("127.0.0.1",),
                    ask=lambda label, reason: (asked.append(label), False)[1]).run("send it")
        check("stage 3 acts alone on a trusted host for benign commits", not asked, f"asked={asked}")

        go_home()
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"}],
            judge=("ask", "spends_money"))
        asked = []
        run = pilot(stage=autopilot.TRUSTED, trusted_hosts=("127.0.0.1",),
                    ask=lambda label, reason: (asked.append(label), False)[1]).run("pay it")
        check("stage 3 still asks when money is involved", bool(asked) and run.status == "declined",
              f"asked={asked} status={run.status}")

        # 9. a judge that says "safe" but gives a reason that must never pass alone is asked
        go_home()
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"}],
            judge=("safe", "changes_settings"))
        asked = []
        run = pilot(stage=autopilot.FILL_IN, ask=lambda label, reason: (asked.append(label), False)[1]).run("find it")
        check("safe-but-changes-settings is treated as ask", bool(asked), f"asked={asked}")

        # 10. an untrusted host gets no stage 3 treatment
        go_home()
        autopilot.jev.decide = scripted(
            [lambda s: {"step": "click_button", "element": element_named(s, "Delete account"), "text": "none", "answer": "none"}],
            judge=("ask", "sends_or_posts"))
        asked = []
        run = pilot(stage=autopilot.TRUSTED, trusted_hosts=("example.com",),
                    ask=lambda label, reason: (asked.append(label), False)[1]).run("send it")
        check("an untrusted host still asks", bool(asked), f"asked={asked}")

    finally:
        autopilot.jev.decide = real_decide
        try:
            bridge._call(lambda: executor._browser.close())
            bridge._call(lambda: executor._pw.stop())
        except Exception:
            pass
        proc.terminate()
        server.shutdown()

    total = 11
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
