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

import pytest

from collab.client import daemon as d
from collab.client import exclusive
from collab.config import SessionProfile

PROBERS = 3
PROBES = 2000


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    return p


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
        for _ in range(300):
            lock = exclusive.DaemonLock(profile.dir)
            if lock.acquire():
                lock.release()
            else:
                refused += 1
    finally:
        stop.set()
        for t in noise:
            t.join()

    assert refused == 0, f"{refused} of 300 daemons stood down for nobody"
