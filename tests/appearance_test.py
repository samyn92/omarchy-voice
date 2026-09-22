"""The browser must keep its own light/dark appearance while voice control is attached.

Playwright emulates `prefers-color-scheme: light` on every page it attaches to, which
would drag the user's browser out of dark mode for as long as we are connected.  This
starts a throwaway browser, connects the way the daemon does and asks the page itself.

    uv run python tests/appearance_test.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import browser as browser_mod

PORT = 9334   # not 9222/9223: never touch the user's own browser
PAGE = urllib.parse.quote("""<html><body><h1>appearance</h1><script>
const m = matchMedia('(prefers-color-scheme: dark)');
const show = () => document.title = m.matches ? 'DARK' : 'LIGHT';
m.addEventListener('change', show); show();
</script></body></html>""")
URL = "data:text/html," + PAGE


def titles() -> list[str]:
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2))
    return [t["title"] for t in tabs if t["type"] == "page"]


def main() -> int:
    binary = next((shutil.which(b) for b in ("helium", "helium-browser", "chromium", "brave", "google-chrome-stable")
                   if shutil.which(b)), None)
    if not binary:
        print("No Chromium-family browser installed — skipped.")
        return 0

    profile = tempfile.mkdtemp(prefix="omarchy-voice-appearance-")
    proc = subprocess.Popen([binary, f"--user-data-dir={profile}", f"--remote-debugging-port={PORT}",
                             "--no-first-run", "--no-default-browser-check", URL],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = 0
    try:
        for _ in range(60):
            try:
                if titles() and titles()[0] in ("DARK", "LIGHT"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        want = titles()[0]          # whatever this desktop's browser does on its own
        print(f"browser on its own: {want}")

        bridge = browser_mod.BrowserBridge((f"http://127.0.0.1:{PORT}",))
        executor = bridge._call(lambda: bridge._ex())
        time.sleep(1)
        for label, got in (("while connected", titles()),):
            ok = all(t == want for t in got)
            failures += not ok
            print(("ok    " if ok else "FAIL  ") + f"{label}: {got}")

        bridge._call(lambda: executor._page.goto(URL))
        time.sleep(1.5)
        ok = all(t == want for t in titles())
        failures += not ok
        print(("ok    " if ok else "FAIL  ") + f"after navigating: {titles()}")

        bridge._call(lambda: executor._browser.contexts[0].new_page().goto(URL))
        time.sleep(1.5)
        ok = all(t == want for t in titles())
        failures += not ok
        print(("ok    " if ok else "FAIL  ") + f"a tab opened while connected: {titles()}")
    finally:
        try:      # let go of the browser before it dies, or Playwright's driver complains loudly
            bridge._call(lambda: executor._browser.close())
            bridge._call(lambda: executor._pw.stop())
        except Exception:
            pass
        proc.terminate()

    print(f"\n{3 - failures}/3 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
