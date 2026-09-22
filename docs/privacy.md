# Privacy

## Stays on your machine

- **Audio.** Recognition runs locally: Whisper base.en in the service, and voxtype's
  Whisper large-v3-turbo on your GPU. No audio is uploaded anywhere.
- **Commands understood locally.** Exact phrases and their sound-alike mistakes
  ("scroll down", "select left", "new claude", "show numbers", numbers, keys, system
  actions) are decided by code on your machine. In the panel they show as LOCAL or FUZZY.
- **Dictation.** What you dictate with `transcribe` goes from voxtype straight into the
  focused app.
- **History.** `~/.local/state/omarchy-voice/history.jsonl` keeps what was heard, how it
  was routed and what was done, so `omarchy-voice misses` can show what did not work.
  Turn it off with `"keep_history": false` or delete the file.

## Sent to Jev (OpenRouter)

Open-ended commands (JEV in the panel) are decided by Jev through OpenRouter's decisions
API. One request contains:

- what you said (the transcript, at most 400 characters)
- the titles and apps of your open windows
- the text of the window under the mouse: page elements of a browser tab, accessibility
  labels of an app, or text read from the screen (OCR) — up to 250 items
- your last three voice actions
- herdr agent names, tasks and states, when a herdr window is open

Nothing is sent for commands that are understood locally, while you dictate, or when
nothing was addressed to the computer. At about 10,000 tokens a request costs roughly
$0.0004; the panel shows the running total for the day.

To send nothing at all, don't set an API key: every locally understood command keeps
working and open-ended ones show *Jev isn't set up*.

## On screen

The service reads the screen only while you speak, to understand the command you are
giving. It never records the screen, and it ignores its own on-screen messages so it
cannot act on them.
