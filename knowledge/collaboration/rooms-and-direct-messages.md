---
type: Feature
title: Rooms and direct messages
description: Where a message goes, who is entitled to see it live and on replay, and why the sender always gets their own message back.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/hub.py
tags: [rooms, dm, delivery, visibility]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: hub-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/hub.py
    title: collab.server.hub — entitlement and fan-out
    last_modified: 2026-09-01T23:18:43Z
  - id: store-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/store.py
    title: collab.server.store — history filtered by viewer
    last_modified: 2026-09-01T23:18:43Z
  - id: rooms-run
    resource: collab rooms, run against a live session at f9abc76
    title: Live run — the room list
---

# Rooms

Every session starts with one room, `general`, created when the session is
minted. `collab rooms` lists them; `collab rooms --create <name>` adds one, and
publishes a `presence` event saying so. A room name is bounded at 64
characters.

```
$ collab rooms
 * #general
```

Rooms are a namespace, not a permission boundary. Anybody admitted to the
session can list them and post in them. The
[trust model](/operating/security-model.md) says so plainly: an invite is a key
to the room, and collab controls who gets in rather than making a room member
harmless.

# Direct messages

`collab send --to <name> "..."` addresses one participant. Entitlement is
decided by `_entitled`, and it is three lines:[^hub-src]

- If the envelope has a recipient, it reaches exactly two participant ids — the
  recipient's and the sender's.
- Otherwise it is room-wide.

The comparison is by **id**. A participant who renamed themselves keeps
receiving their own messages, and a direct message addressed to a name somebody
else has since taken resolves to the person who held it, not to the new holder.

**The sender gets their own message back.** That is deliberate, and it is what
keeps every participant's local log identical, which is what makes `seq`-based
resume sound at all. `collab listen` and `collab recv` hide your own messages
by default; `--mine-too` shows them.

# Replay does not widen visibility

`store.history` over-fetches and then filters each row through the same
visibility test before returning it, so a backfill or a reconnect shows a
viewer exactly what live delivery would have shown them.[^store-src] A private
conversation stays private across a dropped connection, which is the moment it
would otherwise leak.

# A slow consumer is dropped, not waited for

If a subscriber's queue is full — 1000 events — the oldest is discarded to make
room for the newest rather than blocking the fan-out.[^hub-src] A consumer that
far behind is not coming back; it will resume from its stored `seq` when it
reconnects, and holding up delivery for everybody else to spare it would trade
one slow client for the whole session.

# Threads

`collab send --thread <id>` sets the envelope's `thread` field. It is carried
and displayed; it does not affect delivery or visibility.

# Related

- [The envelope](/architecture/envelope.md) — `room`, `to`, `toId`, `thread`.
- [The event feed](/architecture/event-feed.md) — how a message reaches a
  participant, and how a reconnect resumes.

[^hub-src]: collab.server.hub — entitlement and fan-out
[^store-src]: collab.server.store — history filtered by viewer
[^rooms-run]: collab rooms, run against a live session
