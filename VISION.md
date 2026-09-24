# Vision

Applications are built to be operated by a person: menus, buttons, settings, forms. Agents
are being bolted onto them one app at a time, each seeing only its own window. The desktop
is the one place that sees every app at once.

Omarchy Voice should give **every application a standard surface** — one way to see it and
one way to operate it — and put a **harness** on top that you simply talk to. Say "go to
settings and find dark mode" in whatever app is in front of you, or "compare the price of
these two graphics cards", and it works through the interface the way you would: visibly,
step by step, stoppable at any moment, asking before anything that commits.

This document is written against what exists and what has been tried on this machine, so
every claim has a measurement behind it.

## Where we are

| | State |
|---|---|
| Hearing | Rust ear, Parakeet, ~25 ms per command, chosen on real recordings |
| Understanding | local phrases and sound-alike matching first, Jev for the rest — Jev only ever *chooses* |
| Acting | Hyprland dispatch, uinput mouse and keyboard, browser via CDP, herdr via its socket |
| Goals | skill → map → Jev, in the browser and in Electron apps; two safety gates |
| Showing | the orb and the panel, drawn by the Omarchy shell on the GPU |

## Part one: a standard surface for every app

### What was proven

Hermes desktop is an Electron app. Started with `--remote-debugging-port`, it answers the
same protocol as the browser and exposes its interface as labelled elements — 100 of them:
"Open settings", "Appearance", "Dark", "New session". The browser goal loop ran on it
unchanged:

> "go to settings and find where to enable dark mode" → Appearance → Dark — 4.5 s, $0.0007

