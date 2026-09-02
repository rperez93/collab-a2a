---
type: Mechanism
title: Input from somebody else
description: What is done to a display name, a message, a filename and a usage figure between arriving from a remote party and reaching a terminal or a status line.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/protocol.py
tags: [security, sanitising, terminal, bounds]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: protocol-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/protocol.py
    title: collab.protocol — scrub, scrub_block, clip, bounded_meta
    last_modified: 2026-09-01T23:18:43Z
  - id: batch-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/batch.py
    title: collab.batch — bounds on the way out, not only on the way in
    last_modified: 2026-09-01T23:18:43Z
  - id: escapes-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_terminal_escapes.py
    title: tests/test_terminal_escapes.py
  - id: bounds-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_input_bounds.py
    title: tests/test_input_bounds.py
---

# A name is not text

A display name, a message, a task title and a file name are all chosen by
another participant and all end up printed to this machine's terminal — by
`collab recv`, `collab listen`, `collab who`, `collab file list`, and the
one-line render a Monitor turns into a notification.[^protocol-src]

A raw `ESC` in one of those is not text. It is a command to the terminal.
`\x1b[2J` clears the reader's screen, `\x1b]0;…\x07` rewrites their window
title, and a bare carriage return paints a forged line over a real one — so a
remote name reading `alice` could carry a cursor-up and overwrite the line
above it with anything at all.

# `scrub`, for a field

Every C0 and C1 control byte — Unicode category `Cc`: `ESC`, `CR`, `BEL`,
backspace, `DEL` and the rest — is removed. Every printable character survives
exactly as it was: letters, spaces, emoji, CJK and combining marks.

**Newlines and tabs go too, and that is the deliberate part.** Every caller
renders one *field* into one *line*: a name in a roster row, a title in a board
listing, a message in a single-line notification. A newline surviving into any
of those does not merely spoil the layout — it lets a sender write a second
line of their own into a transcript, and a forged `[dm→you] alice: …` reads
exactly like a real one. A tab does the quieter version of the same thing to a
column.

# `scrub_block`, for a message

The same, for text that is *allowed* to span lines. Newlines and tabs survive;
nothing else does — carriage return included, because `CR` is the character
that paints a forged line over a real one, and it is no part of a line break
that `\n` does not already carry.

The distinction exists because `HubError` falls back to the response body: a
dead tunnel's HTML 502 arrives as many lines, and flattening it turns something
barely readable into something not readable at all.

The curses TUI needs neither, because ncurses renders a control byte as `^[`
rather than passing it to the terminal. These are for the plain-print paths,
which hand the string straight to a real terminal.

# Bounds, on the way in

Every self-declared value is clipped and counted rather than trusted, because
each is stored and then replayed to every roster every few seconds. See
[the envelope](/architecture/envelope.md) for the table.

`bounded_meta` is the sharpest of them. The join handshake's `hello` — focus,
repo, branch — is chosen by an untrusted joiner and lands straight in that
participant's meta. Unbounded, a megabyte of display text is amplified across
the whole session on a timer. So only scalar values survive, each key and
string is clipped, and the number of keys is capped at 24. A nested `stats` or
`activity` object is dropped **on purpose**: both reach the roster through
their own sanitised endpoints, and one smuggled in through `hello` would arrive
having passed no sanitiser at all.[^protocol-src]

# Bounds on the way out, too

The figures a client draws are the **hub's**, copied into a guest's own status
file verbatim, so a renderer is parsing something a remote party
chose.[^batch-src]

- `count_of` floors at zero and survives a non-integer, because `done: "x"`
  raised `ValueError`, the status line's top-level handler swallowed it, and
  the entire collab segment vanished from that agent's bar — not the batch
  figure, the whole thing, silently. A remote party should not be able to blank
  somebody else's status line by sending a string.
- `bar` clamps the percentage again at draw time, because `done: -5` rendered
  a nine-character bar into a six-column budget, and a bar wider than the width
  it was measured at is the one thing the status line's arithmetic cannot
  survive.

Validating on the way in is not enough when the value crossed a machine
boundary after that.

# Scrubbing on assignment, not at each call site

`SessionProfile` scrubs `name` and `host_name` when they are **assigned**,
not where they are printed. Ten call sites across the CLI, the watch pane, the
TUI title and the daemon's status file read `host_name`, and wrapping each is
the arrangement that failed three times: every site was found, every site was
wrapped, and the next one written was raw again.

There is no reader anywhere that wants a control character in a display name,
so the value never needs to hold one. Assignment also catches the case a
constructor hook would miss — the daemon adopting the hub's answer after
construction, on every snapshot refresh — and a profile loaded from disk that
an older build had already written a hostile name into.

# Related

- [The trust model](/operating/security-model.md).
- [Identity and the roster](/collaboration/identity-and-roster.md).

[^protocol-src]: collab.protocol — scrub, scrub_block, clip, bounded_meta
[^batch-src]: collab.batch — bounds on the way out, not only on the way in
