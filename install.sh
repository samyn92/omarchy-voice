#!/bin/bash

# Set up Omarchy Voice: the voice engine behind the bar widget.
#
# Safe to run again — it updates what is there and only asks about what is missing.
# Pass --yes to accept every default without asking.

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-voice"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VOXTYPE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype/config.toml"
VENV="$DATA_DIR/venv"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"

YES=0
[[ ${1:-} == --yes || ${1:-} == -y ]] && YES=1

step() { printf '\n\e[1m%s\e[0m\n' "$*"; }
ok() { printf '  \e[32m✓\e[0m %s\n' "$*"; }
note() { printf '  · %s\n' "$*"; }

ask() {
  (( YES )) && return 0
  if command -v gum >/dev/null; then
    gum confirm "$1"
  else
    local answer
    read -r -p "$1 [Y/n] " answer
    [[ -z $answer || $answer =~ ^[Yy] ]]
  fi
}

step "System packages"
missing=()
for pair in uv:uv grim:grim tesseract:tesseract wtype:wtype wl-copy:wl-clipboard jq:jq \
  xdg-terminal-exec:xdg-terminal-exec notify-send:libnotify pw-play:pipewire; do
  command -v "${pair%%:*}" >/dev/null || missing+=("${pair#*:}")
done
[[ -f /usr/share/tessdata/eng.traineddata ]] || missing+=(tesseract-data-eng)
python3 -c 'import gi; gi.require_version("Atspi", "2.0"); gi.require_version("Gtk", "4.0"); gi.require_version("Gtk4LayerShell", "1.0")' 2>/dev/null ||
  missing+=(python-gobject gtk4 gtk4-layer-shell at-spi2-core)