It also showed what apps do that web pages rarely do: they repeat themselves ("Session
actions" on every row pushed the settings pane to element 81 of 100), and they do not mark
their panes as dialogs. Both are now handled — repeats are collapsed, what just appeared
comes first, dialog contents before that.

### Three ways into an app

Every application falls into one of three kinds. Each has a best way in, and the surface
picks it:

| Kind | Way in | Quality | On this machine |
|---|---|---|---|
| Electron / Chromium | the debug protocol: the real interface tree | exact | Hermes, Signal, Spotify, ChatGPT, Heroic, Helium |
| GTK / Qt | the accessibility tree (already enabled system-wide) | good | Nautilus, settings, most native tools |
| Everything else | text recognition, and a vision model that finds clickable things (OmniParser) | approximate | games, canvas apps, terminals |

On top of whichever it is sits **one element list** (label, role, position, state), **one
action set** (click, type, keys, scroll — through uinput, which every app accepts on
Wayland), and **one goal loop** with the safety gates.

### What Omarchy adds

Omarchy decides how applications start. That makes it the right place to switch the best
way in on:

- its launcher starts the Electron apps you choose with a debug port, bound to this
  machine only, so they are operable the moment they open — no per-app work by anyone;
- Hyprland says which window you mean: "in this app" is the focused window, "in Signal" is
  found by class;
- the accessibility bus is already on; Qt apps get `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`.

A debug port lets any program on the machine drive that app. It stays opt-in, per app,
listed in the panel.

### Built

The goal runs where you are: "in hermes …", the focused app, or the web. "Give voice access
to obsidian" makes Omarchy's launcher start that app with a local port from then on;
password managers are refused outright. `omarchy-voice surface` shows what can be reached.
Still to come: the accessibility tree as the way into GTK and Qt apps, and text recognition
for everything else.

## The knowledge layer: maps and skills

Two kinds of interface need two approaches. **The open web** is different on every visit —
there Jev works each goal out, choosing among what is on the page. **Known interfaces** —
Omarchy's apps, and its web apps — are the same for every user. What is learned about them
once is worth keeping, and shipping.

| | What it is | Model needed |
|---|---|---|
| **App maps** | every screen seen, and what each click revealed | no — learned from use |
| **Skills** | a goal that worked, as the labels to click in order | no — replayed |
| **Matching** | what was said, to the skill or the place on the map | no — fuzzy matching today, a small local model later |
| **Jev** | whatever the maps and skills do not cover | yes, and what it finds is kept |

This is where the research points: AutoDroid (MobiCom 2024) explores apps offline into a
transition graph and beats GPT-4 agents by 36–40 points; UI-KOBE (2026) shows small models
become reliable when the app knowledge is precomputed; SkillDroid (2026) replays compiled
skills without a model at 100% over 79 rounds and gets *better* with use while a plain agent
degrades from 80% to 44%. General screen-reading models, by contrast, still finish only
22–25% of desktop tasks.

Measured on Hermes, through the running service:

| | Chosen by | Cost | Time |
|---|---|---|---|
| "go to settings and find where to enable dark mode", first time | Jev — then kept | $0.00076 | 5.0 s |
| the same, again | skill | $0 | 3.0 s |
| the same, in other words | skill | $0 | 3.0 s |
| "go to appearance" | the map | $0 | 3.0 s |
| a fresh install, shipped skill, new wording | skill | $0 | 3.5 s |

What was learned on the way shaped it:

- **Exploring a live app is not safe.** The explorer's "navigational" buttons in Hermes
  included "Restore checkpoint" and suggested prompts that start agent tasks, and the app
  marked its chat history — not its settings — as navigation. Maps on a user's machine are
  therefore learned from goals; maps that ship are made in a disposable instance.
- **Finding is not operating.** The first run switched Hermes to dark mode when asked where
  the setting was. The judge now separates the two, and a verdict that contradicts its own
  reason resolves to the stricter half.
- **Asking too much is also a failure.** Opening settings was once put to the user. Opening a
  screen is not a change, and a click the map knows only led somewhere skips the judge —
  unless it once needed approval, in which case it always will.
- **Skills record labels, maps record screens.** Skills ship; maps from a user's machine
  never do — their screens include chat titles.

## Part two: a real harness for deep goals

### Where the loop stops today

"Compare the price of the RTX 5080 with the price of the RX 9070 XT" returned, in 2.6 s:

> "Radeon RX 9070 XT is $799.50; RTX 5080 is $1,599.99."

One search, one line quoted from the results page. US dollars, no shop, no date, no source
opened. It looks like an answer and is a snippet. The loop today is a single thread with no
memory: it can find one fact, not gather several and weigh them.

### What a deep goal needs

**A plan you can see.** The goal becomes a short to-do list before anything runs, shown in
an overlay drawn by the shell like the orb, ticking off as it goes:

```
  Compare RTX 5080 and RX 9070 XT prices
  ☑ RTX 5080 — cheapest new, Germany          geizhals.de   1 049 €
  ◐ RX 9070 XT — cheapest new, Germany        geizhals.de   reading…
  ☐ compare
  say "stop" to end it
```

**A tab per task.** Each item runs in its own tab of your browser, so you can watch any of
them, and they do not trample each other.

**A notebook of evidence.** Every finding is recorded with where it came from: the value, the
page, the line it was read from. The conclusion links back to its sources; a finding with no
source does not count.

**Extraction by choosing, conclusion by code.** Jev does not write the price down — it
*chooses* the line or element that holds it, and code reads the number and currency out of
the page's own text. Comparing is arithmetic, done in code:

> RX 9070 XT is 409 € cheaper (geizhals.de, 2 shops each, read 14:02).

That keeps the property everything else rests on: nothing on screen is invented. A written
summary in prose needs a model that *generates* — a local one, or a larger one — and is a
different level of trust, shown as such.

**The right source first.** For prices in Germany the answer is on a comparison site
(geizhals, idealo), not in a search snippet. A small table of good sources per kind of
question — prices, reviews, opening hours, specifications — makes deep goals fast and
reliable.

### Planning without generating

Most deep goals have a few shapes: compare, find, collect a list, check a state. Jev chooses
the shape and fills its slots with spans of what you said — "compare [RTX 5080] with
[RX 9070 XT] on [price]" — the same way it chooses elements on a page. The plan is built
from known shapes, so it is readable, checkable and bounded before a single click.

### Local or remote decisions

Jev decides today: fast, cheap (a comparison like the one above is a few tenths of a cent),
choosing only. A local model can sit behind the same interface for privacy or offline use.
Which one decides is settled the way the recognizer was: by a bench of real goals, scored by
whether the result was right, not by reputation.

## Part three: pointing and awareness

**The cursor points.** Voice is bad at "this", "that", "the one next to it"; the mouse is
good at them. Elements ranked by distance from the cursor turn "click that", "what's this"
and "send this to claude" into precise commands, and let hints number only the few things
near where you are looking. Talon pairs voice with eye tracking, but has no Wayland support
and none planned — on Hyprland this space is empty.

**Awareness.** The machine already emits what matters: Hyprland marks windows that want
attention, notifications arrive, agents block and finish. Collected and ranked by an
attention budget — ignore, glance (the orb changes colour), notice, interrupt — it answers
"what did I miss" and "who needs me" across everything, and mostly stays quiet.

## Principles

- **Choose, don't generate.** Jev picks among things that exist: elements, lines, plan
  shapes, spans of what was said. What is shown or done can be traced to the screen.
- **Structure first, pixels last.** Debug protocol, then accessibility, then text
  recognition. Faster, cheaper and more reliable than screenshots.
- **Visible and stoppable.** The plan is on screen before it runs and while it runs. "Stop"
  always works.
- **Two gates before anything that commits.** A check in code and a separate judgement.
  Either stops and asks; a contradiction resolves to the stricter answer.
- **Measure, don't guess.** Every choice here was made on real data from this machine, and
  the benchmark's favourite was twice the worst in practice.
- **Local first.** Hearing and common commands never leave the machine.

## Road

**Done** — the ear in Rust; the orb in the shell; goals in Electron apps through voice access;
skills and maps, learned from use and replayed without a model; three Hermes skills shipped;
the goal card showing who chose each step.

**Next — more of the surface.** The accessibility tree as the way into GTK and Qt apps; maps
for Omarchy's default apps built in a disposable instance and shipped; more shipped skills.

**Then — the deep harness.** Plan shapes, a tab per item, the notebook with sources,
conclusions computed in code. "Compare these two cards" gives a real, sourced answer.

**Then — pointing.** Radar around the cursor, "that" and "this", hints near the cursor,
"send this to claude".

**Then — awareness.** Events, the attention budget, the orb as a glance.

**Later** — a small local model for matching what was said to skills and places; sharing
skills between Omarchy users; text recognition and a vision model as the third way in.

## Open questions

- **Which Electron apps accept the debug port cleanly?** Hermes does. Others may refuse it,
  sandbox it or change behaviour. Needs a pass over the apps people actually use.
- **How much can a deep goal do before it should ask?** Reading and collecting are safe;
  the budget of steps and time for a multi-tab goal needs real use to set.
- **When is a generated summary acceptable?** Choosing is traceable, prose is not. Where the
  line sits should be visible to the user, not buried in a setting.
