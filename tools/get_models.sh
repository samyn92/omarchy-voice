#!/bin/bash
# Fetch the recognizers the benchmark compares (tools/asr_bench.py).
# They land in ~/.local/share/omarchy-voice/asr-models/<name>/ and are only needed for
# the comparison — the engine ships with whichever one wins.
set -euo pipefail

DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-voice/asr-models"
BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
mkdir -p "$DIR"

MODELS=(
  "moonshine-v2-tiny:sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27"
  "moonshine-v2-base:sherpa-onnx-moonshine-base-en-quantized-2026-02-27"
  "moonshine-v1-tiny:sherpa-onnx-moonshine-tiny-en-int8"
  "whisper-tiny.en:sherpa-onnx-whisper-tiny.en"
  "whisper-base.en:sherpa-onnx-whisper-base.en"
  "parakeet-110m:sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8"
)

for entry in "${MODELS[@]}"; do
  name="${entry%%:*}"
  archive="${entry#*:}"
  if [[ -d $DIR/$name ]]; then
    echo "  have $name"
    continue
  fi
  echo "  fetching $name …"
  curl -fL --progress-bar "$BASE/$archive.tar.bz2" -o "$DIR/$archive.tar.bz2"
  tar -xjf "$DIR/$archive.tar.bz2" -C "$DIR"
  mv "$DIR/$archive" "$DIR/$name"
  rm -f "$DIR/$archive.tar.bz2"
done

du -sh "$DIR"/* | sort -h
