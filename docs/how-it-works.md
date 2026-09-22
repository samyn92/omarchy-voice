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

## Safety

- Actions come from fixed lists; Jev only chooses among them.
- Closing windows and power actions ask for confirmation unless YOLO mode is on.
- Clicks on buttons like Delete, Send, Buy or Log out ask even in YOLO mode.
- Speech not addressed to the computer is ignored; so is a sentence the two recognizers
  heard differently.
- Only explicit trigger phrases switch into a typing mode — a model guess never does.
- herdr agents waiting for an approval are never prompted.
