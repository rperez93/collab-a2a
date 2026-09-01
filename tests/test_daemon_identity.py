"""A pid is not an identity, and everything here used to treat it as one.

`daemon.pid` outlives the process that wrote it — SIGKILL, an OOM kill and a
reboot all leave the file exactly where it was — and the kernel hands the
number out again afterwards. `kill(pid, 0)` was taken as proof of a daemon, so
`collab status` reported a listener that was somebody's editor, and
`stop_orphans` —which `collab host` and `collab join` both run unprompted, with
no flag and no prompt— sent that editor SIGTERM and then SIGKILL. A
`wsl --shutdown` restarts the pid counter at 1 while the stale files in the
repo survive, which makes the collision seconds away rather than months.

The lock the daemon now holds for its whole life is the fix for all of it: the
kernel takes it back when the process ends, however it ends.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from collab import lockfile
from collab.client import daemon as d
from collab.client import exclusive
from collab.config import SessionProfile

#: Ignores SIGTERM long enough to be caught reporting itself stopped early: it
#: removes its pid file the way the daemon's teardown does, and only then goes.
_LINGERS = (
    "import os, signal, sys, time\n"
    "def bye(*_):\n"
    "    os.unlink(sys.argv[1])\n"
    "    time.sleep(0.6)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, bye)\n"
    "open(sys.argv[2], 'w').close()\n"
    "time.sleep(30)\n"
)

HAVE_PROC = os.path.isdir("/proc/self")


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    return p


def _record(profile, pid, began=""):
    (profile.dir / "daemon.pid").write_text(f"{pid}\n{began}\n")


# --- a number that is alive is not the process that wrote it ----------------

def _left_behind(profile, pid):
    """What a killed daemon leaves: a pid file, and a lock it no longer holds.

    The pid is a live one and belongs to somebody else, which is the whole of
    the fault — after a `wsl --shutdown` the counter restarts at 1 and the
    numbers in the surviving files are handed straight back out.
    """
    (profile.dir / "daemon.pid").write_text(f"{pid}\n")
    exclusive.lock_path(profile.dir).touch()


def test_a_reused_pid_is_not_this_sessions_daemon(profile):
    """This pid is alive and it is not the daemon; only the kernel knew."""
    _left_behind(profile, os.getpid())
    assert d.is_running(profile) is None


def test_a_reused_pid_is_not_signalled_as_an_orphan(profile, monkeypatch):
    """The one with teeth: `stop_orphans` escalates to SIGKILL, runs from both
    `host` and `join` with no flag and no prompt, and says so afterwards."""
    _left_behind(profile, os.getpid())
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "kill",
        lambda pid, sig: signalled.append((pid, sig)) if sig else None)

    assert d.stop_orphans(profile.home, keep="other") == []
    assert signalled == [], "an unrelated process was signalled"


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc to know a start time")
def test_a_start_time_tells_two_processes_with_one_number_apart(profile):
    """The second answer, for a filesystem that cannot lock.

    Weaker than the lock and kept anyway: without it, losing `flock` would mean
    losing every defence at once rather than dropping to the older one.
    """
    assert exclusive.same_process(exclusive.started_at(os.getpid()), os.getpid())
    assert not exclusive.same_process("1", os.getpid())

    _record(profile, os.getpid(), "1")     # a start time we can never have had
    assert d.is_running(profile) is None


def test_a_pid_file_from_an_older_collab_is_still_believed(profile):
    """No start time and no lock means the file predates both.

    Refusing to recognise a daemon that is plainly running would turn an
    upgrade into a crash, which is a worse answer than the one being fixed.
    """
    (profile.dir / "daemon.pid").write_text(f"{os.getpid()}\n")
    assert d.is_running(profile) == os.getpid()


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc to make a zombie")
def test_a_zombie_is_not_a_running_daemon(profile):
    """A zombie keeps its /proc entry and still answers `kill(pid, 0)`.

    Latent while the daemon is reparented to init and reaped at once; it bites
    the moment a long-lived process spawns one and stays.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.time() + 10
        while not exclusive.is_zombie(child.pid) and time.time() < deadline:
            time.sleep(0.01)
        assert exclusive.is_zombie(child.pid), "the child never became a zombie"

        _record(profile, child.pid)
        assert d.is_running(profile) is None
    finally:
        child.wait()


