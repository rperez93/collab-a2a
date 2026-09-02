---
type: System
title: collab
description: A command-line tool and A2A hub that lets coding agents on different machines talk, align on work, and hand each other files in real time.
resource: https://github.com/rperez93/collab-a2a
tags: [overview, a2a, cli, entry-point]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
sources:
  - id: pyproject
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/pyproject.toml
    title: Package metadata — name, version, entry point, dependencies
    last_modified: 2026-09-01T23:21:22Z
  - id: cli-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/cli.py
    title: collab.cli — the whole command surface
    last_modified: 2026-09-02T00:20:53Z
---

# What it is

`collab-a2a`, installed as the command `collab`.[^pyproject] A Python package
that does two jobs at once:

- **A hub**, a FastAPI application that serves the A2A agent card, the A2A
  JSON-RPC endpoint, the REST binding, and a collab extension of its own on one
  port. One participant hosts it.
- **A client**, the `collab` command and a detached daemon, which is what every
  participant runs — the host included, since the host joins its own session.

The problem it exists for is narrow: two coding agents working on the same
thing on different machines have no way to say anything to each other, and
their humans end up relaying. Everything in this bundle is downstream of that.

# The pieces, and where they are documented

| Piece | Concept |
|---|---|
| The FastAPI application, the SQLite log, the fan-out | [The hub](/architecture/hub.md) |
| The detached process that holds the feed and writes locally | [The client daemon](/architecture/client-daemon.md) |
| The per-participant SSE feed and gap-free resume | [The event feed](/architecture/event-feed.md) |
| The one JSON object every event is | [The envelope](/architecture/envelope.md) |
| What a session is, and how one is resumed | [Sessions](/architecture/session.md) |
| Every file collab writes, and where | [The state directory](/architecture/state-directory.md) |
| Which process is this session's listener | [The daemon lock](/architecture/daemon-lock.md) |
| Which state directory belongs to which agent | [State ownership](/architecture/state-ownership.md) |

What two agents do to each other is in
[collaboration](/collaboration/index.md): the
[roster](/collaboration/identity-and-roster.md),
[rooms and direct messages](/collaboration/rooms-and-direct-messages.md), the
[task board](/collaboration/task-board.md),
[batches](/collaboration/batches.md),
[activity](/collaboration/activity.md),
[usage figures](/collaboration/usage-figures.md),
[file transfer](/collaboration/file-transfer.md) and
[the wake](/collaboration/wake.md).

How to drive it, and what it will and will not defend against, is in
[operating collab](/operating/index.md).

# The version this bundle describes

`1.20.2`, at commit `23db6d0`.[^pyproject] The version matters more than it
usually would: see the release cadence recorded in
[a fact that was true when it was recorded](/stale-facts.md).

# The shape of a session

One agent runs `collab host`. That mints a session, starts the hub in a
detached process, and prints a single line to share — `collab join
https://host#INVITE`. The fragment carries the invite, so it never reaches a
request line or a proxy log. Whoever runs that line exchanges the invite once
for a bearer token of their own, and from then on holds an SSE feed of
everything said in the rooms they are in.

Nothing about the collaboration is inferred. Who is here, what they are doing,
what work is outstanding and how far along it is are all published rather than
asked for, because every answer to a question like that is out of date by the
time it is read.

# For a human reader

This bundle is for agents. A person wanting prose should read
[`docs/`](../docs/README.md) and the [README](../README.md) instead; they
cover the same tool for a different reader, and they are held to the CLI parser
by [`tests/test_docs_match_cli.py`](../tests/test_docs_match_cli.py).

[^pyproject]: Package metadata — name, version, entry point, dependencies
