#!/bin/bash

# Remove the Omarchy Voice engine. Your settings and history are kept unless you pass --purge.
# Remove the bar widget itself with: omarchy plugin remove io.github.samyn92.omarchy-voice

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-voice"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-voice"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omarchy-voice.service"
VOXTYPE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype/config.toml"

systemctl --user disable --now omarchy-voice.service 2>/dev/null || true
rm -f "$UNIT" "$HOME/.local/bin/omarchy-voice"
systemctl --user daemon-reload
rm -rf "$DATA_DIR"
echo "Removed the service, the command and the engine ($DATA_DIR)."

if [[ -f $VOXTYPE_CONFIG ]] && grep -q "omarchy-voice" "$VOXTYPE_CONFIG"; then
  cp "$VOXTYPE_CONFIG" "$VOXTYPE_CONFIG.bak.$(date +%s)"
  # drop our post_process block (comment, header, command, timeout) and restore the 60 s cap
  sed -i '/^# omarchy-voice: drop the spoken stop phrase/,/^timeout_ms = /d' "$VOXTYPE_CONFIG"
  sed -i 's/^max_duration_secs = 600  # omarchy-voice.*/max_duration_secs = 60/' "$VOXTYPE_CONFIG"
  systemctl --user restart voxtype.service 2>/dev/null || true
  echo "Removed the voxtype bridge from $VOXTYPE_CONFIG (backup next to it)."
fi

if [[ ${1:-} == --purge ]]; then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  echo "Removed settings, API key and history."
else
  echo "Kept settings and API key ($CONFIG_DIR) and history ($STATE_DIR) — pass --purge to remove them."
fi

[[ -f /etc/udev/rules.d/60-omarchy-voice-uinput.rules ]] &&
  echo "The uinput udev rule stays (/etc/udev/rules.d/60-omarchy-voice-uinput.rules); remove it with sudo if you like."
echo "Plugin files are still in $PLUGIN_DIR."
