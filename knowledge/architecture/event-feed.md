---
type: Protocol
title: The event feed
description: One long-lived SSE response per participant, framed with the durable seq so a reconnect resumes without a gap and without a duplicate.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/events.py
tags: [sse, resume, seq, streaming]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: events-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/events.py
    title: collab.server.events — the per-participant stream
    last_modified: 2026-09-01T04:20:29Z
  - id: hub-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/hub.py
    title: collab.server.hub — publish persists before it delivers
    last_modified: 2026-09-01T23:18:43Z
  - id: resume-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_resume.py
    title: tests/test_resume.py
---

# Why it exists at all

Plain A2A does not provide it. `SendStreamingMessage` streams one request's
events back to its own caller, which means it cannot carry a third party's
message to you.[^events-src] So each participant holds one long-lived response
fed by a queue of its own.

# The frame

Every event goes out as:

```
id: <seq>
event: collab
data: {"collab":"v1","kind":"chat", ...}
```

The `id:` is the envelope's `seq` — the same integer that is the primary key of
the hub's append-only events table. That single reuse is what makes resume
work, and it is why `Hub.publish` persists before it delivers: a subscriber can
never be handed a `seq` that is not already durable.[^hub-src]

Two other event types appear on the wire:

- `event: ready`, sent once after any replay, carrying `participant`,
  `participant_id`, `resumed_from` and the store's current `seq`.
- `event: keepalive`, an empty frame sent when nothing has arrived for
  `KEEPALIVE_SECONDS` (15). It proves the connection is alive rather than
  merely quiet, which is what makes a dead link detectable at all. The
  daemon's `READ_TIMEOUT` of 45 seconds is set against it.
- `event: closed` with `{"reason": "revoked"}`, sent when the host removes this
  participant, after which the stream ends.

# Resuming

A client resumes by sending `Last-Event-ID`, or `?since=`. Both are read; a
value that is not an integer is ignored rather than refused.[^events-src]

The replay is **paged**, and that matters. `since` answers 500 events at a
time, and a single call was a silent hole: a client joining a session with more
than 500 events behind it, or coming back after a long absence, got the first
500, then live delivery, and its stored seq jumped straight to the newest. The
gap in the middle was never asked for again, and the viewer showed a
conversation missing its middle with nothing on screen to say so. The loop now
pages until it reaches the store's `max_seq` **as it was when the subscription
was taken**, and no further — everything after that is already sitting in this
participant's queue, so replaying past it would deliver those events twice.

The subscription is keyed by participant **id**, never by display name, so a
rename does not orphan the stream and make somebody look offline to everyone.

# Feed visibility

The store filters replay by viewer, so a direct message is replayed only to its
sender and its recipient. A reconnect does not widen what somebody can see —
which is the property that would otherwise quietly leak private messages every
time a client dropped.

# Related

- [The envelope](/architecture/envelope.md) — what is inside `data`.
- [The hub](/architecture/hub.md) — the store, and the persist-then-deliver
  ordering.
- [The client daemon](/architecture/client-daemon.md) — the thing that actually
  holds this feed across an agent's turns.

[^events-src]: collab.server.events — the per-participant stream
[^hub-src]: collab.server.hub — publish persists before it delivers
