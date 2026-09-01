---
type: Mechanism
title: The daemon lock
description: Which process is this session's listener, answered by an advisory flock held for the process's whole life rather than by a pid file that outlives it.
resource: ../../src/collab/client/exclusive.py
tags: [flock, pid, exclusion, identity]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: exclusive-src
    resource: ../../src/collab/client/exclusive.py
    title: collab.client.exclusive — the daemon slot
    last_modified: 2026-09-01T23:21:22Z
  - id: lock-flow-test
    resource: ../../tests/test_lock_flow.py
    title: tests/test_lock_flow.py
stale_after: 2027-09-01T00:00:00Z
---

# A pid is not an identity

`daemon.pid` outlives the process that wrote it. `SIGKILL`, an OOM kill and a
reboot all leave the file exactly where it was, and the kernel hands the same
number out again afterwards.[^exclusive-src] So *the pid in the file is alive*
has meant *some unrelated process exists* often enough to do damage:
`collab status` reported a listener that had been dead for days, and
`stop_orphans` — which runs unprompted from both `host` and `join` — sent
`SIGTERM` and then `SIGKILL` to whatever had inherited the number.

A `wsl --shutdown` makes that likely rather than unlikely, because the pid
counter restarts at 1 while the stale files in the repository survive.

This is [the recurring defect](/stale-facts.md) in its purest form: a fact that
was true when it was recorded, read later as a statement about now.

# What replaced it

An advisory `flock` on `daemon.lock`, taken once and held for the daemon's
whole life. The kernel releases it when the process ends by whatever route, so
there is no stale state to reason about and no cleanup path to get wrong —
which is the point, since every case that hurt was a case where the cleanup
path never ran.

It is also the exclusion the daemon never had. Two starts racing for one
session both used to succeed: the second overwrote the first's pid file, and
then the loser's teardown deleted the *winner's* pid file on its way out,
leaving a daemon that was streaming happily and invisible to everything looking
for one.

The file descriptor is opened once and never reopened, because `flock` belongs
to the open file description rather than to the descriptor: closing any
duplicate would drop the lock while the daemon carried on believing it held
one.

# Acquiring is retried, and the number is measured

`ACQUIRE_ATTEMPTS` is 40 at `ACQUIRE_PAUSE` 0.005 s — a fifth of a
second.[^exclusive-src] A probe holds a *shared* lock for microseconds and a
daemon holds an *exclusive* one for a lifetime, but `EWOULDBLOCK` looks
identical either way, so an acquire that met a probe at the wrong instant
announced that another daemon already held the session and exited, leaving
nothing running at all.

The figures recorded in the source: 1 396 wrongly refused starts in 20 000
against a busy prober, measured across separate processes, and 55 in 300
against three spinning threads, which is the harsher case and the one the test
reproduces. Three attempts did not survive it; a fifth of a second does.

# The limits, which are written down rather than glossed

`flock(2)` is POSIX, so the lock is the *more* portable half of this and is
expected to hold at full strength on macOS and the BSDs — **expected, not
measured**. What has been measured is ext4, and WSL2's 9p mount over `/mnt/c`,
where exclusion held and the lock was free the instant its holder was
`SIGKILL`ed. Nobody has run this on macOS or on NFS.[^exclusive-src]

`started_at` is the weaker second answer throughout: it is what remains when a
filesystem cannot lock, and what reads a pid file written by an older collab.
It comes from `/proc`, which macOS does not have, and falls back to `ps` there,
answering only to the second.

**And the lock is trusted absolutely when it says free.** `taken()` returning
`False` is taken as proof that no daemon is running, and everything above rests
on that one assumption. A filesystem that reports a successful `flock` without
enforcing it — the classic NFS-without-a-lock-daemon and SMB failure — breaks
it silently and in the worst direction: a live daemon reads as dead, and a
second one starts on top of it. There is no way to detect that from inside, and
the `enforced` flag does not catch it either, because such a filesystem says
yes.

A filesystem that cannot lock at all is treated differently and deliberately:
`acquire` returns `True` with `enforced` `False`. Losing the exclusion is bad;
refusing to run a session because the state directory lives on a share is
worse.

This concept carries a `stale_after` a year out rather than none, because it is
a platform observation. It decays with the platforms, not with collab.

# Related

- [State ownership](/architecture/state-ownership.md) — the *other* lock, which
  answers a different question.
- [The client daemon](/architecture/client-daemon.md).

[^exclusive-src]: collab.client.exclusive — the daemon slot
