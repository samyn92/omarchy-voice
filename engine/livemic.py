"""Continuous live-mode capture: mic -> Silero neural VAD -> voxtype transcribe.

Live mode semantics (vs. push-to-talk):
  tap CTRL+F9 once  -> live mode ON: continuous listening, hands free
  tap CTRL+F9 again -> live mode OFF

Why Silero and not energy thresholds: this machine's room has music playing, and
music beats / mains hum sit exactly where any RMS threshold lives, so an energy
VAD either never starts or never ends. Silero is a small neural speech detector
(the same one whisper uses internally); it ignores music and hum, runs in ~1-3 ms
per 1 s of audio on CPU, and needs no tuning per room.

Transcription stays `voxtype transcribe` — GPU whisper large-v3-turbo as a pure
function: wav in, text out, nothing typed anywhere.
"""

import os
import queue
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

SPOOL = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")) / "omarchy-voice" / "spool"
SPOOL.mkdir(parents=True, exist_ok=True)
WAV = SPOOL / "live.wav"

WHISPER_RATE = 16000
BLOCK_MS = 30
VAD_EVERY_MS = 60           # how often we ask Silero (~0.3ms per call)
VAD_WINDOW_S = 0.6          # context per decision: shorter = less end-lag
TAIL_S = 0.25               # speech reaching this close to "now" still counts as speaking
PAD_S = 0.20                # non-speech after speech before we cut the utterance
PREROLL_S = 0.35            # audio kept before speech onset so the first word is whole
MAX_UTTERANCE_S = 20


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x.astype(np.float32, copy=False)
    n = int(len(x) * dst / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def save_wav(audio: np.ndarray, path=WAV) -> str:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(WHISPER_RATE)
        w.writeframes(pcm.tobytes())
    return str(path)


_fallback = None


def transcribe(path: str):
    """voxtype transcribe (GPU Whisper large-v3-turbo) when installed, else an in-process Whisper."""
    import shutil

    if not shutil.which("voxtype"):
        global _fallback
        if _fallback is None:
            _fallback = FastWhisper(os.environ.get("OMARCHY_VOICE_FALLBACK_MODEL", "small.en"))
        return _fallback.transcribe(path)
    try:
        out = subprocess.run(["voxtype", "transcribe", path],
                             capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    if not lines:
        return None
    text = lines[-1]
    if not text or text.startswith(("INFO", "WARN", "ERROR")):
        return None
    return text


class MicrophoneLost(RuntimeError):
    pass


def reset_audio() -> None:
    """Re-read the audio device list: PortAudio caches it at start-up, so after a
    device change the old default can no longer be opened ("host API not found")."""
    try:
        sd._terminate()
    except Exception:
        pass
    sd._initialize()


class LiveVAD:
    """Keeps every block it hears; Silero decides where the speech is."""

    def __init__(self, device=None):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self._timestamps = get_speech_timestamps
        # speech_pad_ms must be 0 here: Silero otherwise extends each segment
        # 400ms past its end, which makes "speech reaches the window tail" true
        # forever and stalls the utterance until the safety limit.
        self._opts = VadOptions(speech_pad_ms=0, min_silence_duration_ms=100,
                                min_speech_duration_ms=100)
        self.device = device
        dev = sd.query_devices(device if device is not None else sd.default.device[0])
        self.rate = int(dev["default_samplerate"])
        self.block = max(1, int(self.rate * BLOCK_MS / 1000))
        self.blocks_per_vad = max(1, VAD_EVERY_MS // BLOCK_MS)
        self.blocks_per_preroll = max(1, int(PREROLL_S * 1000 / BLOCK_MS))

    def _speech_at_tail(self, buf16: np.ndarray):
        """Silero's last speech end, in seconds from the START of buf16, or None.

        Returned as an absolute offset so the caller can compute how long ago
        speech actually stopped — far more robust than a boolean tail test.
        """
        ts = self._timestamps(buf16, self._opts)
        if not ts:
            return None
        return ts[-1]["end"] / WHISPER_RATE

    def record_utterance(self, active_check, max_seconds=MAX_UTTERANCE_S, on_speech=None):
        """Block until one utterance completes.

        `on_speech` (optional) is called once, from this thread, the moment speech
        is first detected — early enough to start work that runs while the user talks.

        Returns (audio_16k_float32 | None, reason) with reason in
        'utterance' | 'timeout' | 'disabled' | 'device'.
        Toggling live mode off mid-utterance still delivers what was said.
        """
        q: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(indata.copy())

        try:
            stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="int16",
                                     blocksize=self.block, device=self.device,
                                     callback=callback)
        except Exception:
            return None, "device"

        heard: list[np.ndarray] = []      # every block, in order
        since_vad = 0
        speech_start = None               # index into `heard`
        last_speech_at = None
        t0 = time.time()

        with stream:
            while True:
                if not active_check():
                    if speech_start is not None:
                        break                 # deliver what was said
                    return None, "disabled"
                try:
                    data = q.get(timeout=0.2)
                except queue.Empty:
                    continue

                heard.append(data.flatten().astype(np.float32) / 32768.0)
                since_vad += 1

                if since_vad >= self.blocks_per_vad:
                    since_vad = 0
                    n_window = int(VAD_WINDOW_S * self.rate)
                    window = np.concatenate(heard[-self.blocks_per_vad - int(n_window / self.block) + 1:])
                    window16 = _resample(window[-n_window:], self.rate, WHISPER_RATE)
                    last_end = self._speech_at_tail(window16)
                    window_s = len(window16) / WHISPER_RATE
                    # how many seconds ago did speech stop (>=0 means still speech)
                    silence_ago = (window_s - last_end) if last_end is not None else None

                    if silence_ago is not None and silence_ago <= TAIL_S:
                        last_speech_at = time.time()
                        if speech_start is None:
                            speech_start = max(0, len(heard) - self.blocks_per_preroll - self.blocks_per_vad)
                            if on_speech:
                                try:
                                    on_speech()
                                except Exception:
                                    pass
                    elif speech_start is not None and last_speech_at:
                        if time.time() - last_speech_at >= PAD_S:
                            break

                if speech_start is not None and time.time() - t0 >= max_seconds:
                    break

        if speech_start is None or not heard:
            return None, "timeout"

        audio = np.concatenate(heard[speech_start:])
        audio16 = _resample(audio, self.rate, WHISPER_RATE)
        if len(audio16) < WHISPER_RATE // 4:
            return None, "timeout"
        return audio16, "utterance"


    def utterances(self, active_check, on_speech=None, max_seconds=MAX_UTTERANCE_S, on_end=None):
        """Yield one 16 kHz utterance after another from a microphone stream that stays open.

        Unlike record_utterance(), nothing is lost while the caller is busy with the
        previous utterance: the stream keeps filling a queue, and segmentation picks
        up where it left off.  Ends when active_check() turns false.
        """
        q: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(indata.copy())

        stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="int16",
                                blocksize=self.block, device=self.device, callback=callback)
        keep_idle = self.blocks_per_preroll + self.blocks_per_vad + int(VAD_WINDOW_S * self.rate / self.block) + 2
        n_window = int(VAD_WINDOW_S * self.rate)
        heard: list[np.ndarray] = []
        since_vad, speech_start, last_speech_at, started_at = 0, None, None, 0.0
        # block timestamps come from the audio clock (block count), not the wall clock, so a
        # backlog built up while the caller was busy is segmented exactly as it was spoken
        block_s = self.block / self.rate
        clock = 0.0
        last_block = time.monotonic()
        with stream:
            while True:
                if not active_check():
                    if speech_start is not None:
                        audio = np.concatenate(heard[speech_start:])
                        yield _resample(audio, self.rate, WHISPER_RATE)
                    return
                try:
                    data = q.get(timeout=0.2)
                except queue.Empty:
                    # a device that vanishes (unplugged, monitor hub switched) just stops sending
                    if time.monotonic() - last_block > 3.0:
                        raise MicrophoneLost("the microphone stopped delivering audio")
                    continue
                last_block = time.monotonic()
                clock += block_s
                heard.append(data.flatten().astype(np.float32) / 32768.0)
                since_vad += 1
                if speech_start is None and len(heard) > keep_idle:
                    del heard[: len(heard) - keep_idle]   # silence: keep only what a preroll needs
                if since_vad < self.blocks_per_vad:
                    continue
                since_vad = 0
                window = np.concatenate(heard[-(int(n_window / self.block) + 1):])
                window16 = _resample(window[-n_window:], self.rate, WHISPER_RATE)
                last_end = self._speech_at_tail(window16)
                silence_ago = (len(window16) / WHISPER_RATE - last_end) if last_end is not None else None
                done = False
                if silence_ago is not None and silence_ago <= TAIL_S:
                    last_speech_at = clock
                    if speech_start is None:
                        speech_start = max(0, len(heard) - self.blocks_per_preroll - self.blocks_per_vad)
                        started_at = clock
                        if on_speech:
                            try:
                                on_speech()
                            except Exception:
                                pass
                elif speech_start is not None and last_speech_at is not None and clock - last_speech_at >= PAD_S:
                    done = True
                if speech_start is not None and clock - started_at >= max_seconds:
                    done = True
                if done:
                    if on_end:          # every stretch of speech ends, even blips too short to keep
                        try:
                            on_end()
                        except Exception:
                            pass
                    audio = np.concatenate(heard[speech_start:])
                    heard = heard[-keep_idle:]
                    speech_start, last_speech_at = None, None
                    audio16 = _resample(audio, self.rate, WHISPER_RATE)
                    if len(audio16) >= WHISPER_RATE // 4:
                        yield audio16


_default: LiveVAD | None = None


def record_utterance(active_check, device=None, max_seconds=MAX_UTTERANCE_S, on_speech=None):
    """Module-level convenience so main.py stays unchanged."""
    global _default
    if _default is None:
        _default = LiveVAD(device)
    return _default.record_utterance(active_check, max_seconds, on_speech)

class FastWhisper:
    """A small Whisper kept loaded in-process: ~180 ms per command on CPU.

    Less robust than voxtype's large model, so callers only act on its output
    directly for short, unambiguous commands and otherwise use it to start
    work early while the accurate transcript is still being made.
    """

    def __init__(self, model: str = "base.en", threads: int = 8):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)

    def transcribe(self, audio16: np.ndarray):
        segments, _ = self.model.transcribe(
            audio16, language="en", beam_size=1, vad_filter=False,
            without_timestamps=True, condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments).strip()
        return text or None
