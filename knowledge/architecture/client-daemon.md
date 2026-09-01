---
type: Component
title: The client daemon
description: The only thing that talks to the hub continuously, republishing every event locally three ways so an agent never has to know a reconnect happened.
resource: ../../src/collab/client/daemon.py
tags: [client, sse, resume, listener]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: daemon-src
    resource: ../../src/collab/client/daemon.py
    title: collab.client.daemon — the listener
    last_modified: 2026-09-01T23:21:22Z
  - id: inbox-src
    resource: ../../src/collab/client/inbox.py
    title: collab.client.inbox — JSONL and SQLite from one write
    last_modified: 2026-09-01T23:21:22Z
  - id: check-cmd
    resource: ../../src/collab/cli.py
    title: collab check, run against a live session with no listener
    last_modified: 2026-09-01T23:21:22Z
stale_after: 2027-03-01T00:00:00Z
---

# What it is for

An agent's turn ends. Whatever it started dies with it. The daemon does not:
it is detached, it holds the SSE feed across turns, it resumes from the last
stored `seq` after a drop, and it republishes everything locally so that a
command run three minutes later still sees what arrived.[^daemon-src]

Every participant runs one, the host included. `collab host --no-daemon` and
`collab join --no-daemon` suppress it, and `collab daemon start|stop|status`
manage it afterwards.

# Three republications from one event

| Local form | Read by |
|---|---|
| A line appended to `inbox.jsonl` | `collab listen --follow`, and anything tailing the file |
| A row in `inbox.db` | `collab recv`, which needs a durable read cursor, and the resume `seq` |
| A WebSocket frame | The bridge, for a client that wants a socket |

The JSONL and the SQLite row come from one write in the inbox, so the two
cannot disagree about what arrived.[^inbox-src]

# The timing constants

These are tuned figures, not arbitrary ones, and each was set against a
specific failure. They carry a `stale_after` for the reason set out in
[a fact that was true when it was recorded](/stale-facts.md).

| Constant | Value | Why |
|---|---|---|
| `BACKOFF_START` / `BACKOFF_CAP` | 0.5 s / 30 s | Reconnect backoff. |
| `READ_TIMEOUT` | 45 s | The hub sends a keepalive every 15 s, so silence well past that is a dead connection rather than a quiet one. |
| `STATUS_HEARTBEAT` | 3 s | How often `status.json` is rewritten. |
| `SNAPSHOT_REFRESH` | 9 s | A participant's `hello` is published *before* their own feed subscribes, so a roster read triggered by that event still shows them offline. The timer catches it. |
| `ACTIVITY_REFRESH` | 300 s | An unchanged activity is re-asserted on this interval, so its `updated_at` means *still true* rather than *last edited*. |

# What must refresh the snapshot, and what happened when it did not

`REFRESHES_THE_SNAPSHOT` is the set of event kinds that pull a fresh snapshot
immediately instead of waiting for the nine-second timer: `hello`, `presence`,
`system` and `task`.[^daemon-src]

It is a named frozen set rather than a tuple of string literals because it was
a tuple of three, and `task` was missing from it. A rename or an arrival
refreshed the roster instantly; completing a task — the one event that moves
the shared batch figure — refreshed nothing, so the number crawled up on the
poll with every client on its own independent phase. Two agents read 50% and 0%
off the same hub at the same instant, and because that skew is well inside
`batch.STALE_AFTER` neither reading was marked stale. Not late: confidently
wrong.

# `status.json`, and why the status line never touches the network

The daemon writes one file the status line reads, atomically, via a temporary
file and a rename, so a reader never sees half of it.[^daemon-src] It carries
the session id, the names, the connection state, the URL, counts of others
connected and total, unread and unread-messages counts, watcher and WebSocket
client counts, `last_seq`, the batch figures with the age of the count
attached, a heartbeat, `connected_since`, a failure count, a hint, the armed
wake's state, and the collab version.

`wake.last_wake` and `wake.last_attempt` are separate fields. One field for
both meant a wake that had never once succeeded still reported *last woke 2m
ago*.

# When it is not running

`collab check` says so, and says what to do. Run against a live session with
the listener suppressed, it printed:[^check-cmd]

```
  ✗ listener   the listener is not running
    → collab daemon start
  ✗ watching   nothing is reading this session
    → arm a watcher on `collab listen --follow` that outlives the turn, or poll `collab recv --wait 60` every turn
  ! activity   working on checking the counted figure (just now) — but only here, not on the roster
    → the listener republishes it once it reconnects
  fix these before you carry on — they are why the other agent is waiting
```

That third line is the daemon's role stated from the outside: an activity
published while nothing is listening exists locally and nowhere else.

# Related

- [The event feed](/architecture/event-feed.md) — what the daemon is holding.
- [The daemon lock](/architecture/daemon-lock.md) — how a second daemon is
  prevented, and how the first is recognised.
- [The wake](/collaboration/wake.md) — what the daemon does when messages are
  waiting and nothing is reading them.

[^daemon-src]: collab.client.daemon — the listener
[^inbox-src]: collab.client.inbox — JSONL and SQLite from one write
[^check-cmd]: collab check, run against a live session with no listener
