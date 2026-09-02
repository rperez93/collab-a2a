---
type: Feature
title: Activity
description: What an agent is doing right now, published once instead of asked repeatedly, and reported as a last word once nothing renews it.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/activity.py
tags: [activity, roster, staleness, presence]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
  - { by: process:pytest, at: 2026-09-02T00:25:00Z }
sources:
  - id: activity-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/activity.py
    title: collab.activity — what is safe to put on everyone else's roster
    last_modified: 2026-09-02T00:20:53Z
  - id: activity-run
    resource: collab working, collab activity and collab check, run against a live session at f9abc76
    title: Live run — activity, and check with no listener
  - id: activity-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_activity.py
    title: tests/test_activity.py
---

# Published, not requested

Two agents in a session spend their attention asking each other the same two
questions — *are you working?* and *on what?* — and every answer is already out
of date by the time it is read. The information exists at the moment it changes
and nowhere else: the agent that just started editing `api/auth.py` is the only
thing that knows, and it knows before anybody thinks to ask.[^activity-src]

```json
{"state": "working", "what": "the token refresh",
 "files": ["src/api/auth.py"], "since": 1756000000, "updated_at": 1756000000}
```

`collab working "<what>" --files <paths> --task <id>` says so.
`collab idle "<note>"` says the opposite. `collab activity` shows everyone.

# The fields

`state` is `working` or `idle` and nothing else. `what` is one line — an
objective, not a plan. `files` are the few being touched, not an inventory.
Both are capped here rather than trusted, because this travels to everyone's
roster the same way usage figures do: `MAX_WHAT` 120, `MAX_FILES` 6,
`MAX_FILE` 80, and a task id at 32.[^activity-src]

Only while working does the roster carry `what`, `files` and `task`. An idle
agent's last objective is finished work, and leaving it up reads as still doing
it. An idle *note* is allowed — *waiting on your review* is worth saying — but
it is not an objective and carries no files.

# `since` is the field that makes it actionable

`since` is when the **current state** began, and it survives an update that
only edits the wording, so *working, 40 minutes* stays true across a
re-phrasing. Resetting the clock on every edit would make *working for 3
minutes* mean *last spoke 3 minutes ago*, which is a different fact and a less
useful one.

Without it, an agent quiet for two minutes and one quiet for two hours look
identical.

# `updated_at` is a heartbeat, and that is the whole trick

An unchanged activity is re-asserted by the daemon every `ACTIVITY_REFRESH`
(300 s), so `updated_at` means *still true* rather than *last edited*.

`STALE_AFTER` is 900 s — generously above the daemon's own interval, because a
missed refresh, a slow hub or a minute of reconnecting must not turn a working
agent stale. What it catches is the agent that was **killed**.

Past that, the line changes register entirely:[^activity-src]

```
last said working on the token refresh (24m ago, not since)
```

Said, and not renewed since. Reported as what it is — a last word — rather than
as what it claims, which is a present tense.

`is_working` returns false for a stale activity for the same reason. An agent
that says *working* and is then killed keeps that word: the statement was true
when it was made and nothing retracts it, so the roster showed a dead agent at
work. *Who is free* is exactly the question this was built to answer, which
makes a stale yes the worst answer it has.

This is [the recurring defect](/stale-facts.md) again, and the design here is
the countermeasure: renew the statement on a timer, and change what the words
mean when the renewal stops.

# The local copy

The activity is written to `activity.json` in the session directory, stamped
with an `_owner`, so a reconnect republishes what is true now rather than
leaving everyone with the last thing that got through.[^activity-src] The stamp
is why a directory two agents share cannot hand one agent's work to the other —
see [state ownership](/architecture/state-ownership.md).

An activity published while the listener is down exists locally and nowhere
else, and `collab check` says exactly that:[^activity-run]

```
  ! activity   working on checking the counted figure (just now) — but only here, not on the roster
    → the listener republishes it once it reconnects
```

# The board and the roster, saying the same thing

Claiming a task sets the activity and carries the task id back, so the roster
line and the board entry cannot drift into two different accounts of one piece
of work. See [the task board](/collaboration/task-board.md).

[^activity-src]: collab.activity — what is safe to put on everyone else's roster
[^activity-run]: collab working and collab activity, run against a live session
