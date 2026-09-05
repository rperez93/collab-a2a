---
type: Feature
title: The wake
description: A command, armed once, that the daemon runs when messages are waiting and nothing is reading them — and the three ways it is careful about firing.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/wake.py
tags: [wake, daemon, unattended, recipes]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: wake-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/wake.py
    title: collab.wake — the recipes, the settling, and the framing
    last_modified: 2026-09-01T23:16:47Z
  - id: agents-run
    resource: collab wake agents, run against a live session at f9abc76
    title: Live run — the wake recipes
  - id: wake-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_wake.py
    title: tests/test_wake.py
stale_after: 2026-10-01T00:00:00Z
---

# The gap it fills

Claude Code holds a Monitor across turns and needs none of this: it watches the
feed from inside its own loop. Codex and most others cannot — whatever they
start dies when the turn does — so a message arriving while they are idle is
read by nobody until their user happens to type something.[^wake-src]

The daemon already holds the feed, resumes it after a drop and outlives the
turn that started it. What it never did was *tell anybody*. The wake is that
missing half: a command, given once, that the daemon runs when messages are
waiting and nothing is reading them.

`collab wake {show,set,off,agents,deliver}`.

# Where the message lands is the whole question

The goal is the session the user already has open, because that agent already
knows what it is doing. Two routes reach it, and both are real:

- an agent with its own inter-session messaging (`codex queue --thread`), and
- for anything in a tmux pane, one line typed into the terminal it is already
  sitting in.

Where neither is available the fallback is a fresh run in the same checkout,
which knows nothing of the open session and has to read the room to catch up.
That is a consolation prize, and `collab wake agents` says so rather than
letting it pass for the real thing — it prints the two under
*into the session you already have open* and the rest under
*a new run in this checkout — it knows nothing of your open
session*.[^agents-run]

# Three things it is careful about

Each has its own way of going wrong.[^wake-src]

**Nothing to say, nothing to do.** A wake costs the user a turn of their
agent's time and money. It fires only when there is unread substance **and** no
live watcher **and** no recent poll. `WAKE_KINDS` is `chat`, `task`, `request`,
`response` — presence, hello and the roster churn behind them are bookkeeping,
and waking an agent to be told that somebody's name is now shown in a different
colour is exactly the noise that gets a feature turned off.
`POLL_COUNTS_AS_LISTENING` is 600 s, and it is the same window the status line
and `collab check` use, so all three agree on what *listening* means rather
than each holding a private opinion.

**Once, not once per message.** Five messages in a burst are one batch and one
turn. `SETTLE` is 20 s, held so the burst can finish arriving; `MIN_GAP` is
90 s, and no two turns *for messages* start closer together than that however
much arrives. The qualifier is load-bearing: the standing reminder rides this
same delivery and waits on the gap without spending it, so a message can start
a turn a second after a reminder-only one. The gap paces how often other
people's messages start a turn, and the reminder is nobody's message.
`MAX_BATCH` is 40 arrivals and `MAX_TEXT` 2 000 characters of any one message:
a batch is a turn's worth of *what did I miss*, not an archive, and the
conversation is still in the inbox.

**The batch is data.** It is what other participants said. An agent that reads
it as instruction has handed its authority to whoever spoke last, so it is
framed as evidence to interpret — and that framing is not negotiable.

# The failure modes it refuses to hide

`TIMEOUT` is 540 s: a turn that has not finished in nine minutes is not going
to. `RETRY_PAUSE` is 120 s, multiplied by the number of consecutive failures up
to `BACKOFF_STEPS` (15). `GIVE_UP_AFTER` is 3 — a wake aimed at a session that
has since been closed fails identically every time, and from the inside that is
indistinguishable from a quiet room, so past three it is said out loud rather
than retried for ever in silence.

`DEFERRED_TOO_LONG` is 3 600 s. A run of *not now* answers that long is the
same problem under a politer name: a pager is seconds, but a pane left in
tmux's copy mode overnight is a session nobody is reading, which is the silence
this feature exists to break rather than to join.

`MAX_PROMPT_BYTES` is 60 000, well under Linux's 128 KiB limit for a single
argument. Five of the recipes pass the prompt as one argument, and a batch over
that limit does not fail loudly — it fails with `Argument list too long` on
every retry, for ever, which is how a wake bricks itself.

# History is decided by seq, never by a clock

`_is_history` asks whether an envelope's `seq` is at or below the seq at which
the wake was armed.[^wake-src] The first attempt compared the envelope's `ts`
against the arming time and was wrong twice over: `ts` has one-second
resolution, so a message sent in the same second as arming looked older than
it; and the two values come from different machines' clocks, so a hub running a
minute slow would have silently dropped a minute of real messages. `seq` is one
authority counting in one direction and needs no clock at all.

An envelope with no seq is treated as new — the safe direction, since a
spurious wake costs a turn and a swallowed one costs a message nobody reads.

# The recipes