# --- the lock, not the file, is what answers --------------------------------

def test_a_lock_nobody_holds_means_nobody_is_running(profile):
    """The pid file is what a killed daemon leaves behind; the lock is what the
    kernel takes back off it, and it is right about SIGKILL, the OOM killer
    and a reboot alike — the three that leave the file untouched."""
    _left_behind(profile, os.getpid())

    assert d.is_running(profile) is None


def test_a_held_lock_is_what_says_a_daemon_is_running(profile):
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    try:
        _record(profile, os.getpid(), exclusive.started_at(os.getpid()))
        assert d.is_running(profile) == os.getpid()
    finally:
        lock.release()


# --- two daemons for one session --------------------------------------------

def test_a_second_daemon_for_one_session_refuses_to_start(profile):
    """`ensure_daemon` checks and then spawns with nothing in between, and the
    window is the whole of a Python start-up, because nothing writes the pid
    file until the daemon reaches `run`. Both used to win."""
    first = exclusive.DaemonLock(profile.dir)
    assert first.acquire()
    try:
        asyncio.run(d.Daemon(profile).run())
        assert not (profile.dir / "daemon.pid").exists()
        assert not (profile.dir / "status.json").exists(), \
            "the loser wrote its own account of a session it does not hold"
    finally:
        first.release()


def test_a_second_daemon_touches_nothing_before_it_gives_up(profile):
    """Not one file, and «not one» has to be literal or the sentence does no
    work: opening the inbox runs the schema and leaves `inbox.db-wal` and
    `-shm` beside it, which is a trace of a daemon that never ran."""
    _record(profile, 4242, "1")
    first = exclusive.DaemonLock(profile.dir)
    assert first.acquire()
    try:
        before = sorted(p.name for p in profile.dir.iterdir())

        asyncio.run(d.Daemon(profile).run())

        assert (profile.dir / "daemon.pid").read_text() == "4242\n1\n"
        assert sorted(p.name for p in profile.dir.iterdir()) == before
    finally:
        first.release()


# --- teardown is not allowed to clear up after somebody else -----------------

def _serve_and_stop(profile, monkeypatch, during=None):
    """Run the daemon's whole start-up and teardown with no hub in the middle."""
    async def instead(self):
        if during is not None:
            during()

    monkeypatch.setattr(d.Daemon, "_connect_forever", instead)
    daemon = d.Daemon(profile)
    try:
        asyncio.run(daemon._serve())
    finally:
        daemon.inbox.close()
    return daemon


def test_a_dying_daemon_leaves_a_live_ones_pid_file_alone(profile, monkeypatch):
    """The lost race became a permanent fault right here.

    Two daemons on one session, and the loser's teardown unlinked the pid file
    unconditionally: the winner —alive and streaming— went invisible to
    `is_running`, unstoppable by `stop`, and was replaced by a third the next
    time anything called `ensure_daemon`. It needs no race to carry on.
    """
    def somebody_else_takes_over():
        _record(profile, 4242, "1")

    _serve_and_stop(profile, monkeypatch, during=somebody_else_takes_over)

    assert (profile.dir / "daemon.pid").read_text() == "4242\n1\n"


def test_a_daemon_still_named_in_the_file_does_clear_it_away(profile, monkeypatch):
    """The guard must not turn into a leak: our own record still goes."""
    _serve_and_stop(profile, monkeypatch)

    assert not (profile.dir / "daemon.pid").exists()


def _repo_lock(profile, listener_pid):
    lockfile.acquire(lockfile.Lock(
        name=profile.name, session_id=profile.session_id, role="guest",
        listener_pid=listener_pid), profile.home)


