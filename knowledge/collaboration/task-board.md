---
type: Feature
title: The task board
description: The shared list of work, the six verbs that move a task, and the two transitions the hub refuses because they rewind a figure somebody was reading.
resource: ../../src/collab/server/app.py
tags: [tasks, board, state-machine, coordination]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: app-src
    resource: ../../src/collab/server/app.py
    title: collab.server.app — the task route and its refusals
    last_modified: 2026-09-01T23:18:43Z
  - id: board-run
    resource: ../../src/collab/cli.py
    title: A live session, driven through propose / claim / complete / cancel and the refusals
    last_modified: 2026-09-01T23:21:22Z
  - id: overview-test
    resource: ../../tests/test_overview.py
    title: tests/test_overview.py
---

# The verbs and the states

`collab task {propose,claim,update,complete,fail,cancel,list,show}`.

| Verb | Resulting state |
|---|---|
| `propose` | `TASK_STATE_SUBMITTED` |
| `claim` | `TASK_STATE_WORKING` |
| `update` | `TASK_STATE_WORKING` |
| `complete` | `TASK_STATE_COMPLETED` |
| `fail` | `TASK_STATE_FAILED` |
| `cancel` | `TASK_STATE_CANCELED` |

A task id is `T_` plus 12 hex characters, or whatever id the client names.
`collab task list --json` returns a bare JSON array; each entry carries `id`,
`title`, `state`, `owner`, `room`, `created_by`, `created_at`, `updated_at`,
`detail` and `batch`.[^board-run]

`--detail` adds a longer description, bounded at 4 000 characters. `--files`,
with `claim`, declares the files about to be touched.

# Three refusals, and what each one was protecting

All three were observed against a live hub while writing this.[^board-run]

**Proposing onto an existing id is a 409.** `propose` creates and never
overwrites. Naming an id that already existed used to fall through to the
update branch: the row was reset to `SUBMITTED` and its owner wiped, so
re-proposing a completed task dropped `done` while `total` stood still. 100%
became 50% on a batch nobody had touched, with nothing on screen to explain
it.[^app-src]

```
[fail] POST /ext/collab/v1/tasks failed (409): T_one already exists — propose
       without an id to get a fresh one, or act on that task with
       claim/update/complete
```

**Any verb against a finished task is a 409.** `FINISHED_STATES` is
`{COMPLETED, CANCELED}`, and the guard asks the *task* rather than listing the
verbs.[^app-src] It began as a list of the verbs that had been seen to
misbehave — first `claim`, then `update` — and every verb left off it was
another way back in: `fail` on a completed task dropped the numerator, and
`cancel` dropped the numerator *and* the denominator, both with nothing to
account for the fall.

`FAILED` is deliberately **not** in that set. Failing or withdrawing work that
is in progress is the retry path, and blocking it was never the point.

**Claiming somebody else's task is a 409**, with the owner named so the
claimant can go and negotiate rather than guess. The finished check runs first,
because both can be true at once and only one of them is worth acting on:
*ask alice before taking it over* sends an agent to negotiate over work that is
already done.

Re-claiming a task you already own succeeds. It is the same statement made
twice, not a conflict.

# The board and the roster say the same thing

Claiming a task is already the statement *I am doing this*, so it sets the
claimant's [activity](/collaboration/activity.md), and the activity carries the
task id back. That is what stops the roster line and the board entry drifting
into two different accounts of one piece of work.

```
[ok]   claim: T_two  two  [working]  okfcheck
       everyone's roster now shows you on T_two
[ok]   complete: T_two  two  [completed]  okfcheck
       and you are shown as idle again
```

# What a task action publishes

Every action publishes a `task` envelope carrying the action, the id, the
title, the resulting state and the owner. That kind is in the daemon's
`REFRESHES_THE_SNAPSHOT` set, so every client pulls a fresh snapshot at once
rather than waiting for its timer — see
[the client daemon](/architecture/client-daemon.md) for what happened when it
was not.

# Related

- [Batches](/collaboration/batches.md) — what the board is counted into, and
  why proposing into an open batch moves the shared bar backwards.

[^app-src]: collab.server.app — the task route and its refusals
[^board-run]: A live session, driven through propose / claim / complete / cancel and the refusals
