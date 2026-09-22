"""A virtual keyboard that every application accepts, in the user's own layout.

`wtype` talks the Wayland virtual-keyboard protocol, which some clients ignore
(Chromium-based browsers and Electron apps drop its keys).  This keyboard is a
kernel uinput device instead — to applications it is indistinguishable from a
physical one.  The compositor interprets its keycodes with the user's layout,
so every character and shortcut is looked up in that layout via libxkbcommon
("z" on a German keyboard is the key labelled Y on a US one).  Characters the
layout cannot produce are pasted through the clipboard.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import json
import os
import struct
import subprocess
import threading
import time

UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
EV_SYN, EV_KEY = 0x00, 0x01
_EVENT = struct.Struct("llHHi")

# evdev codes of the modifier keys
KEY_LEFTSHIFT, KEY_LEFTCTRL, KEY_LEFTALT, KEY_LEFTMETA, KEY_RIGHTALT = 42, 29, 56, 125, 100
NAMED_MODS = {"SHIFT": KEY_LEFTSHIFT, "CTRL": KEY_LEFTCTRL, "CONTROL": KEY_LEFTCTRL, "ALT": KEY_LEFTALT,
              "SUPER": KEY_LEFTMETA, "META": KEY_LEFTMETA, "LOGO": KEY_LEFTMETA}
# xkb modifier names -> the key that produces them
XKB_MODS = {"Shift": KEY_LEFTSHIFT, "Control": KEY_LEFTCTRL, "Mod1": KEY_LEFTALT, "Mod4": KEY_LEFTMETA, "Mod5": KEY_RIGHTALT}


class _RuleNames(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


def _xkb():
    lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")
    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames), ctypes.c_int]
    for fn in ("xkb_keymap_min_keycode", "xkb_keymap_max_keycode"):
        getattr(lib, fn).restype = ctypes.c_uint32
        getattr(lib, fn).argtypes = [ctypes.c_void_p]
    lib.xkb_keymap_num_layouts.restype = ctypes.c_uint32
    lib.xkb_keymap_num_layouts.argtypes = [ctypes.c_void_p]
    lib.xkb_keymap_layout_get_name.restype = ctypes.c_char_p
    lib.xkb_keymap_layout_get_name.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.xkb_keymap_num_levels_for_key.restype = ctypes.c_uint32
    lib.xkb_keymap_num_levels_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                     ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    lib.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
    lib.xkb_keymap_key_get_mods_for_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                      ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t]
    lib.xkb_keymap_mod_get_index.restype = ctypes.c_uint32
    lib.xkb_keymap_mod_get_index.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.xkb_utf32_to_keysym.restype = ctypes.c_uint32
    lib.xkb_utf32_to_keysym.argtypes = [ctypes.c_uint32]
    lib.xkb_keysym_from_name.restype = ctypes.c_uint32
    lib.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
    return lib


def compositor_layout() -> dict:
    """The layout the compositor applies to keyboards: {rules, model, layout, variant, options, active}."""
    names = {"rules": "", "model": "", "layout": "us", "variant": "", "options": "", "active": ""}
    try:
        import desktop

        for key in ("rules", "model", "layout", "variant", "options"):
            value = json.loads(desktop.request(f"j/getoption input:kb_{key}")).get("str", "")
            names[key] = value.strip() or names[key]
        main = next((k for k in json.loads(desktop.request("j/devices")).get("keyboards", []) if k.get("main")), None)
        if main:
            names["active"] = main.get("active_keymap", "")
    except Exception:
        pass
    return names


class Keymap:
    """keysym -> (evdev keycode, modifier keys) for the active layout."""

    def __init__(self, names: dict | None = None) -> None:
        self.lib = _xkb()
        names = names or compositor_layout()
        ctx = self.lib.xkb_context_new(0)
        rn = _RuleNames(*(names.get(k, "").encode() or None for k in ("rules", "model", "layout", "variant", "options")))
        self.km = self.lib.xkb_keymap_new_from_names(ctx, ctypes.byref(rn), 0)
        if not self.km:
            raise RuntimeError(f"xkbcommon could not compile layout {names}")
        layout_index = 0
        for i in range(self.lib.xkb_keymap_num_layouts(self.km)):
            if (self.lib.xkb_keymap_layout_get_name(self.km, i) or b"").decode() == names.get("active"):
                layout_index = i
        mod_bits = {self.lib.xkb_keymap_mod_get_index(self.km, name.encode()): key for name, key in XKB_MODS.items()}
        self.table: dict[int, tuple[int, tuple[int, ...]]] = {}
        syms = ctypes.POINTER(ctypes.c_uint32)()
        masks = (ctypes.c_uint32 * 8)()
        for kc in range(self.lib.xkb_keymap_min_keycode(self.km), self.lib.xkb_keymap_max_keycode(self.km) + 1):
            for level in range(self.lib.xkb_keymap_num_levels_for_key(self.km, kc, layout_index)):
                n = self.lib.xkb_keymap_key_get_syms_by_level(self.km, kc, layout_index, level, ctypes.byref(syms))
                if n != 1:
                    continue
                count = self.lib.xkb_keymap_key_get_mods_for_level(self.km, kc, layout_index, level, masks, 8)
                options = []
                for m in range(count):
                    mask = masks[m]
                    keys = tuple(key for bit, key in mod_bits.items() if mask & (1 << bit))
                    covered = sum(1 << bit for bit in mod_bits if mask & (1 << bit))
                    if mask == covered:          # only modifiers we can press (never Lock)
                        options.append(keys)
                if not options:
                    continue
                mods = min(options, key=len)
                sym = syms[0]
                current = self.table.get(sym)
                if not 1 <= kc - 8 <= 248:
                    continue   # outside what the virtual device can press: typed via the clipboard instead
                if current is None or len(mods) < len(current[1]):
                    self.table[sym] = (kc - 8, mods)   # xkb keycodes are evdev codes + 8

    def for_char(self, ch: str):
        if ch == "\n":
            return self.for_name("Return")
        if ch == "\t":
            return self.for_name("Tab")
        return self.table.get(self.lib.xkb_utf32_to_keysym(ord(ch)))

    def for_name(self, name: str):
        sym = self.lib.xkb_keysym_from_name(name.encode(), 0) or self.lib.xkb_keysym_from_name(name.encode(), 1)
        return self.table.get(sym) if sym else None


class Keyboard:
    def __init__(self, name: str = "omarchy-voice-keyboard") -> None:
        self._lock = threading.Lock()
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for code in range(1, 249):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x06, 0x1209, 0x4A57, 1, name.encode()[:79], 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.5)   # let the compositor attach a keymap to the new device
        self.keymap = Keymap()
        self.layout = compositor_layout()

    def refresh_layout(self) -> None:
        now = compositor_layout()
        if now != self.layout:          # the user switched layouts
            self.keymap, self.layout = Keymap(now), now

    def _emit(self, code: int, value: int) -> None:
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        os.write(self.fd, _EVENT.pack(sec, usec, EV_KEY, code, value) + _EVENT.pack(sec, usec, EV_SYN, 0, 0))

    def _tap(self, code: int, mods: tuple[int, ...], delay: float = 0.004) -> None:
        for m in mods:
            self._emit(m, 1)
        self._emit(code, 1)
        time.sleep(delay)
        self._emit(code, 0)
        for m in reversed(mods):
            self._emit(m, 0)
        time.sleep(delay)

    def shortcut(self, mods: str, key: str, times: int = 1) -> bool:
        """mods like "CTRL SHIFT"; key an xkb keysym name ("l", "Return", "Left", "plus", "F5")."""
        with self._lock:
            self.refresh_layout()
            entry = self.keymap.for_name(key) or (self.keymap.for_char(key) if len(key) == 1 else None)
            if entry is None:
                return False
            code, level_mods = entry
            held = tuple(NAMED_MODS[m] for m in mods.upper().split() if m in NAMED_MODS)
            # a letter shortcut uses the letter's own key; shift-level symbols keep their shift
            extra = tuple(m for m in level_mods if m not in held) if not (len(key) == 1 and key.isalpha()) else ()
            for _ in range(max(1, times)):
                self._tap(code, held + extra)
                time.sleep(0.008)
            return True

    def type(self, text: str) -> bool:
        """Type text; runs of characters the layout cannot produce are pasted."""
        with self._lock:
            self.refresh_layout()
            pending = ""
            for ch in text:
                entry = self.keymap.for_char(ch)
                if entry is None:
                    pending += ch
                    continue
                if pending:
                    self._paste(pending)
                    pending = ""
                self._tap(*entry)
            if pending:
                self._paste(pending)
            return True

    def _paste(self, text: str) -> None:
        """Clipboard paste for characters outside the layout; the previous clipboard text is restored."""
        try:
            previous = subprocess.run(["wl-paste", "--no-newline", "--type", "text/plain"], capture_output=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            previous = None
        subprocess.run(["wl-copy", "--", text], timeout=2)
        time.sleep(0.05)
        v = self.keymap.for_char("v")
        if v:
            self._tap(v[0], (KEY_LEFTCTRL,))
        time.sleep(0.15)
        if previous:
            subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE).communicate(previous, timeout=2)


_keyboard: Keyboard | None = None
_kb_lock = threading.Lock()


def keyboard() -> Keyboard | None:
    """The shared virtual keyboard, or None when /dev/uinput is not writable (then wtype is used)."""
    global _keyboard
    with _kb_lock:
        if _keyboard is None:
            try:
                _keyboard = Keyboard()
            except OSError:
                return None
        return _keyboard
