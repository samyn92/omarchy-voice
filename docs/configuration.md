# Configuration

## Settings

`~/.config/omarchy-voice/config.json`. Every key is optional; the service picks up
changes on restart (`systemctl --user restart omarchy-voice`). The panel and the command
line change the common ones for you.

| Key | Default | |
|---|---|---|
| `yolo_mode` | `false` | skip confirmations (closing windows, power). Risky clicks still ask, see below |
| `confirm_risky_clicks_in_yolo` | `true` | clicks on buttons like Delete, Send or Buy ask even in YOLO mode |
| `jev_only` | `false` | every command is decided by Jev (mode switches stay local) |
| `transcribe_silence_s` | `3.0` | seconds of silence that end a quick `transcribe` (0.5–30; short values end at pauses between sentences) |
| `default_agent` | `""` | herdr agent kind for "new agent" (`claude`, `codex`, `omp` …); empty = the kind you run most |
| `look` | `"window"` | what is read for decisions: the whole window under the mouse, or `"mouse"` for just `look_region` around it |
| `look_region` | `[1100, 700]` | size of that region in logical pixels |
| `hud` | `true` | the line at the bottom of the screen showing what was heard and done |
| `speak` | `true` | spoken questions and errors (actions themselves stay silent) |
| `keep_history` | `true` | keep `~/.local/state/omarchy-voice/history.jsonl` |
| `fast_stt_model` | `"base.en"` | the in-process Whisper model that makes simple commands instant; `""` turns it off |
| `autopilot_stage` | `2` | goals in the browser: `1` look only, `2` may fill in but asks before committing, `3` also acts alone on trusted sites |
| `autopilot_trusted_sites` | `[]` | stage 3 only, e.g. `["github.com"]`. Money, deletions, account changes and signing in always ask, everywhere |
| `autopilot_max_steps` | `14` | a goal gives up after this many steps (or four minutes) |

## API key

`omarchy-voice key` stores your OpenRouter key in `~/.config/omarchy-voice/env`
(readable only by you). `OPENROUTER_API_KEY` in the service environment works too.
Without a key everything that is understood locally keeps working; open-ended commands
show *Jev isn't set up*.

## Environment

| Variable | |
|---|---|
| `OMARCHY_VOICE_TTS` | path to another Piper `.onnx` voice for spoken replies |
| `OMARCHY_VOICE_FALLBACK_MODEL` | Whisper model used when voxtype is not installed (default `small.en`) |
| `JEV_VOICE_MODEL` | Jev model id on OpenRouter (default `~typesafe/jev-latest`) |

## Command line

```
omarchy-voice toggle | start | stop        listening on/off
omarchy-voice status                      current state (add --json before the subcommand for JSON)
omarchy-voice speak "<text>"               run a command as if it was said
omarchy-voice preview "<text>"             what would happen, without doing it
omarchy-voice confirm | cancel             answer a pending confirmation
omarchy-voice hints | grid [--screen] | clear
omarchy-voice dictation                   toggle continuous dictation
omarchy-voice yolo | yolo-on | yolo-off
omarchy-voice jev-only                     toggle Jev-only mode
omarchy-voice transcribe-silence [seconds] quick transcription timeout (no value: cycle 0.5/1/2/3/5)
omarchy-voice goal "<text>"                work towards a goal in the browser
omarchy-voice misses [--hours N] [--replay] [--all]
omarchy-voice key                          store the OpenRouter key
omarchy-voice catalog [--json]             the built-in system actions
omarchy-voice action <id>                  run one of them directly
omarchy-voice daemon                       the service itself
```

## Keybindings

`extras/bindings.lua` has examples for `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + F9", "Voice: toggle listening", "omarchy-voice toggle")
o.bind("SUPER + SHIFT + F9", "Voice: show hints", "omarchy-voice hints")
```

## voxtype bridge

`install.sh` offers two changes to `~/.config/voxtype/config.toml` (with a backup):

- an `[output.post_process]` filter (`extras/voxtype-strip.py`) that removes the spoken
  stop phrase from sessions voice control ended ("… end transcribe"). It only touches the
  end of the text, and only the exact words voice control heard; your own push-to-talk
  dictation passes through unchanged.
- `max_duration_secs = 600`, so long `start transcribe` sessions are not cut at a minute.

`uninstall.sh` takes both out again.

## Files

| Path | |
|---|---|
| `~/.config/omarchy/plugins/io.github.samyn92.omarchy-voice/` | the plugin (widget, engine code, docs) |
| `~/.config/omarchy-voice/` | `config.json`, `env` (API key) |
| `~/.local/share/omarchy-voice/` | Python environment and the Piper voice |
| `~/.local/state/omarchy-voice/history.jsonl` | what was heard and done |
| `$XDG_RUNTIME_DIR/omarchy-voice/` | live state for the widget (`state.json`, `trace.json`), the control socket |
| `~/.config/systemd/user/omarchy-voice.service` | the service |
