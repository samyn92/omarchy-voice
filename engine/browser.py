"""Thread-confined bridge to the Playwright executor.

Playwright's sync API must stay on the thread that created it, while the voice
daemon perceives and acts from several threads.  Every call is shipped to one
worker thread; a dead CDP connection is re-opened on the next call.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future

import browsers


class BrowserBridge:
    def __init__(self, cdp_candidates=browsers.DEFAULT_CDP) -> None:
        self.candidates = cdp_candidates
        self.cdp_url: str | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._executor = None
        self._down_until = 0.0   # after a failed connect, don't retry (and stall perception) for a while
        threading.Thread(target=self._run, name="browser-bridge", daemon=True).start()

    def _run(self) -> None:
        while True:
            fn, fut = self._jobs.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except Exception as exc:
                    fut.set_exception(exc)

    def _call(self, fn, timeout: float = 20.0):
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut.result(timeout=timeout)

    def _ex(self):
        from executor import Executor

        if self._executor is not None:
            try:
                self._executor._browser.contexts  # noqa: B018 — raises if the connection died
                if self._executor._browser.is_connected():
                    return self._executor
            except Exception:
                pass
            try:
                self._executor.close()
            except Exception:
                pass
            self._executor = None
        self.cdp_url = browsers.cdp_endpoint(self.candidates)
        if not self.cdp_url:
            raise ConnectionError("no browser with remote debugging (start it with --remote-debugging-port=9222)")
        executor = Executor.__new__(Executor)
        executor.cdp_url = self.cdp_url
        executor._pw = executor._browser = executor._page = None
        from playwright.sync_api import sync_playwright

        executor._pw = sync_playwright().start()
        executor._browser = executor._pw.chromium.connect_over_cdp(self.cdp_url, timeout=3000)
        executor._keep_appearance()
        executor._select_page()
        self._executor = executor
        return executor

    def snapshot_for(self, window_title: str, timeout: float = 3.0) -> dict | None:
        if time.monotonic() < self._down_until:
            return None
        try:
            return self._call(lambda: self._ex().snapshot(window_title), timeout)
        except Exception:
            self._down_until = time.monotonic() + 30
            return None

    def available(self) -> bool:
        """A browser answers on a DevTools endpoint (cheap; re-checked every 30 s after a miss)."""
        if time.monotonic() < self._down_until:
            return False
        if browsers.cdp_endpoint(self.candidates, timeout=0.2):
            return True
        self._down_until = time.monotonic() + 30
        return False

    def current_url(self, window_title: str | None = None) -> str:
        def run():
            ex = self._ex()
            page = ex.select_page_for_title(window_title) if window_title else ex._select_page()
            return page.url
        try:
            return self._call(run, 3)
        except Exception:
            return ""

    def execute(self, action: dict, window_title: str | None = None) -> dict:
        def run():
            ex = self._ex()
            if window_title:
                ex.select_page_for_title(window_title)
            return ex.execute(action)

        try:
            return self._call(run, 40)
        except Exception as exc:
            return {"ok": False, "outcome": f"browser unavailable: {exc}"[:200]}

    def page_count(self) -> int:
        try:
            return self._call(lambda: self._ex().page_count, 3)
        except Exception:
            return 0
