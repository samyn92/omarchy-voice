"""Voice I/O, fully local: microphone -> Whisper STT -> text; text -> Piper TTS -> speakers.

STT: faster-whisper on CPU (tiny.en model, int8). Streaming chunks from the mic
with a simple energy VAD so silence ends an utterance.
TTS: piper with a local onnx voice, played through pipewire (pw-play) so we
don't fight the mic for the audio device.
"""

import os
import queue
import subprocess
import tempfile
import time
import wave

import numpy as np
import sounddevice as sd

WHISPER_RATE = 16000
CHANNELS = 1
BLOCK_MS = 30

# VAD: utterance ends after this much near-silence
SILENCE_MS = 900
# and starts after this much speech energy
PRE_SPEECH_MS = 250

# Piper voice downloaded by install.sh; OMARCHY_VOICE_TTS points at another .onnx voice
VOICE = os.environ.get("OMARCHY_VOICE_TTS") or os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "omarchy-voice", "voices", "en_US-amy-medium.onnx")


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))


class Listener:
    """Records one utterance at a time with energy VAD. blocking: record_utterance()."""

    def __init__(self, model_size="tiny.en", device=None):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self.device = device
        # use the device's native rate (the C920 mic is 48k; asking for 16k fails
        # with paInvalidSampleRate) and resample to 16k for Whisper
        if device is None:
            dev = sd.query_devices(kind="input")
        else:
            dev = sd.query_devices(device)
        self.rate = int(dev["default_samplerate"])
        self.block = max(1, int(self.rate * BLOCK_MS / 1000))
        self._q: queue.Queue = queue.Queue()
    def _callback(self, indata, frames, time_info, status):
        self._q.put(indata.copy())

    def record_utterance(self, max_seconds=12, idle_timeout=60):
        """Blocks until an utterance is captured. Returns (text, is_final, duration_s).

        Returns (None, False, 0) on idle timeout so the caller can stay responsive.
        """
        chunks = []
        speaking = False
        silence_ms = 0
        pre_ms = 0
        total_ms = 0
        started = time.time()

        with sd.InputStream(samplerate=self.rate, channels=CHANNELS, dtype="int16",
                            blocksize=self.block, device=self.device, callback=self._callback):
            while True:
                if not speaking and time.time() - started > idle_timeout:
                    return None, False, 0
                try:
                    block = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue
                total_ms += BLOCK_MS
                energy = rms(block)

                if not speaking:
                    if energy > 0.006:  # C920 noise floor is ~0.002; speech is ~0.02+
                        pre_ms += BLOCK_MS
                        chunks.append(block)
                        if pre_ms >= PRE_SPEECH_MS:
                            speaking = True
                    else:
                        chunks = chunks[-8:] if chunks else []  # keep a little pre-roll
                    continue

                chunks.append(block)
                if energy < 0.004:
                    silence_ms += BLOCK_MS
                    if silence_ms >= SILENCE_MS:
                        break
                else:
                    silence_ms = 0
                if total_ms / 1000 > max_seconds:
                    break

        if not chunks or len(chunks) * BLOCK_MS < 250:
            return "", True, 0
        audio = np.concatenate(chunks).flatten().astype(np.float32) / 32768.0
        # resample native rate -> 16k mono for Whisper (simple linear interpolation)
        if self.rate != WHISPER_RATE:
            n = int(len(audio) * WHISPER_RATE / self.rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        duration = len(audio) / WHISPER_RATE
        segments, info = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, True, duration


class Speaker:
    """Piper TTS -> wav -> pw-play. Speaks whole sentences, non-blocking optional."""

    def __init__(self, voice=VOICE):
        from piper import PiperVoice

        self.piper = PiperVoice.load(voice)
        self.sample_rate = self.piper.config.sample_rate
        self._proc = None

    def speak(self, text: str, wait: bool = True):
        text = (text or "").strip()
        if not text:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
            w = wave.open(path, "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            for chunk in self.piper.synthesize(text):
                w.writeframes(chunk.audio_int16_bytes)
            w.close()
        proc = subprocess.Popen(["pw-play", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._proc = proc
        if wait:
            proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None