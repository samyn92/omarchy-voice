# Omarchy Voice

Control Omarchy by talking to it. Open apps, arrange windows, switch workspaces, click
things on screen, browse, dictate text and drive your coding agents in herdr — without
touching the keyboard or the mouse.

Speech is recognized on your machine by Whisper. Common commands ("scroll down",
"select left", "new claude") are understood locally in a fraction of a second.
Everything open-ended ("snap the browser to the right half", "click the sign in button",
"tell the agentops agent to run the tests") is decided by [Jev](https://openrouter.ai)
from what is on your screen. Jev only ever *picks* from the windows, buttons and
actions that exist — it never writes commands.

A microphone in the bar shows whether it is listening. Its panel is a live trace: what
was heard, how it was understood, what was done and how long it took.

## Install

```sh
omarchy plugin add https://github.com/samyn92/omarchy-voice.git --enable
~/.config/omarchy/plugins/io.github.samyn92.omarchy-voice/install.sh
```

The first command adds the bar widget. The second sets up the voice engine: the speech
models, a user service, the `omarchy-voice` command and your OpenRouter key for Jev. It
only asks about what is missing and is safe to run again. Until it has run, the panel
offers a **Set up Omarchy Voice** button that does the same.

Then right-click the microphone in the bar to start listening, or bind a key in
`~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + F9", "Voice: toggle listening", "omarchy-voice toggle")
```

## Usage

Just say what you want. A few examples:

| Say | What happens |
|---|---|
| "open spotify", "open two terminals" | launches apps (or focuses them when they are already open) |
| "select left", "go to the second workspace" | moves focus between windows and workspaces |
| "expand", "snap the browser to the right half", "fullscreen" | arranges windows |
| "scroll down a bit", "page down", "again" | scrolls wherever the mouse is; "again" repeats |
| "click sign in", "press the play button" | clicks what it can read on screen |
| "show numbers" … "seven" | numbers every clickable thing; say one to click it |
| "search for flights to Zurich", "search youtube for techno" | searches with your browser's own engine |
| "copy", "paste", "undo", "hit enter", "press two" | keys and shortcuts in any app |
| "transcribe" … *talk* … *pause* | dictates into the focused field, ends after 3 s of silence |
| "start transcribe" … "end transcribe" | long dictation; pauses are fine |
| "new claude", "next agent", "who needs me" | starts and moves between herdr agents |
| "tell the agentops agent to run the tests" | sends a prompt to an agent without switching to it |
| "figure out when the Christmas market opens" | works towards a goal in the browser, step by step |

The full list is in [docs/commands.md](docs/commands.md).

In the panel:

- **Live card** — the pipeline (Hear → Transcribe → Decide → Act) and the last command.
  While voxtype records it shows how the session ends.
- **Stats** — median and p90 latency, the share decided on your machine, Jev calls and cost today.
- **Transcribe** starts a long dictation, **Hints** numbers the screen, **YOLO** skips
  confirmations, **Jev only** sends every command to Jev (for comparing).
- **Trace** — one card per utterance: route (LOCAL, FUZZY, JEV, IGNORED…), what both
  recognizers heard, the action, timings and cost.

The command line does the same and more:

```sh
omarchy-voice toggle              # start or stop listening
omarchy-voice speak "open spotify"  # run a command as if it was said
omarchy-voice preview "click sign in"   # what would happen, without doing it
omarchy-voice misses --replay     # what you said that was not done, re-decided with today's code
omarchy-voice key                 # store your OpenRouter key
omarchy-voice --help
```

## Goals in the browser

Instead of one command you can give a goal, and it works towards it: look at the page, take
the next step, look again. Say "figure out …", "find out …", "research …" or "autopilot …".

```
"figure out when the Bremen Christmas market opens"
  1. Search the web for "when the Bremen Christmas market opens"
  2. Done — "The Christmas market in Bremen will be open from 23 November to 23 December"
```

Every step is chosen from what is actually on the page, the panel shows each one as it
happens, and saying "stop" ends it at once. Answers are quoted from the page, never
invented: if it cannot find one it says so rather than guess.

Before anything that commits — sending, posting, buying, booking, changing an account — two
independent checks run: a word list in code, and a second Jev call that sees the page and
judges what the step would do. Either one is enough to stop and ask you, and anything that
fails counts as "ask". Fields that hold a password or a card number are never typed into,
and never reach Jev at all. By default it may fill things in but stops at the commit and
asks you; `autopilot_stage` in the config changes that — see
[docs/configuration.md](docs/configuration.md).

## Requirements

- Omarchy 4 on Hyprland
- A microphone
- An [OpenRouter](https://openrouter.ai/settings/keys) key for Jev (about $0.0004 per open-ended
  command; common commands never leave your machine and work without a key)
- Optional: [voxtype](https://github.com/peteonrails/voxtype) for the most accurate recognition and
  for `transcribe` (`omarchy voxtype install`), [herdr](https://herdr.dev) for agent control, and a
  Chromium-family browser started with `--remote-debugging-port=9222` for reading and clicking web
  pages precisely (search and navigation work in every browser without it)

`install.sh` installs the system packages it needs (grim, tesseract, wtype, GTK layer shell)
with `omarchy pkg add` and asks before anything that needs sudo.

## Privacy

Audio never leaves your machine. For commands that go to Jev, the text you said, the
titles of your open windows and the text visible in the window under the mouse are sent
to OpenRouter. A history of what was heard is kept locally in
`~/.local/state/omarchy-voice/`. Details and how to turn things off:
[docs/privacy.md](docs/privacy.md).

## More

- [Commands](docs/commands.md) — everything you can say
- [Configuration](docs/configuration.md) — settings, the command line, keybindings
- [How it works](docs/how-it-works.md) — recognition, decisions, perception, safety
- [Development](docs/development.md) — layout, tests, contributing

## Uninstall

```sh
~/.config/omarchy/plugins/io.github.samyn92.omarchy-voice/uninstall.sh
omarchy plugin remove io.github.samyn92.omarchy-voice
```

Settings and history stay unless you pass `--purge` to `uninstall.sh`.

## License

MIT. The browser decision prompts started from Moritz Kremb's
[jev-voice-browser](https://github.com/moritzkremb/jev-voice-browser) (MIT).
