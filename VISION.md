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
| Goals | a step loop with two safety gates — wired to the browser only |
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

### Not built yet

The test ran from a script. Missing for it to be a feature: launching apps with the port,
pointing the goal loop at the focused window instead of the browser, and falling back to the
accessibility tree and text recognition when there is no port.

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

**Next — the standard surface.** Electron apps launched with a debug port (opt-in), the goal
loop pointed at the focused window, accessibility tree as the second way in. "In this app,
go to settings and find X" works by voice in Hermes, Signal, Spotify.

**Then — the deep harness.** Plan shapes, the to-do overlay, a tab per item, the notebook
with sources, conclusions computed in code, a table of good sources. "Compare these two
cards" gives a real, sourced answer.

**Then — pointing.** Radar around the cursor, "that" and "this", hints near the cursor,
"send this to claude".

**Then — awareness.** Events, the attention budget, the orb as a glance.

**Later** — text recognition and a vision model as the third way in; a local decision model
where the bench says it is good enough; a personal command recognizer; plugins for apps
that want to offer more than their interface shows.

## Open questions

- **Which Electron apps accept the debug port cleanly?** Hermes does. Others may refuse it,
  sandbox it or change behaviour. Needs a pass over the apps people actually use.
- **How much can a deep goal do before it should ask?** Reading and collecting are safe;
  the budget of steps and time for a multi-tab goal needs real use to set.
- **When is a generated summary acceptable?** Choosing is traceable, prose is not. Where the
  line sits should be visible to the user, not buried in a setting.
