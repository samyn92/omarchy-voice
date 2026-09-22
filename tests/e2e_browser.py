"""End-to-end test of browser control, through the real microphone path.

Starts its own visible Chromium-family browser (Helium, Chromium, Brave or Chrome)
with a throwaway profile and remote debugging on the standard port 9222, so the
user's own browser and profile are never touched; CDP is only used to read back
where a tab went.  The same commands then run once with CDP disabled, the
way a browser without remote debugging is driven (address bar only).

    uv run python e2e_browser.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import browsers
import desktop
import e2e


def main() -> int:
    from playwright.sync_api import sync_playwright

    binary = next((shutil.which(b) for b in ("helium", "helium-browser", "chromium", "brave", "google-chrome-stable") if shutil.which(b)), None)
    if not binary:
        print("No Chromium-family browser installed.")
        return 2
    profile = tempfile.mkdtemp(prefix="omarchy-voice-e2e-browser-")
    test_browser = subprocess.Popen([binary, f"--user-data-dir={profile}", "--remote-debugging-port=9222",
                                     "--no-first-run", "--no-default-browser-check", "about:blank"],
                                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = e2e.wait_for(lambda: browsers.cdp_endpoint(("http://127.0.0.1:9222",)), 15)
    if not endpoint:
        test_browser.terminate()
        print("The test browser did not open its DevTools endpoint.")
        return 2
    orig_source = e2e.sh("pactl", "get-default-source")
    desk0 = desktop.snapshot()
    was_listening = (e2e.RUN / "omarchy-voice" / "listening").exists()
    module = e2e.sh("pactl", "load-module", "module-null-sink", f"sink_name={e2e.SINK}", check=True)
    pw = sync_playwright().start()
    cdp = pw.chromium.connect_over_cdp(endpoint)
    tab = cdp.contexts[0].pages[0] if cdp.contexts[0].pages else cdp.contexts[0].new_page()
    tab.goto("https://example.org/", wait_until="domcontentloaded")
    tab.bring_to_front()

    target = cdp.contexts[0].new_cdp_session(tab).send("Target.getTargetInfo")["targetInfo"]["targetId"]

    def url() -> str:
        # from the DevTools endpoint: Playwright's sync tab.url is stale while this thread sleeps
        return browsers.tab_urls(endpoint).get(target, "")

    (e2e.RUN / 'omarchy-voice' / 'testing').touch()   # the daemon marks what it hears now as test speech
    try:
        e2e.sh("pactl", "set-default-source", f"{e2e.SINK}.monitor", check=True)
        e2e.sh("systemctl", "--user", "restart", "omarchy-voice.service")
        time.sleep(9)
        e2e.sh(str(Path.home() / ".local/bin/omarchy-voice"), "start")
        time.sleep(1)
        win = next(w for w in desktop.snapshot().windows if browsers.is_chromium(w.cls) and "Example Domain" in w.title)
        desktop.focus_window(win)
        desktop.move_cursor(win.x + win.w // 2, win.y + win.h // 2)
        time.sleep(0.4)

        e2e.say("Search for voice control on Linux.", settle=2.5)
        e2e.wait_for(lambda: "example.org" not in url(), 6)
        e2e.check("plain search uses the browser's own engine (address bar)",
                  "voice" in url() and "linux" in url().lower() and "example.org" not in url(), f"{urlparse(url()).hostname} · {url()[:90]}")

        e2e.say("Search YouTube for stone techno.", settle=2.5)
        e2e.wait_for(lambda: "youtube.com/results" in url(), 6)
        e2e.check("'search YouTube for …' uses YouTube's search", "youtube.com/results" in url() and "stone" in url().lower(), url()[:90])

        e2e.say("Go to example dot com.", settle=2.5)
        e2e.wait_for(lambda: urlparse(url()).hostname == "example.com", 6)
        e2e.check("'go to example dot com' opens it", urlparse(url()).hostname == "example.com", url())

        e2e.say("Go back.", settle=2.0)
        e2e.wait_for(lambda: "youtube.com" in url(), 6)
        e2e.check("'go back' returns to the previous page", "youtube.com" in url(), url()[:90])

        # which virtual keyboards does a Chromium browser accept? (address bar: Ctrl+L, text, Enter)
        from brain import send_keys, type_text

        desktop.focus_window(next(w for w in desktop.snapshot().windows if w.address == win.address))
        time.sleep(0.3)
        subprocess.run(["wtype", "-M", "ctrl", "-k", "l", "-m", "ctrl", "-s", "150", "example.net", "-s", "80", "-k", "Return"])
        time.sleep(2.0)
        e2e.check("info: wtype reaches Chromium's address bar", urlparse(url()).hostname == "example.net", url())
        send_keys("CTRL", "l"); time.sleep(0.15); type_text("example.com"); send_keys("", "Delete"); send_keys("", "Return")
        e2e.wait_for(lambda: urlparse(url()).hostname == "example.com", 4)
        e2e.check("kernel keyboard reaches Chromium's address bar", urlparse(url()).hostname == "example.com", url())

        # the same, as a browser without remote debugging would be driven: keyboard only
        import omarchy_voice as sv
        from brain import Brain
        from browser import BrowserBridge

        bridge = BrowserBridge()
        bridge.available = lambda: False
        brain = Brain(sv.ACTIONS, sv.exact_match, sv.normalize, {"hud": False, "speak": False}, bridge)
        desktop.focus_window(next(w for w in desktop.snapshot().windows if w.address == win.address))
        time.sleep(0.3)
        for said, host in (("go to example dot net", "example.net"), ("search for hyprland wiki", None)):
            decision = brain.decide(said)
            ok, detail = brain.execute(decision.action, said)
            time.sleep(2.0)
            landed = urlparse(url()).hostname == host if host else ("hyprland" in url() and "example." not in url())
            e2e.check(f"without CDP: '{said}'", ok and landed, f"{decision.action.kind} · {url()[:80]}")
    finally:
        (e2e.RUN / 'omarchy-voice' / 'testing').unlink(missing_ok=True)
        e2e.sh("pactl", "set-default-source", orig_source)
        e2e.sh("pactl", "unload-module", module)
        try:
            pw.stop()
        finally:
            test_browser.terminate()
            try:
                test_browser.wait(10)   # it writes its profile until it has exited
            except subprocess.TimeoutExpired:
                test_browser.kill()
            shutil.rmtree(profile, ignore_errors=True)
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