def test_a_dying_daemon_leaves_another_listeners_repo_lock_alone(profile, monkeypatch):
    """`lockfile.release` deletes the file whoever asks; the lock records the
    listener pid behind it and nothing here read it."""
    _repo_lock(profile, 4242)

    _serve_and_stop(profile, monkeypatch)

    assert lockfile.read(profile.home) is not None, \
        "a lock standing for another listener was released"


def test_a_dying_daemon_does_give_up_its_own_repo_lock(profile, monkeypatch):
    _repo_lock(profile, os.getpid())

    _serve_and_stop(profile, monkeypatch)

    assert lockfile.read(profile.home) is None


# --- stopping is about the process, not the paperwork ------------------------

def test_stop_does_not_report_success_before_the_process_is_gone(profile, tmp_path):
    """It waited on `is_running`, which is really waiting on the pid FILE — and
    the daemon unlinks that on its way out.

    Measured, the file went at about 5ms and the process at about 47ms, so
    `stop` returned True on a daemon still holding the feed, and the SIGKILL
    escalation after the loop was unreachable by every path. `daemon stop &&
    daemon start`, which the tool prints as advice, started the replacement on
    top of the one still connected.
    """
    ready = tmp_path / "ready"
    pid_file = profile.dir / "daemon.pid"
    child = subprocess.Popen(
        [sys.executable, "-c", _LINGERS, str(pid_file), str(ready)])
    try:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        pid_file.write_text(exclusive.stamp(child.pid))

        assert d.stop(profile) is True
        assert not d._alive(child.pid), "it said stopped while the process ran"
    finally:
        child.kill()
        child.wait()


# --- and the repo lock stops believing the pid file --------------------------

def test_a_stale_pid_file_does_not_make_the_repo_look_occupied(tmp_path, monkeypatch):
    """`_take_lock` copied the number out of `daemon.pid` with no check at all,
    and `Lock.held` is `any(_alive(pid))` over exactly that field — so an empty
    repo announced a live agent and the next one here was sent elsewhere."""
    from collab.cli import _take_lock

    home = tmp_path / ".collab"
    monkeypatch.setenv("COLLAB_HOME", str(home))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    p = SessionProfile(session_id="s_1", url="http://h/", name="bob",
                       host_name="alice", token="t", home=str(home))
    p.dir.mkdir(parents=True, exist_ok=True)
    p.save()
    _left_behind(p, os.getpid())

    _take_lock(p, role="guest")

    lock = lockfile.read(home)
    assert lock.listener_pid == 0
    assert not lock.held


def test_a_daemon_can_be_driven_without_going_through_serve(profile):
    """`write_status` and `_stream_once` read `self.inbox` as though it is
    simply there, so building it in `_serve` made the attribute None for the
    window between construction and serving.

    Nothing in production crosses that window — the losing daemon returns from
    `run` before anything reads it, and the one statement between entering
    `_serve` and the inbox does not. But a caller that drives a real Daemon
    directly, which is how you hold it to one event instead of a timer, got an
    AttributeError raised from inside the status write, a long way from the
    cause and saying nothing about it.
    """
    daemon = d.Daemon(profile)
    try:
        daemon.write_status()

        assert json.loads((profile.dir / "status.json").read_text())["state"] \
            == "starting"
        assert daemon.inbox.last_seq() == 0
    finally:
        daemon.inbox.close()


def test_a_daemon_that_never_serves_never_opens_the_database(profile):
    """The other half of the same property, and the reason it is not built in
    `__init__`: opening it runs the schema and leaves `inbox.db-wal` and
    `-shm` beside it, which a daemon that turns out not to hold this session
    has no business creating. Constructing one must touch nothing."""
    first = exclusive.DaemonLock(profile.dir)
    assert first.acquire()
    try:
        before = sorted(p.name for p in profile.dir.iterdir())

        d.Daemon(profile)

        assert sorted(p.name for p in profile.dir.iterdir()) == before
    finally:
        first.release()
