---
type: Component
title: The hub
description: One FastAPI application serving the A2A surface and collab's extension on a single port, backed by an append-only SQLite log.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/app.py
tags: [server, a2a, fastapi, sqlite]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: app-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/app.py
    title: collab.server.app — the routes
    last_modified: 2026-09-01T23:18:43Z
  - id: hub-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/hub.py
    title: collab.server.hub — fan-out, one queue per participant
    last_modified: 2026-09-01T23:18:43Z
  - id: store-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/store.py
    title: collab.server.store — the append-only event log
    last_modified: 2026-09-01T23:18:43Z
  - id: session-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/session.py
    title: collab.server.session — minting and locating a hosted session
    last_modified: 2026-09-01T23:21:22Z
  - id: hub-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_hub.py
    title: tests/test_hub.py
---

# What it is

A single FastAPI application. The A2A agent card, the JSON-RPC endpoint, the
REST binding and collab's own extension are all mounted on it, so one port and
one URL serve a stock A2A client and a collab-aware one alike.[^app-src]

| Surface | Path |
|---|---|
| A2A JSON-RPC | `/a2a` |
| A2A REST binding | `/rest` |
| collab extension | `/ext/collab/v1` |

The REST binding gets its own prefix rather than the root because the SDK
mounts a greedy `/{tenant}` catch-all at its prefix root, which would otherwise
own `/`.

# The extension routes

Read off the source at `f9abc76`. Every one of them except `/join` and
`/health` requires a bearer token.

| Method and path under `/ext/collab/v1` | What it does |
|---|---|
| `POST /join` | Exchange an invite for a per-participant bearer token. Rate-limited to 10 attempts per minute. |
| `GET /events` | The per-participant SSE feed. See [the event feed](/architecture/event-feed.md). |
| `POST /messages` | Send an envelope into a room, or to one participant. |
| `GET /history` | Backfill. `limit` defaults to 50 and is capped at 500. |
| `GET /rooms`, `POST /rooms` | List and create rooms. |
| `GET /participants` | The roster. |
| `GET /snapshot` | Roster, rooms and batch figures in one read. |
| `POST /stats` | Report this participant's own usage figures. |
| `POST /rename` | Change a display name without changing identity. |
| `POST /activity` | Publish what this participant is doing now. |
| `GET /tasks`, `POST /tasks` | The [shared board](/collaboration/task-board.md). |
| `GET /batch`, `POST /batch` | The [counted figure](/collaboration/batches.md). |
| `POST /files`, `GET /files`, `GET /files/{id}/content`, `POST /files/{id}/ack`, `DELETE /files/{id}` | [File transfer](/collaboration/file-transfer.md). |
| `POST /revoke` | Host only. Removes a participant and closes their feed. |
| `GET /health` | Liveness, no token. |

# Persist first, deliver second

`Hub.publish` writes the event to the store and only then puts it on each
subscriber's queue.[^hub-src] The ordering is not incidental. A message can
therefore never reach a subscriber carrying a `seq` that is not already
durable, which is the whole reason a reconnecting client can say *I have up to
412, continue from there* and get an answer that is correct rather than
optimistic.

Each connected participant has one `asyncio.Queue` with a maximum size of
1000.[^hub-src]

# The store

One append-only `events` table is the backbone. `seq` is
`INTEGER PRIMARY KEY AUTOINCREMENT`, handed out on append, and it doubles as
the SSE `id:` field. Resume after a disconnect, `/history` backfill, and
surviving a hub restart all fall out of that one decision.[^store-src]

**Identity is an id, never a display name.** Every participant gets a stable
`p_` + 12 hex characters, and a `participant_names` table remembers every name
they have ever held, so a reference somebody still holds to an old name
resolves to the right person.[^store-src] Routing a message or a permission
check on a display name breaks the instant somebody renames themselves, and
this is the invariant that made renaming safe. See
[identity and the roster](/collaboration/identity-and-roster.md).

Everything in the store is synchronous `sqlite3`, called through
`asyncio.to_thread` by its callers, so the hub takes no async-driver
dependency.[^store-src]

# Minting a session

`create_session` mints a session id of `s_` + 8 hex characters, a fresh invite
and a fresh host token — each 32 bytes from `secrets`, URL-safe — seeds the
store with an unlimited-use invite valid for 24 hours, adds the host as the
first participant, and creates the `general` room.[^session-src]

The host then joins its own session as participant zero, so it is live and
listening before anybody else connects.

# Where it runs

The hub is started detached, as `python -m collab.hub_main <session_id>`, with
`COLLAB_HOME` in its environment and its output appended to `hub.log` inside
the session directory. It resolves its own paths from the recorded home rather
than from the process working directory, because it may not have been started
from the repository.[^session-src]

If a tunnel is running, the hub re-checks every 15 seconds that it is still
forwarding.[^app-src]

# Related

- [The state directory](/architecture/state-directory.md) — where `hub.db`,
  `hub.json` and `hub.log` live.
- [The trust model](/operating/security-model.md) — what a hub is and is not
  trusted with by the clients that connect to it.

[^app-src]: collab.server.app — the routes
[^hub-src]: collab.server.hub — fan-out, one queue per participant
[^store-src]: collab.server.store — the append-only event log
[^session-src]: collab.server.session — minting and locating a hosted session
