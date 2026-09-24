# Commands

Say things the way you would say them to a person. The phrases below are understood on
your machine instantly — including sound-alike mistakes of the recognizer ("select ride"
is "select right"). Anything else is decided by Jev from what is on screen, so different
wordings work too: "make the browser take the left half", "click the blue button".

Words said to someone else ("thank you", "what do you think") are ignored.

## Listening

| Say | |
|---|---|
| "stop listening", "go to sleep", "voice control off" | stop listening (start again from the bar or `omarchy-voice toggle`) |
| "again", "once more", "more" | repeat the last scroll, key, resize or volume step |
| "confirm", "do it", "yes" / "cancel", "never mind", "no" | answer a confirmation |
| "yolo mode on" / "yolo mode off" | skip or require confirmations |
| "jeff only" | toggle Jev-only mode (every command decided by Jev) |

## Windows and workspaces

| Say | |
|---|---|
| "select left / right / up / down", "focus left" | move focus to the neighbouring window |
| "switch to the terminal", "show me my email" | focus a window by app, title or kind |
| "move window left", "swap it to the right" | swap with the neighbour |
| "expand", "wider", "narrower", "taller", "shorter" (+ "a bit" / "a lot") | resize |
| "half width", "snap left", "snap right", "snap the browser to the top right" | place |
| "fullscreen", "maximize", "float", "tile", "pin", "center", "minimize" | window state |
| "close window" | close (asks to confirm) |
| "workspace three", "go to the second workspace", "next / previous workspace" | switch workspace |
| "move this window to the fourth workspace" | move the focused window |
| "show scratchpad" | toggle the scratchpad |

## Apps

| Say | |
|---|---|
| "open spotify", "launch signal", "open chat gpt" | open an installed app, or focus it if it is running |
| "open two browsers", "open a new terminal" | open fresh windows |
| "open terminal", "open browser", "open files", "open editor" | Omarchy launchers |
| "open apps", "system menu", "clipboard history", "emoji picker" | Omarchy menus |

## Screen: clicking, pointing, scrolling

| Say | |
|---|---|
| "click sign in", "press the play button", "select security" | click something by its label |
| "click", "right click", "double click" | click where the mouse is |
| "move the mouse to the search box", "mouse left a bit" | point without clicking |
| "show numbers" / "show hints", then "seven" or "select seven" | click by number |
| "grid", then "five", "two", … "click" | zoom a 3×3 grid onto any spot, then click |
| "hide", "clear" | remove numbers or the grid |
| "scroll down / up / left / right" (+ "a bit" / "a lot"), "page down", "scroll to the top" | scroll |
| "what's this", "read this", "which song is playing" | read aloud what is under the mouse |

## Keyboard

| Say | |
|---|---|
| "enter", "hit enter", "send", "send it", "submit" | Enter |
| "escape", "tab", "shift tab", "backspace", "delete", "space", "arrow up" … | single keys |
| "press enter three times", "press down twice" | repeat a key |
| "press one" … "press nine" | number keys (answer numbered prompts in agents) |
| "copy", "paste", "cut", "undo", "redo", "select all", "save", "find" | shortcuts (terminal-aware) |
| "new tab", "close tab", "reopen tab", "next tab", "previous tab" | tabs |
| "zoom in", "zoom out", "reset zoom", "refresh", "go back", "go forward" | app and browser keys |
| "type hello world", "type see you tomorrow into the message box" | type text (into a named field) |

## Browser

Works in every browser; a Chromium browser with remote debugging adds precise page reading.

| Say | |
|---|---|
| "search for flights to Zurich", "look up hyprland wiki" | search with the browser's own engine |
| "search youtube / wikipedia / github / amazon / reddit for …", "google …" | search that site |
| "go to github", "open example dot com" | open a site |
| "click the first result", "open the second link" | click on the page |

## Dictation

Two ways into [voxtype](https://github.com/peteonrails/voxtype). While it records, nothing
you say is a command — only the phrases that end the session are listened for.

| Say | |
|---|---|
| "transcribe" … *talk* | quick: ends by itself after 3 s of silence (configurable) |
| "start transcribe" … *talk, pause, talk* … "end transcribe" | long: ends only when you say so |
| "end transcribe send" | end and press Enter (send a message or a prompt) |
| "cancel transcription" | throw the recording away |
| "transcribe, fix the login bug" | start and keep the words said in the same breath |
| "start dictation" … "stop dictation" | the older mode: types after every sentence ("new line", "scratch that") |

"End transcribe" is recognized at the end of what you said; said on its own, the way
Whisper often writes it ("and transcribe") counts as well. Voxtype's own push-to-talk (F9)
keeps working: while you dictate with it, voice control does not act on anything you say.

## herdr agents

With a [herdr](https://herdr.dev) window open:

| Say | |
|---|---|
| "new claude", "new codex", "new oh my pi", "new open code", "new gemini" | start an agent in a new tab of the current workspace |
| "new agent" | start the kind you use most (or `default_agent`) |
| "next agent", "previous agent", "select the second agent", "agent three", "select the last agent" | move between the agents in the sidebar |
| "who needs me" | jump to the agent waiting for you (blocked first, then done, then idle) |
| "agent status" | read out what every agent is doing |
| "go to the agentops agent" | focus an agent by project, task or kind |
| "tell the agentops agent to run the tests", "ask claude why the build fails" | send a prompt without switching |
| "interrupt", "stop the agent" | Escape in the focused agent |
| "next tab", "pane left", "zoom", "next project" | herdr tabs, panes and workspaces (when herdr is focused) |
| "open herdr" | open herdr, or focus it |

Agents waiting for an approval are never sent a prompt, and talking *about* an agent
("the alpha agent is slow today") does nothing — prompts start with "tell" or "ask".

## Goals — in apps and on the web

| Say | |
|---|---|
| "in hermes, go to settings and find where to enable dark mode" | a goal inside that app |
| "in this app, find the archived chats" | a goal inside the focused app |
| "go to settings and find the version" · "open appearance" | the same, when the focused app can be operated |
| "figure out when the market opens", "find out who wrote Dune" | a goal on the web, then the answer read out |
| "research the best train to Berlin", "look into …" | the same |
| "autopilot book a table for two on Friday" | a goal that fills something in |
| "give voice access to obsidian" | start that app so goals can operate it (restarts it if open, after you confirm) |
| "stop" | ends the goal at once |
| "confirm" | allows the one step it stopped at |

In an app, a goal first looks for a **skill** (it was done before: replayed, no model), then
asks the **app map** ("go to appearance" is a known path), and only then lets **Jev** work it
out — which is then kept as a skill. On the web only Jev runs: every visit is different.

It searches, follows links, scrolls and reads on its own. Filling in fields and pressing
buttons is allowed, but it stops and asks before anything that sends, posts, buys, books or
changes a setting; finding where a setting is never switches it. It never types into a
password or card field, and password managers never get voice access. See
[configuration](configuration.md) for the three stages and [privacy](privacy.md) for what a
goal sends.

## System

| Say | |
|---|---|
| "volume up", "make it louder", "mute", "mute microphone", "switch audio output" | audio |
| "play", "pause", "next song", "previous track" | media |
| "brighter", "dim the screen", "brightness 40", "night light" | display |
| "toggle bar", "do not disturb", "stay awake" | session toggles |
| "lock the screen", "log out", "restart computer", "shut down" | power (asks to confirm) |
