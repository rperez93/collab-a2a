---
type: Mechanism
title: The daemon lock
description: Which process is this session's listener, answered by an advisory flock held for the process's whole life rather than by a pid file that outlives it.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/client/exclusive.py
tags: [flock, pid, exclusion, identity]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
  - { by: process:pytest, at: 2026-09-02T00:25:00Z }
sources:
  - id: exclusive-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/client/exclusive.py
    title: collab.client.exclusive — the daemon slot
    last_modified: 2026-09-02T00:17:31Z
  - id: daemon-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/client/daemon.py
    title: collab.client.daemon — the backstop refusal, and what /proc costs
    last_modified: 2026-09-02T00:20:53Z
  - id: lock-flow-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_lock_flow.py
    title: tests/test_lock_flow.py
  - id: platform-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_platform_support.py
    title: tests/test_platform_support.py
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

# Three ways `acquire` can fail to give you a real lock

Three faults, three answers, and they are not interchangeable. Reading any two
of them as one is what made this concept wrong at `f9abc76`.

**1. A platform with no locking primitive at all — refuses.** Where `fcntl` is
absent (Windows), `acquire` raises `UnsupportedPlatform`, and the message says
to run under WSL 2 or later.[^exclusive-src] There is nothing there to be right
or wrong about: with no `flock`, two daemons for one session both came up, both
streamed the feed, and the pid file named whichever wrote it last — while
`enforced` said `False` and nothing surfaced it.

**2. A filesystem that will not lock — runs.** `flock` raises something outside
`EACCES`/`EAGAIN`/`EWOULDBLOCK`, so `_flock` answers `None` rather than `True`
or `False`, and `acquire` returns `True` with `enforced` `False`. Losing the
exclusion is bad; refusing somebody a session because their state directory
lives on an unusual mount is worse, and `started_at` is still there as a
second opinion.

**3. An unwritable state directory — also runs.** `os.open` raises, and
`acquire` returns `True` with `enforced` `False` without ever reaching the
lock. Same outcome as case 2, different fault, and worth keeping distinct
because the remedy is not the same one.

**The order of those checks is deliberate.** The platform test comes first, so
a machine that is both unlockable *and* unwritable is told to run under WSL 2
rather than told its directory is read-only. The second message would be true
and would send somebody after the wrong fault — there is no directory
permission that makes Windows lockable.

The refusal is enforced twice over, at both ends. `collab` catches it once in
`main` and only for the commands that actually needed a daemon, so
`collab --help` and `collab update` are not walled off; and `Daemon.run`
checks before it creates the state directory, so a daemon started by hand
leaves nothing behind saying it ran.[^daemon-src]

**This section is the reason the re-pinning rule exists.** Until `23db6d0`
this concept ran cases 1 and 2 together as one, and described the behaviour
that was removed when they were split. It was true when it was written and
false by the time it was read — the subject of [a fact that was true when it
was recorded](/stale-facts.md), committed here in a document about it. See
[how to read this bundle](/how-to-read-this-bundle.md) for the rule that caught
it.

# Three platforms, three answers

Linux has both halves. macOS has the lock and loses everything that reads
`/proc`: `started_at` drops to `ps` and one-second precision, `is_zombie`
cannot tell a process that has exited from one still running, and `environ`
cannot be read at all — so an orphan from before the lock existed is left where
it is for `collab daemon stop` to clear, rather than being signalled. That is
the direction to fail in. Windows has neither half, and is refused.

**What happens without `/proc` is simulated, not measured.** The tests patch
`_HAVE_PROC` to `False` on Linux, which walks the branches macOS would walk on
a kernel that is not macOS. Nobody has run this on a Mac.[^exclusive-src]

# A pid of zero is not a pid

`parse` rejects anything at or below zero, because every reader of
`daemon.pid` eventually hands the number to `os.kill`, and there they do not
mean what they look like: `kill(0, sig)` signals the caller's **entire process
group** and `kill(-1, sig)` signals every process the user can reach. A
truncated write is all it takes, and a pid file containing `0` killed a test
run here — the runner that read it.[^exclusive-src]

It is refused in the parser rather than at each `os.kill`, so a reader written
later is safe by having been written at all. That is the same move as the lock
itself: put the guarantee where it cannot be forgotten, rather than at every
site that would have to remember.

This concept carries a `stale_after` a year out rather than none, because it is
a platform observation. It decays with the platforms, not with collab.

# Related

- [State ownership](/architecture/state-ownership.md) — the *other* lock, which
  answers a different question.
- [The client daemon](/architecture/client-daemon.md).

[^exclusive-src]: collab.client.exclusive — the daemon slot
[^daemon-src]: collab.client.daemon — the backstop refusal, and what /proc costs
