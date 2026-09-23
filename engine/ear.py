"""Client for the ear (see ear/): the Rust process that owns the microphone.

The engine used to capture audio, run voice activity detection and a Whisper model itself,
which cost about a gigabyte of memory and 150 ms per command.  When the ear is running it
does all three and publishes JSON lines on a socket:

    {"t": "level", "v": 0.42}                              every 25 ms, for the orb
    {"t": "speech"}                                        speech started
    {"t": "utterance", "text": "scroll down", "wav": "…", "ms": 26}
    {"t": "idle"}                                          the microphone was released

`wav` is the utterance on disk, so the accurate pass (voxtype) can read the same audio.
Nothing here is required: without the ear the engine falls back to capturing for itself.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-voice" / "ear.sock"


def available(path: Path = SOCKET) -> bool:
    return path.exists()


class Ear:
    """Reads the ear's stream, reconnecting for as long as the caller wants to listen."""

    def __init__(self, path: Path = SOCKET) -> None:
        self.path = path
        self.sock: socket.socket | None = None

    def connect(self, timeout: float = 2.0) -> bool:
        self.close()
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(self.path))
        except OSError:
            return False
        self.sock = sock
        return True

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def events(self, active_check):
        """Yield the ear's messages until `active_check()` turns false.

        A dropped connection is not an error: the ear may be restarting, so it is retried
        until the caller stops asking.
        """
        buffer = b""
        while active_check():
            if self.sock is None and not self.connect():
                time.sleep(1.0)
                continue
            try:
                chunk = self.sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                self.close()
                continue
            if not chunk:                      # the ear went away
                self.close()
                time.sleep(0.5)
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    pass                       # half a line: the next read completes it
        self.close()
