"""End-to-end test through the real microphone path.

A PipeWire null sink becomes the default source for the duration of the test;
Piper speech is played into it, so the voice daemon (VAD → both Whispers →
brain → actions) and voxtype (record → transcribe → post-process → type) hear
exactly what a person would say.  Everything is restored afterwards.

    uv run python e2e.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import io
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import desktop
import perceive
from uinput import pointer

RUN = Path(os.environ["XDG_RUNTIME_DIR"])
TRACE = RUN / "omarchy-voice" / "trace.json"
STATE = RUN / "omarchy-voice" / "state.json"
OUT = Path(os.environ.get("E2E_OUT", "/tmp/omarchy-voice-e2e"))
OUT.mkdir(parents=True, exist_ok=True)
SINK = "jev_e2e"
CLICKLOG = OUT / "entry.log"

results: list[tuple[str, bool, str]] = []


def sh(*argv: str, check: bool = False) -> str:
    r = subprocess.run(argv, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{argv}: {r.stderr}")
    return r.stdout.strip()


def voice():
    from piper import PiperVoice

    import voiceio
    return PiperVoice.load(voiceio.VOICE)


VOICE = None


def say(text: str, settle: float = 1.6) -> None:
    """Speak into the virtual microphone, then leave room for VAD + transcription."""
    global VOICE
    VOICE = VOICE or voice()
    path = OUT / "say.wav"
    with wave.open(str(path), "wb") as w:
        VOICE.synthesize_wav(text, w)
    subprocess.run(["pw-play", "--target", SINK, str(path)], check=True)
    time.sleep(settle)


def trace() -> list[dict]:
    try:
        return json.loads(TRACE.read_text())["entries"]
    except (OSError, ValueError):
        return []


def state() -> dict:
    return json.loads(STATE.read_text())


def last_id() -> int:
    t = trace()
    return t[0]["id"] if t else 0


def new_entries(since: int) -> list[dict]:
    return [e for e in reversed(trace()) if e.get("id", 0) > since]


def wait_for(pred, timeout: float = 8.0, step: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return None


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)


def words(text: str) -> str:
    import re

    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def voxtype() -> str:
    return sh("cat", str(RUN / "voxtype" / "state"))


def settled_entry(timeout: float = 15.0) -> str:
    """voxtype types one character at a time: wait until it is idle and the field stops changing."""
    wait_for(lambda: voxtype() == "idle", timeout)
    last, stable_since = entry_text(), time.monotonic()
    end = time.monotonic() + timeout
    while time.monotonic() < end and time.monotonic() - stable_since < 1.2:
        time.sleep(0.2)
        now = entry_text()
        if now != last:
            last, stable_since = now, time.monotonic()
    return last


def entry_text() -> str:
    lines = [l for l in CLICKLOG.read_text().splitlines() if l.startswith("entry ")] if CLICKLOG.exists() else []
    return lines[-1][6:] if lines else ""


def clicks() -> list[str]:
    return [l for l in CLICKLOG.read_text().splitlines() if l.startswith("clicked")] if CLICKLOG.exists() else []


def shot(name: str, rect: tuple[int, int, int, int] | None = None) -> None:
    argv = ["grim"] + (["-g", f"{rect[0]},{rect[1]} {rect[2]}x{rect[3]}"] if rect else []) + [str(OUT / f"{name}.png")]
    subprocess.run(argv)


def main() -> int:
    orig_source = sh("pactl", "get-default-source")
    desk0 = desktop.snapshot()
    orig_focus, orig_cursor = desk0.focused, desk0.cursor
    was_listening = (RUN / "omarchy-voice" / "listening").exists()
    module = sh("pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
                f"sink_properties=device.description={SINK}", check=True)
    app = None
    (RUN / 'omarchy-voice' / 'testing').touch()   # the daemon marks what it hears now as test speech
    try:
        sh("pactl", "set-default-source", f"{SINK}.monitor", check=True)
        sh("systemctl", "--user", "restart", "voxtype.service")
        sh("systemctl", "--user", "restart", "omarchy-voice.service")
        time.sleep(9)
        sh(str(Path.home() / ".local/bin/omarchy-voice"), "start")
        time.sleep(1)

        # a test window with a text field, floating in the middle, field focused
        CLICKLOG.unlink(missing_ok=True)
        app = subprocess.Popen(["/usr/bin/python3", str(Path(__file__).with_name("e2e_app.py")), str(CLICKLOG)],
                               start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        win = wait_for(lambda: next((w for w in desktop.snapshot().windows if w.cls == "dev.jev.e2e"), None), 6)
        d = desktop.snapshot()
        desktop.window_verb("snap", win, d, region="center")
        time.sleep(0.5)
        win = next(w for w in desktop.snapshot().windows if w.cls == "dev.jev.e2e")
        desktop.focus_window(win)
        desktop.move_cursor(win.x + win.w // 2, win.y + win.h - 80)
        pointer().jiggle()
        time.sleep(0.4)

        # -- 1. plain commands through the microphone ----------------------------
        mark = last_id()
        say("Show numbers.")
        e = wait_for(lambda: [x for x in new_entries(mark) if x["action"]])
        check("mic → 'show numbers' shows hints", e and "hint" in e[-1]["action"].lower(), e and e[-1]["action"])
        shot("1-hints", (win.x - 20, win.y - 30, win.w + 40, win.h + 60))

        mark = last_id()
        say("Select two.")
        e = wait_for(lambda: [x for x in new_entries(mark) if x["action"]])
        time.sleep(0.5)
        check("mic → 'select two' clicks hint 2", clicks()[-1:] == ["clicked Delete everything"], f"{e and e[-1]['action']} · log {clicks()[-1:]}")

        # -- 2. regression: a model guess must never switch into a typing mode ----
        mark = last_id()
        say("Remove all characters.", settle=2.5)
        wait_for(lambda: new_entries(mark), 6)
        check("'remove all characters' does not enter a typing mode", state().get("mode", "") not in ("dictation", "transcribe"), f"mode={state().get('mode')!r}")

        # -- 3. long transcription: "start transcribe" … pauses allowed … "end transcribe"
        el = next(e for e in perceive.A11y().elements(win) if e["editable"])
        desktop.move_cursor(el["x"] + el["w"] // 2, el["y"] + el["h"] // 2)
        pointer().jiggle(); time.sleep(0.05); pointer().click()
        time.sleep(0.3)
        mark = last_id()
        say("Start transcribe.", settle=1.8)
        ok = wait_for(lambda: voxtype() == "recording", 5)
        check("'start transcribe' starts a long voxtype session", ok and state().get("transcribe_kind") == "long",
              f"voxtype={voxtype()} kind={state().get('transcribe_kind')!r}")
        say("Hello from the end to end test. Please close this window and scroll down.", settle=1.5)
        shot("3-transcribing-hud")
        time.sleep(4.5)   # longer than the quick-mode silence: a long session must keep recording
        check("long session survives a 5 second pause", voxtype() == "recording", voxtype())
        say("Please transcribe the minutes and send them.", settle=1.2)   # fluent speech with trigger words in it
        check("'transcribe' and 'send' inside a sentence do not end it", voxtype() == "recording", voxtype())
        say("End transcribe.", settle=1.0)
        text = settled_entry()
        check("voxtype typed exactly the passage", words(text) == "hello from the end to end test please close this window and scroll down"
              " please transcribe the minutes and send them", repr(text))
        check("commands inside the passage were not executed", not any(x["action"] and x["route"] not in ("voxtype", "local") or "Close" in x["action"] for x in new_entries(mark) if x["route"] == "jev"),
              "; ".join(f"{x['route']}:{x['heard']}->{x['action']}" for x in new_entries(mark)))
        wait_for(lambda: state().get("mode", "") == "", 5)
        check("mode back to commands after transcription", state().get("mode", "") == "", f"mode={state().get('mode')!r}")

        # -- 4. quick transcription: "transcribe" … ends by itself after 3 s of silence
        before = entry_text()
        say("Transcribe.", settle=1.8)
        wait_for(lambda: voxtype() == "recording", 5)
        check("'transcribe' starts a quick session", state().get("transcribe_kind") == "quick", state().get("transcribe_kind"))
        say("Quick message, no stop word.", settle=0.5)
        stopped = wait_for(lambda: voxtype() != "recording", 8)
        text2 = settled_entry()
        added = text2[len(before):]
        check("quick session ends by itself after the silence", stopped and state().get("mode", "") == "", f"voxtype={voxtype()} mode={state().get('mode')!r}")
        check("quick session typed exactly its passage", words(added) == "quick message no stop word", repr(added))

        # -- 4b. a sentence that merely starts like a command is not a fast command
        mark = last_id()
        say("Next workspace, select left, close this window.", settle=2.5)
        wait_for(lambda: new_entries(mark), 6)
        fast_hits = [x for x in new_entries(mark) if x["route"] == "local" and x["action"]]
        check("long sentence is not taken as the 'next workspace' shortcut", not fast_hits, "; ".join(f"{x['route']}:{x['action']}" for x in new_entries(mark)))
        if desktop.snapshot().active_workspace != desk0.active_workspace:
            desktop.go_workspace(str(desk0.active_workspace)); time.sleep(0.3)
            desktop.focus_window(next(w for w in desktop.snapshot().windows if w.cls == "dev.jev.e2e"))

        # -- 6. the bar panel is open (it holds the keyboard): the text must still land in the field
        def focus_field():
            w = next(x for x in desktop.snapshot().windows if x.cls == "dev.jev.e2e")
            desktop.focus_window(w)
            f = next(e for e in perceive.A11y().elements(w) if e["editable"])
            desktop.move_cursor(f["x"] + f["w"] // 2, f["y"] + f["h"] // 2)
            pointer().jiggle(); time.sleep(0.05); pointer().click(); time.sleep(0.3)
            from brain import send_keys
            send_keys("CTRL", "a"); send_keys("", "BackSpace"); time.sleep(0.3)   # empty field, caret at the start

        focus_field()
        before = entry_text()
        say("Transcribe.", settle=1.8)
        wait_for(lambda: voxtype() == "recording", 5)
        px, py = (int(v) for v in os.environ.get("E2E_PANEL", "4398,16").split(","))
        desktop.move_cursor(px, py); pointer().jiggle(); time.sleep(0.1); pointer().click(); time.sleep(0.8)
        shot("6-panel-open", (px - 500, 30, 1100, 420))
        say("The panel is open.", settle=1.2)
        say("End transcribe.", settle=1.0)
        added = settled_entry()[len(before):]
        shot("6-panel-after", (px - 500, 30, 1100, 420))
        check("panel open during transcription: text still lands in the field", words(added) == "the panel is open", repr(added))

        # -- 7. "transcribe off" when nothing is being transcribed does nothing
        focus_field()
        before = entry_text()
        mark = last_id()
        say("Transcribe off.", settle=2.0)
        time.sleep(1.0)
        check("'transcribe off' outside a session starts nothing and types nothing",
              voxtype() == "idle" and entry_text() == before, f"voxtype={voxtype()} added={entry_text()[len(before):]!r}")

        # -- 8. start phrase and text in one breath
        before = entry_text()
        say("Transcribe, one breath start.", settle=1.5)
        wait_for(lambda: voxtype() == "recording", 5)
        say("And the rest.", settle=1.2)
        say("End transcribe.", settle=1.0)
        added = settled_entry()[len(before):]
        check("'transcribe, <text>' keeps the text said in the same breath", words(added) == "one breath start and the rest", repr(added))

        # -- 9. workspaces by ordinal, resize words, apps with a count
        start_ws = desktop.snapshot().active_workspace
        target_ws, word = (3, "third") if start_ws == 2 else (2, "second")
        say(f"Go to the {word} workspace.", settle=1.5)
        check(f"'go to the {word} workspace'", wait_for(lambda: desktop.snapshot().active_workspace == target_ws, 4),
              f"active={desktop.snapshot().active_workspace}")
        desktop.go_workspace(str(start_ws)); time.sleep(0.4)

        w = next(x for x in desktop.snapshot().windows if x.cls == "dev.jev.e2e")
        desktop.focus_window(w); time.sleep(0.3)
        width = w.w
        say("Expand window.", settle=1.5)
        grown = wait_for(lambda: next(x for x in desktop.snapshot().windows if x.cls == "dev.jev.e2e").w > width, 4)
        check("'expand window' makes it wider", grown, f"{width} -> {next(x for x in desktop.snapshot().windows if x.cls == 'dev.jev.e2e').w}")

        before = {x.address for x in desktop.snapshot().windows}
        say("Open two terminals.", settle=2.5)
        fresh = wait_for(lambda: [x for x in desktop.snapshot().windows if x.address not in before and "foot" in x.cls.lower()]
                         if len([x for x in desktop.snapshot().windows if x.address not in before and "foot" in x.cls.lower()]) >= 2 else None, 6) or []
        check("'open two terminals' opens two new terminal windows", len(fresh) == 2, f"{len(fresh)} new")
        for x in fresh:
            desktop.window_verb("close", x, desktop.snapshot())
        time.sleep(0.4)
        desktop.focus_window(next(x for x in desktop.snapshot().windows if x.cls == "dev.jev.e2e"))

        # -- 5. dictating with voxtype directly (F9): voice control must step aside
        mark = last_id()
        wait_for(lambda: voxtype() == "idle", 10)
        sh("voxtype", "record", "start")
        check("F9-style voxtype recording started", wait_for(lambda: voxtype() == "recording", 3), voxtype())
        say("Next workspace. Select left. Close this window.", settle=2.0)
        shot("5-f9-popup")
        sh("voxtype", "record", "cancel")
        time.sleep(0.8)
        acted = [x for x in new_entries(mark) if x["action"]]
        check("while F9 dictation runs, nothing is executed", not acted, "; ".join(x["action"] for x in acted) or "only voxtype rows")
        check("workspace unchanged", desktop.snapshot().active_workspace == desk0.active_workspace)
    finally:
        (RUN / 'omarchy-voice' / 'testing').unlink(missing_ok=True)
        sh("pactl", "set-default-source", orig_source)
        sh("pactl", "unload-module", module)
        if app:
            app.terminate()
        sh("systemctl", "--user", "restart", "voxtype.service")
        sh("systemctl", "--user", "restart", "omarchy-voice.service")
        time.sleep(3)
        sh(str(Path.home() / ".local/bin/omarchy-voice"), "start" if was_listening else "stop")
        if orig_focus:
            desktop.focus_window(orig_focus)
        desktop.move_cursor(*orig_cursor)
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} passed · screenshots in {OUT}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
