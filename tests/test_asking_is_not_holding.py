"""Asking whether a daemon holds the lock must not look like holding it.

The probe took an EXCLUSIVE lock and gave it straight back, so every prober
excluded every other prober and each reported a daemon that was in fact the
other one asking. Two probers is not a rare arrangement — a status line and a
watch pane on one session is two, and `collab status` and a join is two.

It is worse than a wrong answer, because of how `is_running` reads it: a lock
that says held returns the recorded pid without consulting the start time. So a
phantom resurrects a pid the start time had already ruled out, and
`stop_orphans` will then signal it. That is the original defect arriving
through the mechanism meant to close it, needing no pid reuse at all.

A shared probe excludes nothing except the exclusive lock a daemon holds, which
is the only thing anyone is asking about. These tests measure a rate, because
one call proves nothing about a race.
"""

from __future__ import annotations

import os
import threading
import time

from collab.client import daemon as d
from collab.client import exclusive

#: Eight rather than three. Three passed on an idle machine and failed in the
#: full suite, which is the only load this ever runs under that resembles a
#: real one; eight with the suite alongside is the configuration the defect
#: below was measured at, so it is the configuration it is held to.
PROBERS = 8
PROBES = 2000
#: Starts attempted against that crowd. Longer than it was, for the same
#: reason there are more probers.
STARTS = 500


def _in_parallel(work, workers=PROBERS):
    """Run `work` in several threads at once and collect what each returned.

    Threads and not processes on purpose: `flock` is scoped to the open file
    description, so two threads opening the file separately contend exactly as
    two processes do — which is what made this reproducible at all.
    """
    out: list = []
    lock = threading.Lock()

    def run():
        mine = work()
        with lock:
            out.append(mine)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_probers_do_not_report_each_other_as_a_daemon(profile):
    """No daemon has ever held this lock, so every answer must be False."""
    exclusive.lock_path(profile.dir).touch()

    def probe():
        return [exclusive.taken(profile.dir) for _ in range(PROBES)].count(True)

    phantoms = sum(_in_parallel(probe))

    assert phantoms == 0, \
        f"{phantoms} of {PROBERS * PROBES} probes invented a daemon"


def test_a_real_holder_is_still_seen_through_the_crowd(profile):
    """The shared probe must not have bought quiet by answering False always."""
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    try:
        def probe():
            return [exclusive.taken(profile.dir) for _ in range(200)].count(True)

        assert sum(_in_parallel(probe)) == PROBERS * 200
    finally:
        lock.release()


def test_a_reused_pid_stays_dead_while_others_are_asking(profile):
    """The consequence, not the mechanism: a phantom short-circuits the start
    time, and `stop_orphans` signals whatever `is_running` hands it."""
    (profile.dir / "daemon.pid").write_text(f"{os.getpid()}\n1\n")
    exclusive.lock_path(profile.dir).touch()

    stop = threading.Event()

    def probe():
        while not stop.is_set():
            exclusive.taken(profile.dir)
        return None

    noise = [threading.Thread(target=probe) for _ in range(PROBERS)]
    for t in noise:
        t.start()
    try:
        answers = [d.is_running(profile) for _ in range(2000)]
    finally:
        stop.set()
        for t in noise:
            t.join()

    wrong = [a for a in answers if a is not None]
    assert not wrong, f"{len(wrong)} of {len(answers)} brought a ruled-out pid back"


def test_a_daemon_can_still_start_while_the_lock_is_being_probed(profile):
    """The converse, and it costs a session rather than a reading: a daemon
    that met a probe at the wrong instant announced that another daemon held
    the session and exited, leaving nothing running at all."""
    exclusive.lock_path(profile.dir).touch()
    stop = threading.Event()

    def probe():
        while not stop.is_set():
            exclusive.taken(profile.dir)
        return None

    noise = [threading.Thread(target=probe) for _ in range(PROBERS)]
    for t in noise:
        t.start()
    try:
        refused = 0
        for _ in range(STARTS):
            lock = exclusive.DaemonLock(profile.dir)
            if lock.acquire():
                lock.release()
            else:
                refused += 1
    finally:
        stop.set()
        for t in noise:
            t.join()

    assert refused == 0, f"{refused} of {STARTS} daemons stood down for nobody"


def test_a_real_holder_is_still_refused(profile):
    """The other direction, and the reason the deadline is what it is.

    Waiting rather than sampling would be worthless if the wait always ended
    in a yes. A lock somebody holds is never let go inside the deadline, so a
    second daemon for one session is refused exactly as before."""
    holder = exclusive.DaemonLock(profile.dir)
    assert holder.acquire()
    try:
        for _ in range(5):
            second = exclusive.DaemonLock(profile.dir)
            assert second.acquire() is False
            assert second.held is False
    finally:
        holder.release()


def test_a_refusal_costs_about_the_deadline_and_not_a_hang(profile):
    """A blocking wait with nothing bounding it is a start-up that never
    returns. What bounds it is the only thing that constant now decides."""
    holder = exclusive.DaemonLock(profile.dir)
    assert holder.acquire()
    try:
        began = time.monotonic()
        assert exclusive.DaemonLock(profile.dir).acquire() is False
        waited = time.monotonic() - began
    finally:
        holder.release()

    assert exclusive.ACQUIRE_WAIT <= waited < exclusive.ACQUIRE_WAIT + 2.0


def test_a_wait_that_was_given_up_on_does_not_keep_the_lock(profile,
                                                            monkeypatch):
    """The instant the whole handover exists for.

    The wait runs in a thread, and it can be granted the lock a moment after
    the caller has given up on it and reported that it has none. If both sides
    simply believed themselves, the lock would then be held for the life of the
    process by somebody who has already said it is not theirs — and no daemon
    could ever start in that directory again.
    """
    monkeypatch.setattr(exclusive, "ACQUIRE_WAIT", 0.01)
    holder = exclusive.DaemonLock(profile.dir)
    assert holder.acquire()

    refused = [exclusive.DaemonLock(profile.dir).acquire() for _ in range(30)]
    assert refused == [False] * 30

    holder.release()
    # Whatever those waits were granted after they were abandoned, they gave
    # straight back. Retried because the handover is a thread and this is the
    # one place a test has to wait for one.
    for _ in range(200):
        if exclusive.taken(profile.dir) is False:
            break
        time.sleep(0.01)
    assert exclusive.taken(profile.dir) is False, \
        "an abandoned wait is still holding the lock"

    after = exclusive.DaemonLock(profile.dir)
    assert after.acquire() is True
    after.release()
