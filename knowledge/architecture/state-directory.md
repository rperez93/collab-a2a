---
type: Filesystem Layout
title: The state directory
description: Every file collab writes under a repository, what each one is for, and the permissions each was measured to carry.
tags: [state, filesystem, permissions, layout]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: config-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/config.py
    title: collab.config — where state lives, and ensure_home
    last_modified: 2026-09-01T23:18:43Z
  - id: measured-tree
    resource: the state directory of a live session at f9abc76, listed with find -printf '%M %p'
    title: Live run — the state directory, as it exists
  - id: state-dirs-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_state_dirs.py
    title: tests/test_state_dirs.py
stale_after: 2027-03-01T00:00:00Z
---

# Where it is

Session state is **per repository**: a `.collab/` directory at the git top
level, or at the current directory when that is not a repository.[^config-src]
Two checkouts on one machine therefore hold two independent sessions, which is
what you want when two agents on the same box are working on different
projects.

`COLLAB_HOME` overrides it outright. `--home FOLDER` names one for a single
session. A second agent in the same repository gets `.collab-<name>` beside the
first — see [state ownership](/architecture/state-ownership.md).

Only the default display name is global, because that is a property of the
person rather than of the project. It lives in
`~/.config/collab/config.json`, or wherever `COLLAB_CONFIG` points.

# The tree

Listed from a real session hosted against a scratch `COLLAB_HOME`, with a
listener running and a wake armed.[^measured-tree] Modes are as measured, not
as intended.

```
.collab/                                    drwx------
  .gitignore                                -rw-r--r--
  agent.lock                                -rw-------
  current                                   -rw-r--r--
  sessions/                                 drwxr-xr-x
    s_c7928bd7/                             drwxr-xr-x
      hub.json                              -rw-------
      hub.db  hub.db-wal  hub.db-shm        -rw-r--r--
      hub.log                               -rw-r--r--
      profile.json                          -rw-------
      daemon.lock                           -rw-------
      daemon.pid                            -rw-r--r--
      daemon.log                            -rw-r--r--
      status.json                           -rw-r--r--
      snapshot.json                         -rw-r--r--
      inbox.jsonl                           -rw-r--r--
      inbox.db  inbox.db-wal  inbox.db-shm  -rw-r--r--
      files/                                drwxr-xr-x
      wake/config.json
```

Two more files appear in a session directory once the agent has something to
say about itself: `agent_stats.json`
([usage figures](/collaboration/usage-figures.md)) and `activity.json`
([activity](/collaboration/activity.md)). Both carry an `_owner` stamp and are
refused on read when the stamp is not this agent's.

# What each one is

| Path | What it holds |
|---|---|
| `.gitignore` | `*` — everything here is either a secret or local scratch state, so none of it is ever committed. |
| `agent.lock` | Which agent is using this repository's collab state. See [state ownership](/architecture/state-ownership.md). |
| `current` | The session id this home currently answers about. |
| `hub.json` | What the detached hub needs to come up: port, bind, invite, host token, tunnel state. |
| `hub.db` | The append-only event log, the participants, the rooms, the board, the batches. |
| `hub.log` | The detached hub's stdout and stderr. |
| `profile.json` | This participant's credentials and identity for the session. |
| `daemon.lock` | The advisory `flock` a live listener holds. See [the daemon lock](/architecture/daemon-lock.md). |
| `daemon.pid` | The listener's pid, for *saying* — never for deciding whether it is alive. |
| `status.json` | Everything the status line and `collab status` read. Written atomically every 3 seconds. |
| `snapshot.json` | The last roster, rooms and batch figures the daemon fetched. |
| `inbox.jsonl` | Every event, one JSON object per line, for `collab listen --follow`. |
| `inbox.db` | The same events with a durable read cursor, for `collab recv`, and the resume `seq`. |
| `files/` | Blobs the hub is holding for their recipients, each named by a server-generated id. |
| `wake/config.json` | The armed wake command and its settings. |

# The permissions, stated honestly

The directory is `0700`, re-asserted on every call to `ensure_home` rather than
only at creation.[^config-src] The individually sensitive files are `0600`:
`agent.lock`, `hub.json`, `profile.json`, `daemon.lock`.

**The message log is not.** `hub.db` and `inbox.db` are SQLite files created at
the default umask, and the measurement above shows them `0644`. What protects
them is the `0700` on the directory above: on a shared machine, closing the
traversal is what actually keeps another local user out of the conversation,
the roster and everyone's usage figures. That is the design as written, and it
is worth knowing which half of it is doing the work — a file copied out of this
tree carries no protection at all.

# Related

- [Sessions](/architecture/session.md) — what `hub.json` and `profile.json`
  each record.
- [The trust model](/operating/security-model.md).

[^config-src]: collab.config — where state lives, and ensure_home
[^measured-tree]: A live session hosted against a scratch COLLAB_HOME, then listed with find