Eleven, as of `23db6d0`.[^agents-run] Two deliver into an open session
(`codex`, `tmux`); nine start a fresh run (`codex-exec`, `claude`, `gemini`,
`cursor-agent`, `opencode`, `amp`, `copilot`, `goose`, `aider`).

Every one was read off the vendor's own documentation rather than guessed,
because a wake command that is subtly wrong fails in the one place nobody is
watching: no turn starts, no error is shown, and the session looks merely
quiet. Each recipe records the URL it came from, and the `codex` one records
the version it was checked against (`codex-cli 0.151`).

Two things differ between them and both matter: whether the batch is read from
standard input or must be passed as an argument (those are wrapped in
`sh -c '… "$(cat)"'`), and which flag the vendor documents for an unattended
run, since a woken turn has no human at the keyboard to approve anything.

**This concept carries the short `stale_after`.** It is a list of other
people's command-line flags, checked against other people's releases. It is the
most perishable thing in this bundle.

# Delivery that can fail, and says so

The `codex` and `tmux` recipes go through collab's own `wake deliver` rather
than a shell string. Not merely to keep a target out of `sh -c` — though it
does — but because both need to check something before and after they act:
whether the pane still holds an agent, whether the thread still exists. A
delivery that cannot fail is a delivery that reports success while the messages
go nowhere.

The tmux route refuses to type into a pane whose program is a bare shell
(`sh`, `bash`, `zsh`, `fish`, `tmux`, `screen` and the rest), because typing a
sentence into one of those does not wake anybody: it runs the first word as a
command, which at best fails and at worst is a command. `--expect-pid` and
`--expect-command` are how the daemon proves the pane still holds what it held
when the wake was armed.

It also sends a *pointer* to the batch rather than the batch itself, because
pasting many lines into a TUI submits at the first newline.

# The three clocks, and where to read them

Later than the pin: `collab wake show` prints them, and `collab status` carries
the last two facts in two lines.

An agent that has not been woken is being held by exactly one of three things,
and they are separate questions. `settle` is the burst window before a first
turn, measured from the oldest unread message. `min_gap` is how often other
people's messages may start a turn, and only message turns spend it. The retry
pause is what a failure buys, growing with each consecutive one and clearing on
the first delivery that works. `timeout` is not a fourth clock but a ceiling on
a turn that has started.

So the page prints all three, with what each is doing now, when a delivery was
last *attempted* and when one last *arrived* — different facts, and reporting
the attempt as an arrival is how a wake that had never once succeeded looked
healthy — and the reason `due()` gives at this moment.

Asking is read-only. The reminder's interval starts the first time the daemon
asks whether one is due, so that «never reminded» and «reminded an hour ago»
are not the same stored zero; a command that asked while printing would have
started a clock by being looked at, and an agent polling it on a loop would
have pushed its own reminder over the horizon every time.

# The standing reminder's route, and asking for one now

Also later than the pin. The reminder rides this wake for an agent that cannot
hold a monitor, and a followed stream for one that can. Both routes used to
work invisibly: the monitor's drop file is overwritten by the next reminder,
the interval restarts either way, and nothing named the route — so «my agent is
not being reminded» could not be told from «it is, by the route you forgot it
had».

The route is now recorded in the wake's state as `reminded_via`, written where
the interval restarts and for the same reason: a reminder is handed to a
delivery rather than confirmed by one, and counting a failed delivery as «not
yet reminded» would pile the same paragraph onto every retry of a batch that is
already failing.

`collab remind now` makes one due immediately. It asks rather than delivering —
it leaves a marker the daemon picks up on its next heartbeat, and the daemon
decides which route carries it, so an agent holding both still gets exactly
one. A marker rather than a wound-back interval because the daemon reads its
state once, at construction, and holds it for its whole life. The request is
spent when the reminder reaches a route, not when something asks whether one is
due: that question is asked twice on the way to one delivery, and by two
commands that only report.

# It outlives the session it was armed in

Later than the pin. A wake is a command written to disk, not a running thing,
so stopping a session cannot remove it: `collab kill` ends the hub, the
listener and the tunnel, and leaves the instruction exactly where it was. While
the session is down that costs nothing and shows nothing. Resume the session
weeks later and the first batch of messages fires it, and an agent takes a turn
on a conversation whoever is at the keyboard had forgotten.

So the stop now names it, `collab kill --disarm` and `collab daemon stop
--disarm` take it with them, and `collab check` warns about a wake armed on a
session with no listener — while staying silent about one armed on a live
session, which is the wake doing its job. The other thing a stop leaves behind
is the opposite shape: a `collab listen --follow` is a live process belonging
to whatever armed it, so it is named with its pid and never signalled.

# Arming something unreviewed

`collab wake set '<command>'` accepts any command, but one that is not a
reviewed recipe requires `--yes`, because it will run unattended.
`collab wake show` prints the armed command in full. See
[the trust model](/operating/security-model.md).

Nothing here is a system service. One would only add surviving a reboot, and an
agent that is not running has nothing to be woken.

[^wake-src]: collab.wake — the recipes, the settling, and the framing
[^agents-run]: collab wake agents, run against a live session
