"""Virtual pointer through /dev/uinput — no ydotool, no daemon, no sudo.

The user is in the `input` group, which owns /dev/uinput.  Hyprland places the
cursor (`hl.dsp.cursor.move`, exact logical coordinates, no acceleration); this
device only supplies buttons and wheel, which go to whatever surface is under
the cursor.  A one-pixel jiggle after each warp makes the compositor refresh
pointer focus before the button event arrives.
"""

from __future__ import annotations

import fcntl
import os
import struct
import threading
import time

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0B, 0x0C
BUS_VIRTUAL = 0x06

BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x113, "forward": 0x114}

_EVENT = struct.Struct("llHHi")


class Pointer:
    def __init__(self, name: str = "omarchy-voice-pointer") -> None:
        self._lock = threading.Lock()
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
        for code in BUTTONS.values():
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES):
            fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
        setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x1209, 0x4A56, 1, name.encode()[:79], 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.5)  # let the compositor pick up the new device; earlier clicks are dropped

    def _emit(self, *events: tuple[int, int, int]) -> None:
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        data = b"".join(_EVENT.pack(sec, usec, t, c, v) for t, c, v in events)
        data += _EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, data)

    def jiggle(self) -> None:
        with self._lock:
            self._emit((EV_REL, REL_X, 1))
            self._emit((EV_REL, REL_X, -1))

    def button(self, name: str = "left", down: bool = True) -> None:
        with self._lock:
            self._emit((EV_KEY, BUTTONS[name], 1 if down else 0))

    def click(self, name: str = "left", count: int = 1) -> None:
        for i in range(count):
            self.button(name, True)
            time.sleep(0.012)
            self.button(name, False)
            if i + 1 < count:
                time.sleep(0.06)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        """Positive notches scroll down (or right)."""
        if not notches:
            return
        step = 1 if notches > 0 else -1
        code, hires = (REL_HWHEEL, REL_HWHEEL_HI_RES) if horizontal else (REL_WHEEL, REL_WHEEL_HI_RES)
        # evdev wheel is positive *up* / *right*; invert vertical to keep "positive = down".
        sign = step if horizontal else -step
        with self._lock:
            for _ in range(abs(notches)):
                self._emit((EV_REL, code, sign), (EV_REL, hires, sign * 120))
                time.sleep(0.008)

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


_pointer: Pointer | None = None


def pointer() -> Pointer:
    global _pointer
    if _pointer is None:
        _pointer = Pointer()
    return _pointer