if (( ${#missing[@]} )); then
  note "missing: ${missing[*]}"
  if ask "Install them with omarchy pkg add?"; then
    omarchy pkg add "${missing[@]}"
  else
    echo "Omarchy Voice needs these packages. Install them and run this again." >&2
    exit 1
  fi
fi
ok "packages present"

step "Virtual keyboard and mouse"
if [[ -w /dev/uinput ]]; then
  ok "/dev/uinput is writable"
else
  note "Voice control types and clicks through /dev/uinput, exactly like a real keyboard and mouse."
  note "Your user needs access to it: a udev rule for the input group, and membership in that group."
  if ask "Set that up now (uses sudo; log out and in afterwards)?"; then
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' |
      sudo tee /etc/udev/rules.d/60-omarchy-voice-uinput.rules >/dev/null
    sudo udevadm control --reload
    sudo udevadm trigger --name-match=uinput || true
    id -nG | grep -qw input || sudo usermod -aG input "$USER"
    note "done — log out and back in once so the input group applies"
  else
    note "skipped — clicking and typing fall back to wtype, the mouse will not work"
  fi
fi

step "Dictation (voxtype)"
if command -v voxtype >/dev/null; then
  ok "voxtype installed"
elif ask "Install voxtype? It gives the most accurate recognition and powers \"transcribe\"."; then
  omarchy voxtype install
else
  note "skipped — recognition uses an in-process Whisper model, \"transcribe\" is unavailable"
fi

STRIP_CMD="/usr/bin/python3 $PLUGIN_DIR/extras/voxtype-strip.py"
if [[ -f $VOXTYPE_CONFIG ]] && grep -qE '^command = ".*voxtype[-_]strip\.py"' "$VOXTYPE_CONFIG" \
    && ! grep -qF "command = \"$STRIP_CMD\"" "$VOXTYPE_CONFIG"; then
  # our filter from an earlier or moved install: point it at this copy
  cp "$VOXTYPE_CONFIG" "$VOXTYPE_CONFIG.bak.$(date +%s)"
  sed -i -E "s|^command = \".*voxtype[-_]strip\.py\"|command = \"$STRIP_CMD\"  # omarchy-voice|" "$VOXTYPE_CONFIG"
  systemctl --user restart voxtype.service 2>/dev/null || true
  ok "voxtype bridge updated (backup next to the config)"
elif [[ -f $VOXTYPE_CONFIG ]] && ! grep -q "omarchy-voice" "$VOXTYPE_CONFIG"; then
  note "When you end a session by voice (\"end transcribe\"), those words are in voxtype's recording too."
  if ask "Let voxtype drop them (adds a post-process filter and allows 10-minute sessions)?"; then
    cp "$VOXTYPE_CONFIG" "$VOXTYPE_CONFIG.bak.$(date +%s)"
    if grep -q '^\[output.post_process\]' "$VOXTYPE_CONFIG"; then
      note "voxtype already has a post_process command — left untouched"
    else
      printf '\n# omarchy-voice: drop the spoken stop phrase ("end transcribe") from voice-started sessions\n[output.post_process]\ncommand = "%s"\ntimeout_ms = 2000\n' \
        "$STRIP_CMD" >>"$VOXTYPE_CONFIG"
    fi
    sed -i 's/^max_duration_secs = 60\b.*/max_duration_secs = 600  # omarchy-voice: long "start transcribe" sessions/' "$VOXTYPE_CONFIG"
    systemctl --user restart voxtype.service 2>/dev/null || true
    ok "voxtype bridge configured (backup next to the config)"
  fi
elif [[ -f $VOXTYPE_CONFIG ]]; then
  ok "voxtype bridge configured"
fi

step "Voice engine"
mkdir -p "$DATA_DIR/voices" "$CONFIG_DIR"
UV_PROJECT_ENVIRONMENT="$VENV" uv sync --project "$PLUGIN_DIR" --frozen --quiet
ok "python environment in $VENV"
for file in en_US-amy-medium.onnx en_US-amy-medium.onnx.json; do
  [[ -s $DATA_DIR/voices/$file ]] || curl -fsSL -o "$DATA_DIR/voices/$file" "$VOICE_URL/$file"
done
ok "spoken replies: Piper voice \"amy\""
"$VENV/bin/python" -c 'from faster_whisper import WhisperModel; WhisperModel("base.en", device="cpu", compute_type="int8")' >/dev/null 2>&1
ok "fast recognizer: Whisper base.en"

step "Fast recognizer (the ear)"
EAR_BIN="$DATA_DIR/bin/ear"
MODEL_DIR="$DATA_DIR/asr-models/parakeet-tdt-110m"
if [[ ! -d $MODEL_DIR ]] && ask "Download the command recognizer (136 MB, Parakeet)?"; then
  mkdir -p "$DATA_DIR/asr-models"
  archive="sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8"
  curl -fL --progress-bar \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$archive.tar.bz2" \
    -o "$DATA_DIR/asr-models/$archive.tar.bz2"
  tar -xjf "$DATA_DIR/asr-models/$archive.tar.bz2" -C "$DATA_DIR/asr-models"
  mv "$DATA_DIR/asr-models/$archive" "$MODEL_DIR"
  rm -f "$DATA_DIR/asr-models/$archive.tar.bz2"
fi
if [[ -d $MODEL_DIR ]]; then
  if [[ -x $PLUGIN_DIR/ear/target/release/ear ]] || command -v cargo >/dev/null; then
    if [[ ! -x $PLUGIN_DIR/ear/target/release/ear ]]; then
      note "building the ear (a few minutes the first time)"
      (cd "$PLUGIN_DIR/ear" && cargo build --release --quiet)
    fi
    mkdir -p "$DATA_DIR/bin"
    install -m755 "$PLUGIN_DIR/ear/target/release/ear" "$EAR_BIN"
    install -Dm644 "$PLUGIN_DIR/extras/omarchy-voice-ear.service" "$UNIT_DIR/omarchy-voice-ear.service"
    systemctl --user daemon-reload
    systemctl --user enable --now omarchy-voice-ear.service >/dev/null 2>&1 || true
    ok "the ear is running (~25 ms per command, and the engine keeps a gigabyte less in memory)"
  else
    note "no rust toolchain: the engine will recognize speech itself (slower, heavier)"
    note "  install rust and run this again for the fast path: omarchy pkg add rust"
  fi
else
  note "skipped — the engine will recognize speech itself"
fi

step "Service"
mkdir -p "$HOME/.local/bin" "$UNIT_DIR"
ln -sf "$PLUGIN_DIR/bin/omarchy-voice" "$HOME/.local/bin/omarchy-voice"
cp "$PLUGIN_DIR/extras/omarchy-voice.service" "$UNIT_DIR/omarchy-voice.service"
systemctl --user daemon-reload
systemctl --user enable omarchy-voice.service >/dev/null 2>&1
systemctl --user restart omarchy-voice.service
ok "omarchy-voice.service running (command: omarchy-voice)"

step "Jev (decisions for open-ended commands)"
if [[ -n ${OPENROUTER_API_KEY:-} ]] || grep -qs '^OPENROUTER_API_KEY=.' "$CONFIG_DIR/env"; then
  ok "OpenRouter key configured"
elif (( YES )); then
  note "no key — run 'omarchy-voice key' later; common commands work without it"
else
  note "Common commands are understood on this machine. Everything else is decided by Jev on OpenRouter"
  note "(about \$0.0004 per decision). Create a key at https://openrouter.ai/settings/keys"
  omarchy-voice key || note "skipped — run 'omarchy-voice key' later"
fi

step "Done"
note "Right-click the microphone in the bar to start listening — or bind a key:"
note "  $(tail -n 2 "$PLUGIN_DIR/extras/bindings.lua" | head -n 1)"
note "Say \"scroll down\", \"open spotify\", \"new claude\", \"transcribe\" … see $PLUGIN_DIR/docs/commands.md"
