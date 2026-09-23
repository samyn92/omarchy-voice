# Development

## Layout

```
manifest.json, BarWidget.qml     the Omarchy plugin (bar widget + panel)
Orb.qml                          the orb: its own layer surface, drawn by the shell on the GPU
install.sh, uninstall.sh         engine setup and removal
bin/omarchy-voice                the command line (runs engine/omarchy_voice.py in the venv)
ear/                             the ear (Rust): microphone, voice activity, fast recognizer
engine/
  omarchy_voice.py               service: control socket, state, trace, history, CLI
  ear.py                         client for the ear's socket
  learn.py                       mishearings mined from the local history
  brain.py                       fast path, sound-alike matching, Jev questions, policy, execution
  fuzzy.py                       spelling + Metaphone similarity
  livemic.py                     Silero VAD segmentation, voxtype / in-process Whisper
  perceive.py                    what is on screen: CDP, AT-SPI (a11y_helper.py), OCR
  desktop.py                     Hyprland socket: windows, workspaces, dispatchers
  uinput.py, keyboard.py         virtual mouse and layout-aware virtual keyboard
  browsers.py, browser.py, executor.py   browser detection, CDP bridge, Playwright actions
  herdr_ctl.py                   herdr sessions, agents, launcher
  apps.py                        installed applications
  autopilot.py                   goals in the browser: the step loop, the stages, the risk judge
  jev.py, spans.py               Jev request/answer helpers, text span candidates
  overlay.py                     hints, grid, click flashes, HUD (GTK layer shell)
  voiceio.py                     Piper voice for spoken replies
extras/                          voxtype filter, systemd unit, keybinding examples
tests/                           phrase test and end-to-end suites
```

`a11y_helper.py` and `overlay.py` run under the system Python (PyGObject); everything
else runs in the engine's venv.

## Running from a checkout

```sh
git clone https://github.com/samyn92/omarchy-voice.git
ln -s "$PWD/omarchy-voice" ~/.config/omarchy/plugins/io.github.samyn92.omarchy-voice
omarchy-voice/install.sh
journalctl --user -u omarchy-voice -f
```

Edits to the engine take effect with `systemctl --user restart omarchy-voice`; edits to
`BarWidget.qml` with `omarchy restart shell`.

## Tests

```sh
uv run python tests/phrases_test.py   # offline: real transcripts → the action they must (not) trigger
uv run python tests/e2e.py            # windows, clicks, typing, voxtype modes, panel
uv run python tests/e2e_herdr.py      # agents: navigation, launcher, prompts
uv run python tests/e2e_browser.py    # search, navigation, keyboard in Chromium
uv run python tests/appearance_test.py  # the browser keeps its own dark mode while we are attached
uv run python tests/autopilot_test.py   # goals: the loop, the stages, the risk judge (offline, scripted Jev)
uv run python tests/orb_test.py         # the orb: appears while listening, sized right, goes away again
uv run python tests/learn_test.py       # what `learn` may and may not propose (offline)
```

The end-to-end suites speak through the real microphone path: a PipeWire null sink
becomes the default source and Piper speech is played into it, so the service and
voxtype hear exactly what a person would say. They check the text that actually arrives
in a test window, restore your microphone, listening state and focus afterwards, and
mark their speech so it stays out of your history. `e2e_herdr.py` builds its own herdr
session with stand-in agents; `e2e_browser.py` starts its own browser with a throwaway
profile. They need a display that is on, and they take over the screen for a minute or two.

## Improving recognition

```sh
omarchy-voice misses --hours 72 --replay
```

lists what was said but not done and decides each again with the current code. Every
real case worth fixing belongs in `tests/phrases_test.py` — with what the small and the
large recognizer actually wrote — so it stays fixed.
