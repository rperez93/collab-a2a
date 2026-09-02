---
type: Feature
title: Batches, and the one figure everybody sees
description: How much of a shared job is done, counted by the hub from the board because a self-reported percentage does not survive the agent that stops reporting.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/batch.py
tags: [batch, progress, counting, staleness]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: batch-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/batch.py
    title: collab.batch — the arithmetic and what it refuses to draw
    last_modified: 2026-09-01T23:18:43Z
  - id: app-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/app.py
    title: collab.server.app — the batch route, and one denominator at a time
    last_modified: 2026-09-01T23:18:43Z
  - id: batch-run
    resource: a live session at f9abc76, driven through the whole batch arithmetic and read back at every step
    title: Live run — the batch arithmetic
  - id: batch-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_batch_progress.py
    title: tests/test_batch_progress.py
---

# Why nobody reports a percentage

Two agents splitting a job need a shared answer to *how much is left*, and the
obvious way to get one — each agent says how far along it thinks it is — does
not survive contact with a stalled agent. An agent that reports 90% and then
dies goes on reporting 90%. The number was a claim, nothing retracts it, and
the collaborator reading it waits for a last 10% that is never
coming.[^batch-src]

So a batch is a set of tasks on the shared board, and the hub counts them:

```
percent = tasks completed / tasks in the batch
```

That arithmetic happens in one place, over state the hub already holds, which
is what makes every client's figure identical. There is nothing to agree about
and no way for an agent to flatter itself.

# The consequences, which are not all comfortable

Every line below was read off a live hub while writing this.[^batch-run]

**Adding a task to an open batch moves the bar backwards.**

```
███░░░░░░░░░ 33%  1/3 tasks     ← three tasks, one done
███░░░░░░░░░ 25%  1/4 tasks     ← a fourth proposed
```

The work genuinely grew, so the honest picture is a bar that falls. This is
exactly why the counts are printed beside the percentage everywhere: a
percentage alone cannot tell *we lost ground* from *there is more ground*, and
the pair can.

**Cancelling moves it forwards**, for the mirror reason. Withdrawn work is not
outstanding work, so it leaves the denominator — and is counted separately
rather than disappearing silently:

```
███░░░░░░░░░ 25%  1/4 tasks     ← the fourth task, still outstanding
███░░░░░░░░░ 33%  1/3 tasks     ← the fourth cancelled
withdrawn    1 cancelled, out of the count
```

A cancelled task can never complete, so leaving it in the denominator would put
100% permanently out of reach for a batch that is genuinely finished.

**An empty batch has no percentage at all.** `percent` returns `None`. 0% and
100% are both assertions about an empty set that a reader would act on. The
live hub printed `nothing in this batch yet`.

**99.4% is not 100%.** Everything rounds down and is clamped at 99 until every
counted task is actually done, because *complete* is the reading somebody stops
working on.[^batch-src]

**A bar with some progress never draws as none.** One task into ten rendered an
empty bar, which reads as nobody having started, so a non-zero percentage fills
at least one cell.

# One denominator at a time

Starting a second batch while one is open is a 409.[^app-src] Two open batches
would take every task proposed from then on, and the two agents watching the
bar would be watching different sums while each believed the other saw the same
figure — the one thing the feature exists to prevent. The store refuses the
insert as well, so a genuine race between two agents loses at the database
rather than slipping past a check.

Which batch a task joins is decided **at propose time, and not by the
proposer**: a task offered while a batch is open is part of that batch's work
whoever offered it. The store resolves it inside the same lock as the insert,
because reading it in the route and passing the id down left an `await` between
the read and the write, and a close landing in that window put the task into a
batch that had already closed.

Closing is likewise a 409 if it is already closed. Answering 200 published
*closed the batch X* to the room for an event that did not happen, which is the
same untruth as a stale figure: a statement about now, assembled out of
something that was true before.

Closing keeps the tasks and the counts. New tasks belong to no batch until
another is started.

# The figures are the hub's, and a client may be looking at a memory

This is the concept's whole point, and the reason it is cross-referenced from
[a fact that was true when it was recorded](/stale-facts.md).

A client that cannot reach the hub does not have the figures. It has the ones
it had last time. `is_stale` is how a reader tells those apart, and every
renderer here refuses to draw a bar from a remembered count.[^batch-src]

`STALE_AFTER` is 30 seconds, and the number is derived rather than chosen by
feel. A healthy client refreshes on the events that move the figure, which
arrive in milliseconds; the floor is set by the silent case, where nothing has
happened and the only refresh is the daemon's timer — `SNAPSHOT_REFRESH` 9 s
inside a loop that sleeps `STATUS_HEARTBEAT` 3 s, so a healthy quiet client
refreshes every 9–12 s. Anything at or under that would flap into *unknown*
during an ordinary lull, and a staleness marker that cries wolf is one people
stop reading. 30 s is 2.5× the worst healthy interval: room for one missed
refresh and a slow request, and still short enough that a dead hub is called
out inside half a minute.

# The figures arrive from a remote party, and are treated that way

A guest's daemon copies the hub's counts into its own status file verbatim, so
every reader is parsing something somebody else chose.[^batch-src]

- `count_of` floors at zero and survives a non-integer. `int()` on them was a
  straight trust: `done: "x"` raised `ValueError`, the status line's top-level
  handler swallowed it, and the **entire** collab segment vanished from that
  agent's bar — not the batch figure, the whole thing, silently.
- `done: -5` rendered *-50% -5/10* and a nine-character bar into a six-column
  budget, so `bar` clamps on the way **out** as well as on the way in. A bar
  wider than the width it was measured at is the one thing the status line's
  arithmetic cannot survive.
- `is_complete` requires `done == total` exactly. `done > total` is not
  *more than complete*; it is nonsense from a hub the client does not control,
  and it reported *50/10 done* for a batch that was not. Two disagreeing
  figures are a reason to say nothing, not a reason to believe the larger one.

# An honest caveat about the bar characters

`█` is East Asian width Ambiguous and `░` is Neutral, so the rendered bar is
**not** guaranteed to occupy the columns it is measured at. An earlier comment
in the source claimed it was, and that claim was withdrawn. In a CJK locale, or
under tmux with `ambiguous-width double`, `█` is drawn two columns wide while
the width function still counts it as one, and a six-character bar takes
twelve. The characters are kept because the TUI's frame strokes are Ambiguous
too, so this is the assumption the whole project already runs on rather than a
new one.[^batch-src]

# Related

- [The task board](/collaboration/task-board.md) — what is being counted.
- [The client daemon](/architecture/client-daemon.md) — why a task event must
  refresh the snapshot immediately.

[^batch-src]: collab.batch — the arithmetic and what it refuses to draw
[^app-src]: collab.server.app — the batch route, and one denominator at a time
[^batch-run]: A live session, driven through the whole arithmetic and read back at each step
