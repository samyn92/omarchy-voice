"""Which recognizer should the ear use? Decided on our own commands, not on a leaderboard.

Word error rate is the wrong question: what matters is whether a transcript makes the brain
do the right thing.  "scroll down" and "Scroll down." score differently as text and
identically as a command, and "select ride" is wrong as text but right as a command, because
the sound-alike matching catches it.  So every model is scored by what the brain decides.

    uv run python tools/asr_bench.py build     # speak the corpus into WAVs (Piper)
    uv run python tools/asr_bench.py run       # every model over every WAV, scored
    uv run python tools/asr_bench.py run --models moonshine-tiny,whisper-base.en

Add real recordings by dropping 16 kHz mono WAVs into corpus/real/<expected action>.wav —
TTS speech is clean and even, human speech is not, and only the second kind proves anything.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "engine"))
import argparse
import json
import statistics
import time
import wave

ROOT = _Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tools" / "corpus"
MODELS_DIR = _Path.home() / ".local/share/omarchy-voice/asr-models"

# (what is said, the action label it must produce, or None for "must not be acted on")
PHRASES: list[tuple[str, str | None]] = [
    ("scroll down", "Scroll down"),
    ("scroll down a bit", "Scroll down"),
    ("page down", "Page down"),
    ("select left", "Focus left"),
    ("select right", "Focus right"),
    ("select above", "Focus up"),
    ("workspace three", "Workspace 3"),
    ("go to the second workspace", "Workspace 2"),
    ("move this window to the fourth workspace", "Move to workspace 4"),
    ("fullscreen", "Toggle full screen"),
    ("expand", "Wider"),
    ("expand window", "Wider"),
    ("half width", "Half width"),
    ("close window", "Close window"),
    ("open spotify", "Open Spotify"),
    ("open two terminals", "×2"),
    ("open chat gpt", "Open ChatGPT"),
    ("volume up", "Volume up"),
    ("mute", "Mute"),
    ("play", "Play"),
    ("next song", "Next track"),
    ("brighter", "Brightness up"),
    ("copy", "Copy"),
    ("paste", "Paste"),
    ("undo", "Undo"),
    ("select all", "Select all"),
    ("hit enter", "Enter"),
    ("escape", "Escape"),
    ("press down twice", "Down"),
    ("press one", "1"),
    ("new tab", "New tab"),
    ("show numbers", "Show hints"),
    ("clear", "Hide"),
    ("again", None),                      # only valid after something repeatable
    ("search for flights to Zurich", "Search “flights to Zurich”"),
    ("search youtube for stone techno", "Search youtube"),
    ("go to example dot com", "Open https://example.com"),
    ("new claude", "New claude agent"),
    ("next agent", "Select next agent"),
    ("select the second agent", "Select the second agent"),
    ("who needs me", "Who needs me"),
    ("transcribe", "Transcribe (quick)"),
    ("start transcribe", "Transcribe (long)"),
    ("what is under the mouse", "Read screen"),
    ("figure out when the market opens", "Goal:"),
    ("stop listening", "STOP"),
    # short ones: where small models are known to struggle
    ("yes", None),
    ("okay", None),
    ("stop", None),
    ("done", None),
]


def say(text: str, path: _Path) -> None:
    """Piper speaks a phrase into a 16 kHz mono WAV — the same voice the e2e tests use."""
    import numpy as np
    from piper import PiperVoice
    import voiceio

    global _VOICE
    try:
        _VOICE
    except NameError:
        _VOICE = PiperVoice.load(voiceio.VOICE)
    chunks = [np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in _VOICE.synthesize(text)]
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    rate = _VOICE.config.sample_rate
    if rate != 16000:                     # every recognizer here wants 16 kHz
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)),
                          audio.astype(np.float32)).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(audio.tobytes())


def build() -> int:
    for i, (text, _) in enumerate(PHRASES):
        path = CORPUS / "tts" / f"{i:02d}.wav"
        if not path.exists():
            say(text, path)
            print(f"  spoke {text!r}")
    (CORPUS / "tts" / "index.json").write_text(
        json.dumps([{"file": f"{i:02d}.wav", "said": t, "expect": e} for i, (t, e) in enumerate(PHRASES)], indent=1))
    print(f"\n{len(PHRASES)} phrases in {CORPUS / 'tts'}")
    return 0


def record() -> int:
    """Record the same phrases in a real voice — synthetic speech hides what matters.

    Piper says "undo" so that every recognizer here hears "and you"; a person does not.
    Decisions get made on these recordings, not on the TTS ones.
    """
    import numpy as np
    import sounddevice as sd

    out = CORPUS / "real"
    out.mkdir(parents=True, exist_ok=True)
    print("Speak each phrase after the prompt, normally, from where you usually sit.")
    print("Enter to record (about 2.5 s), s to skip, q to stop.\n")
    for i, (text, _) in enumerate(PHRASES):
        path = out / f"{i:02d}.wav"
        if path.exists():
            continue
        answer = input(f"  [{i + 1}/{len(PHRASES)}]  “{text}”  ")
        if answer.strip().lower() == "q":
            break
        if answer.strip().lower() == "s":
            continue
        audio = sd.rec(int(2.5 * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
        audio = np.asarray(audio).flatten()
        loud = np.abs(audio) > 500                      # trim the silence around it
        if loud.any():
            first, last = np.argmax(loud), len(loud) - np.argmax(loud[::-1])
            audio = audio[max(0, first - 1600):min(len(audio), last + 1600)]
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(audio.tobytes())
    done = len(list(out.glob("*.wav")))
    (out / "index.json").write_text(json.dumps(
        [{"file": f"{i:02d}.wav", "said": t, "expect": e} for i, (t, e) in enumerate(PHRASES)
         if (out / f"{i:02d}.wav").exists()], indent=1))
    print(f"\n{done} recordings in {out}\n  score them with:  uv run python tools/asr_bench.py run --voice real")
    return 0


def load_wav(path: _Path):
    import numpy as np
    with wave.open(str(path), "rb") as f:
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    return audio.astype(np.float32) / 32768.0


# -- the recognizers ---------------------------------------------------------------------
def faster_whisper(name: str):
    """What the engine uses today: CTranslate2 Whisper, in this process."""
    from faster_whisper import WhisperModel

    model = WhisperModel(name.split(":", 1)[1], device="cpu", compute_type="int8")

    def run(audio):
        segments, _ = model.transcribe(audio, language="en", beam_size=1)
        return " ".join(s.text for s in segments).strip()

    return run


def sherpa(directory: _Path, threads: int = 2):
    """sherpa-onnx: the runtime the Rust ear would use, with whichever model is in `directory`.

    One runtime, four model families, the same three lines of setup — which is the reason to
    build the ear on it: changing recognizer later is a config line, not a rewrite.
    """
    import sherpa_onnx

    tokens = str(next(directory.glob("*tokens.txt")))
    if (directory / "encoder_model.ort").exists():                       # Moonshine v2
        recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=str(directory / "encoder_model.ort"),
            decoder=str(directory / "decoder_model_merged.ort"),
            tokens=tokens, num_threads=threads)
    elif (directory / "preprocess.onnx").exists():                       # Moonshine v1
        recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(directory / "preprocess.onnx"),
            encoder=str(directory / "encode.int8.onnx"),
            uncached_decoder=str(directory / "uncached_decode.int8.onnx"),
            cached_decoder=str(directory / "cached_decode.int8.onnx"),
            tokens=tokens, num_threads=threads)
    elif list(directory.glob("*encoder*.onnx")):                         # Whisper
        recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(next(directory.glob("*encoder.int8.onnx"))),
            decoder=str(next(directory.glob("*decoder.int8.onnx"))),
            tokens=tokens, language="en", task="transcribe", num_threads=threads)
    elif (directory / "model.int8.onnx").exists():                       # NeMo Parakeet (CTC)
        recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(directory / "model.int8.onnx"), tokens=tokens, num_threads=threads)
    else:
        raise SystemExit(f"no recognizable model files in {directory}")

    def run(audio):
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()

    return run


def recognizers(wanted: list[str]) -> dict:
    out = {}
    for name in wanted:
        if name.startswith("fw:"):
            out[name] = faster_whisper(name)
        else:
            directory = MODELS_DIR / name
            if not directory.is_dir():
                print(f"  ({name}: not downloaded, skipped — see tools/get_models.sh)")
                continue
            out[name] = sherpa(directory)
    return out


# -- scoring -----------------------------------------------------------------------------
def brain_action(brain, sv, transcript: str) -> str | None:
    """What the engine would do with this transcript (local decisions only)."""
    spoken = sv.normalize(transcript)
    decision = brain.fast_path(spoken, transcript)
    if decision is None or decision.action is None:
        decision = brain.fuzzy_command([spoken], threshold=84) or decision
    if decision is None or decision.action is None:
        return None
    return decision.action.label


def run(models: list[str], voice: str = "tts") -> int:
    import omarchy_voice as sv
    from brain import Brain

    source = CORPUS / voice
    if not (source / "index.json").exists():
        raise SystemExit(f"no corpus in {source} — run `build` (TTS) or `record` (your voice) first")
    index = json.loads((source / "index.json").read_text())
    brain = Brain(sv.ACTIONS, sv.exact_match, sv.normalize, {"hud": False, "speak": False, "jev_only": False})
    brain.fuzzy_element = lambda h: None

    # The perfect transcript sets the target: what the brain does with the words as written is
    # the best any recognizer could achieve. A phrase the brain does not know is a gap in the
    # brain, not in the model, and is left out of the comparison instead of punishing everyone.
    outside = []
    for row in index:
        row["target"] = brain_action(brain, sv, row["said"])
        if row["expect"] is not None and row["target"] is None:
            outside.append(row["said"])
    scored = [r for r in index if not (r["expect"] is not None and r["target"] is None)]
    if outside:
        print(f"outside the brain's vocabulary, not scored: {', '.join(repr(o) for o in outside)}\n")

    engines = recognizers(models)
    if not engines:
        print("No recognizers available.")
        return 1

    results = {}
    for name, engine in engines.items():
        correct, wrong, times, mistakes = 0, 0, [], []
        for row in scored:
            audio = load_wav(source / row["file"])
            started = time.perf_counter()
            text = engine(audio)
            times.append((time.perf_counter() - started) * 1000)
            action = brain_action(brain, sv, text)
            target = row["target"]                    # what the words as written would do
            ok = action == target
            correct += ok
            wrong += not ok
            if not ok:
                mistakes.append(f"{row['said']!r} heard as {text!r} -> {action} (wanted {target})")
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        results[name] = {"correct": correct, "of": len(scored), "p50": statistics.median(times),
                         "p90": sorted(times)[int(len(times) * 0.9)], "peak_rss_mb": round(rss),
                         "mistakes": mistakes}
        print(f"\n{name}: {correct}/{len(scored)} commands right · {results[name]['p50']:.0f} ms median "
              f"· {results[name]['p90']:.0f} ms p90")
        for m in mistakes[:6]:
            print(f"    miss  {m}")

    print("\n" + "=" * 78)
    print(f"{'model':28} {'commands right':>16} {'median':>9} {'p90':>9} {'disk':>8}")
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["correct"]):
        directory = MODELS_DIR / name
        size = sum(f.stat().st_size for f in directory.rglob("*.onnx")) / 1e6 if directory.is_dir() else 0
        size += sum(f.stat().st_size for f in directory.rglob("*.ort")) / 1e6 if directory.is_dir() else 0
        print(f"{name:28} {r['correct']:>7}/{r['of']:<8} {r['p50']:>7.0f}ms {r['p90']:>7.0f}ms {size:>6.0f}MB")
    (ROOT / "tools" / "bench-results.json").write_text(json.dumps(results, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "record", "run"])
    parser.add_argument("--voice", default="tts", choices=["tts", "real"], help="which corpus to score")
    parser.add_argument("--models", default="fw:base.en,moonshine-v2-tiny,moonshine-v2-base,"
                                              "moonshine-v1-tiny,whisper-tiny.en,whisper-base.en,parakeet-110m")
    args = parser.parse_args()
    if args.command == "build":
        return build()
    if args.command == "record":
        return record()
    return run([m.strip() for m in args.models.split(",") if m.strip()], args.voice)


if __name__ == "__main__":
    raise SystemExit(main())
