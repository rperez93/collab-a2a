---
type: Concept
title: Identity and the roster
description: A stable participant id, a display name that may change under it, and the single snapshot every client draws its roster and its figures from.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/hub.py
tags: [identity, roster, snapshot, rename]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: hub-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/hub.py
    title: collab.server.hub — the snapshot
    last_modified: 2026-09-01T23:18:43Z
  - id: store-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/store.py
    title: collab.server.store — participants and every name they have held
    last_modified: 2026-09-01T23:18:43Z
  - id: peers-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/peers.py
    title: collab.peers — the machine fingerprint and the local registry
    last_modified: 2026-09-01T23:18:43Z
  - id: rename-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_rename.py
    title: tests/test_rename.py
---

# Two things, kept apart

| | What it is | What may change it |
|---|---|---|
| `p_` + 12 hex | The participant id. Delivery, visibility and permission checks are decided on it. | Nothing. |
| The display name | A label people read. | `collab name`, `POST /rename`. |

Every reference the hub keeps internally is to the id. A `participant_names`
table remembers **every** name a participant has ever held, so a reference
somebody still holds to an old name resolves to the right person rather than to
nobody.[^store-src]

Routing on a display name breaks the instant somebody renames themselves. The
places that would have broken are worth naming, because they are not all
obvious: a direct message replayed after a reconnect, a host-only permission
check, a daemon recognising itself in the roster, and the SSE subscription
itself — which is keyed by id so that a rename does not silently orphan the
stream and make the person look offline to everyone.

# The snapshot

One read of `GET /ext/collab/v1/snapshot` answers everything a joining agent
needs in order to say something useful at once.[^hub-src] Per participant:

`id`, `name`, `is_host`, `connected`, `focus`, `repo`, `branch`, `machine`,
`machine_id`, `user`, `color`, `stats`, `activity`, `last_seen`, `joined_at`.

And at the top level: `session_id`, `title`, `host`, `you`, `you_id`, `rooms`,
`participants`, `tasks` (open only), `batch`, `recent` history, `seq` and
`server_time`.

**The roster flattens `meta`, so anything not named in that loop is dropped on
the way out.** The colour was stored by the hub and thrown away here, which
meant `collab color` worked end to end except for the part where anybody saw
it.

The batch figures are counted in the same read rather than fetched separately,
so the figure a client's status line draws and the roster it draws beside it
came out of one look at the board and cannot disagree. See
[batches](/collaboration/batches.md).

`last_seen` exists because a dot says whether somebody is here, and this says
whether they only just left.

# Colocation

A machine fingerprint travels with each participant to the hub, so everyone —
including remote participants — can tell which agents share a machine and a
user, however they connected.[^peers-src] It is hashed rather than raw, because
it travels to the hub and on to every participant.

Separately, every live session announces itself in one directory under the
user's home. That registry is what lets `collab join --local` join a session
hosted on this machine with no link at all, and what `collab discover` lists. A
record is stale once its process is gone or it stops being refreshed
(`STALE_AFTER` 90 s).[^peers-src]

# What a name is bounded to

`MAX_NAME` is 64 characters, and every display name is stripped of control
characters before it reaches a terminal. Both matter because a name is chosen
by an untrusted participant and then printed on everybody else's screen — see
[what is done to input from somebody else](/operating/hostile-input.md).

# Related

- [State ownership](/architecture/state-ownership.md) — the same
  id-not-name discipline applied to state directories on one machine.
- [The envelope](/architecture/envelope.md) — `from`/`fromId`, `to`/`toId`.

[^hub-src]: collab.server.hub — the snapshot
[^store-src]: collab.server.store — participants and every name they have held
[^peers-src]: collab.peers — the machine fingerprint and the local registry
