# How it works

```
mic ─► Silero VAD ─┬─► Whisper base.en (in process, ~0.2 s) ──► exact / sound-alike command? ─► act now
                   │                                                  │ no
                   └─► voxtype large-v3-turbo (~0.6 s) ──────────────►├─► Jev decides (~0.4 s, started early)
                                                                      │
          screen: DOM (CDP) · accessibility (AT-SPI) · OCR  ─────────►┘
                                                                      ▼
                         Hyprland dispatch · uinput mouse + keyboard · browser · herdr API · voxtype
```

## Hearing

The microphone belongs to **the ear** (`ear/`, Rust): it captures, detects speech with
Earshot, recognizes with Parakeet through sherpa-onnx, and publishes lines on a socket —
levels for the orb, and each utterance with its text and a path to its audio. A command is
transcribed in about 25 ms. The engine subscribes, so it holds no audio models at all; when
the ear is not installed it falls back to capturing and recognizing for itself.

Which recognizer the ear uses was decided by measurement, not reputation: fifty phrases
recorded in a real voice, scored by *the action the brain takes*, in `tools/asr_bench.py`.
Moonshine wins on synthetic speech and collapses on a person; Parakeet 110M beat the Whisper
we shipped on both accuracy and speed.


The microphone stays open; Silero cuts speech into utterances while earlier ones are
still being handled, so nothing said in the meantime is lost. Each utterance goes to two
recognizers at once: a small Whisper inside the service, and voxtype's large one. Simple
commands act on the small one after about 0.2 s. For everything else Jev starts deciding
on the small transcript while the large one finishes; when both agree the decision is
reused, so the large model costs no extra time.

## Understanding

1. **Fast path** — exact phrases, in code. Most everyday commands end here.
2. **Sound-alike matching** — both transcripts are compared with every known phrase by
   spelling and by Metaphone code, and "click / select X" with the text on screen (whole
   words only). A clear winner is used; anything close to a tie is not.
3. **Jev** — one request with typed questions (what to do, which window, which element,
   which key, which app …). Jev answers with choices from lists the code built; the code
   turns the answers into an action. Model output is never executed as text.

If the two recognizers heard completely different things, a Jev decision is dropped
rather than guessed.

## Seeing

When you start speaking, the window under the mouse is read:

- **Browser pages** over CDP (Chromium with remote debugging): real DOM elements.
- **Native apps** through AT-SPI accessibility: buttons, fields, list items.
- **Everything else** (terminals, Electron apps, canvases) with OCR: twelve Tesseract
  engines read horizontal strips in parallel (~0.4 s for a window).

Every element becomes a numbered line with its role, text and position, so "click the
second result" or "the one under the mouse" can be resolved.

## Acting

- **Windows and workspaces** — Hyprland's Lua dispatchers over its socket (~0.1 ms).
- **Mouse and keyboard** — two uinput devices. Keys are looked up in your keyboard layout
  with libxkbcommon, so text types correctly on German, French or US layouts; characters
  a layout cannot type are pasted. Every application accepts them like real input.
- **Browsers** — the address bar for searching and navigating (your own search engine,
  any browser); CDP for clicking page elements.
- **herdr** — its socket API: focus agents, tabs and panes, start agents, send prompts.
- **Dictation** — `voxtype record start/stop/cancel`.

Before anything is typed the bar panel closes, because an open panel holds the keyboard.

## The orb

The engine never draws the orb. It streams one `{state, level}` line per 25 ms to
`$XDG_RUNTIME_DIR/omarchy-voice/orb.sock`, and the bar plugin's `Orb.qml` draws it on its
own layer surface through Qt's scene graph — GPU-composited and locked to the monitor's
refresh, like the rest of the Omarchy shell. A client that cannot keep up is skipped rather
than waited for, so drawing can never stall the microphone thread.

## Working towards a goal

A goal runs where you are: in the named app ("in hermes …"), in the focused app when it can
be operated, and otherwise on the web.

**The standard surface.** Electron apps are Chromium inside. Started with a local debug port
they answer the same protocol as the browser, and expose their interface as labelled
elements — Hermes shows about a hundred: "Open settings", "Appearance", "About". Omarchy's
launcher starts the apps you gave voice access with that port; everything else about the
goal is the same in an app as on a page. Password managers never get a port.

**Cheapest first.** Each goal is worked the cheapest way known to work:

1. **A skill.** The goal was done here before. Its clicks are replayed by label — ids change
   from screen to screen, labels do not — with no model at all. If the app changed and a
   label is gone, the replay stops there and Jev carries on from that screen.
2. **The app map.** Every click records the screen before and after it. From that graph,
   "go to appearance" is a path: Open settings → Appearance. Jev also sees, next to each
   element, what clicking it is known to reveal.
3. **Jev.** Read the screen, choose the single next step among the elements that exist,
   judge it, do it, read again. What just appeared comes first — an app repeats its sidebar
   on every screen, and what a click opened would otherwise land past what a request can
   carry.

A goal Jev finishes cleanly — nothing asked, nothing refused — becomes a skill; what was
typed from what you said becomes a slot ("search my notes for {text}"). A few skills ship
with the plugin. Maps ship only when made in a disposable instance: on a live, connected app
no label heuristic makes exploring safe — on Hermes the "navigational" buttons included
"Restore checkpoint" and suggested prompts that start agent tasks — so on your machine maps
are learned from goals, whose every click already passed the safety gates.

It stops at an answer, at a dead end, when the screen stops changing, after 14 steps or four
minutes, or the moment you say stop. Answers are quoted from the screen — a line of text, or
the control that was being looked for — never written.

Anything that commits is judged by a second, separate Jev call that sees the screen and the
step about to happen, and answers safe / ask / refuse with a reason. A word list in code
checks the same step independently. Either one is enough to stop and ask; a verdict that
contradicts its own reason resolves to the stricter half; an error or a timeout counts as
"ask". Two rules keep that from turning into constant questions: opening a screen to look at
it is not a change, and a click the map knows only ever led somewhere is navigation — unless
it once needed your approval, in which case it always will.

## Safety

- Actions come from fixed lists; Jev only chooses among them.
- Closing windows and power actions ask for confirmation unless YOLO mode is on.
- Clicks on buttons like Delete, Send, Buy or Log out ask even in YOLO mode.
- Speech not addressed to the computer is ignored; so is a sentence the two recognizers
  heard differently.
- Only explicit trigger phrases switch into a typing mode — a model guess never does.
- herdr agents waiting for an approval are never prompted.
