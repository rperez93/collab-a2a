"""Which platforms the daemon's identity mechanism actually works on.

Two shapes are exercised here, neither of them this machine's. Both are
SIMULATED — `fcntl` patched away for the Windows shape, `_HAVE_PROC` patched
False for the macOS one — which walks the branches those platforms would walk
on a kernel that is neither of them. Nothing here is a measurement on a Mac or
on Windows, and nothing here should be read as one.

The Windows shape is the one that used to be silent: with no `fcntl` both
daemons for a session acquired, both recorded `enforced` False, and nobody was
told. That is the fault these tests exist to keep fixed.

The macOS shape is a set of degradations, and the point of pinning them is
that each one degrades in the direction that withholds a signal rather than
sends one. An orphan that leaks is recoverable; an orphan reaped by mistake
was somebody's live daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import subprocess
import sys
import time

import pytest

from collab import cli
from collab.client import daemon as d
from collab.client import exclusive, onboard

HAVE_PROC = os.path.isdir("/proc/self")

#: Sleeps under a command line that names the daemon module and a session, so
#: that `argv` has something true to find whichever way it reads it.
_SLEEPER = "import time; time.sleep(30)"


@pytest.fixture()
def no_fcntl(monkeypatch):
    """The Windows shape: no locking primitive at all."""
    monkeypatch.setattr(exclusive, "fcntl", None)


@pytest.fixture()
def no_proc(monkeypatch):
    """The macOS shape: a working `flock` and nothing to read in /proc."""
    monkeypatch.setattr(exclusive, "_HAVE_PROC", False)


@pytest.fixture()
def sleeper(profile):
    """A live process whose argv names this session's daemon, in this home."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _SLEEPER, d.DAEMON_MODULE, profile.session_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "COLLAB_HOME": profile.home})
    try:
        yield proc
    finally:
        # Only this process, started here. Nothing in these tests goes looking
        # for a pid by name.
        with contextlib.suppress(OSError, ProcessLookupError):
            proc.kill()
        proc.wait(timeout=10)


def _record(profile, pid, began=""):
    (profile.dir / "daemon.pid").write_text(f"{pid}\n{began}\n")


# --- the Windows shape: refuse, and say why -------------------------------


def test_no_locking_primitive_refuses_the_lock(profile, no_fcntl):
    with pytest.raises(exclusive.UnsupportedPlatform):
        exclusive.DaemonLock(profile.dir).acquire()


def test_the_refusal_leaves_no_lock_file_behind(profile, no_fcntl):
    """A daemon that is not going to run must leave nothing that says it did."""
    with pytest.raises(exclusive.UnsupportedPlatform):
        exclusive.DaemonLock(profile.dir).acquire()
    assert not exclusive.lock_path(profile.dir).exists()


def test_the_refusal_names_the_version_to_use():
    """WSL 1 is not the answer, so «WSL» on its own is not the message."""
    assert "WSL 2 or later" in exclusive.UNSUPPORTED_PLATFORM


def test_a_filesystem_that_will_not_lock_still_starts(profile, monkeypatch):
    """The other half of the same judgement, and it must NOT have moved.

    An unusual mount is not a reason to refuse somebody a session. Only a
    platform with no primitive at all is.
    """
    monkeypatch.setattr(exclusive.DaemonLock, "_flock", lambda self, fd: None)
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire() is True
    assert lock.enforced is False
    lock.release()


def test_an_unwritable_state_directory_still_starts(profile, monkeypatch):
    """The third case, and it is deliberate too.

    A lock file that cannot even be opened is not a platform without locking;
    it is one directory that will not take one, and the session should still
    run. Untested anywhere until now, which matters more since the refusal
    above was put into the same function and in front of this.
    """
    def unwritable(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(exclusive.os, "open", unwritable)
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire() is True
    assert lock.enforced is False
    assert lock.held is False


def test_no_locking_beats_an_unwritable_directory(profile, no_fcntl, monkeypatch):
    """Which of the two answers first, when both are true at once.

    The platform is the wider fact, so it is checked before the file is
    touched: «run this under WSL 2» is the useful thing to say, and «your
    state directory is read-only» would send somebody after the wrong fault.
    """
    def unwritable(*a, **kw):
        raise AssertionError("opened a lock file on a platform that cannot lock")

    monkeypatch.setattr(exclusive.os, "open", unwritable)
    with pytest.raises(exclusive.UnsupportedPlatform):
        exclusive.DaemonLock(profile.dir).acquire()


def test_the_daemon_does_not_come_up_without_a_lock(profile, no_fcntl, caplog):
    """It stops, it leaves nothing behind, and it says why into daemon.log.

    The log is not where anybody is looking — the CLI refuses first, in front
    of the person — but a daemon started by hand has nowhere else to say it.
    """
    daemon = d.Daemon(profile)

    async def refuse():
        raise AssertionError("served a session it could not hold")

    daemon._serve = refuse
    with caplog.at_level("ERROR"):
        asyncio.run(daemon.run())
    assert not (profile.dir / "daemon.pid").exists()
    assert "WSL 2 or later" in caplog.text


def test_spawn_refuses_before_starting_anything(profile, no_fcntl, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("spawned a daemon that would refuse on arrival")

    monkeypatch.setattr(onboard.subprocess, "Popen", never)
    with pytest.raises(exclusive.UnsupportedPlatform):
        onboard.spawn_daemon(profile)


def test_the_cli_says_it_once_and_exits(profile, no_fcntl, monkeypatch):
    """The one place that knows it is talking to a person is the one that says it."""
    monkeypatch.setattr(cli.SessionProfile, "current",
                        classmethod(lambda c: profile))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = cli.main(["daemon", "start"])
    assert code == 1
    assert "WSL 2 or later" in err.getvalue()
    assert err.getvalue().count("WSL 2") == 1


# --- the macOS shape: degrade, never raise --------------------------------


def test_started_at_still_answers_without_proc(no_proc):
    assert exclusive.started_at(os.getpid())


def test_started_at_is_empty_for_a_pid_that_is_gone(no_proc):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    # A reaped pid has no `ps` entry either, which is the answer we want.
    assert exclusive.started_at(proc.pid) == ""


def test_a_process_is_still_itself_without_proc(no_proc):
    began = exclusive.started_at(os.getpid())
    # Asserted first, and not for tidiness: `same_process` TRUSTS an empty
    # record, so without this the check below passes on a start time that was
    # never read and proves nothing about `ps`.
    assert began
    assert exclusive.same_process(began, os.getpid())


def test_the_stamp_is_written_without_proc(no_proc):
    pid, began = exclusive.parse(exclusive.stamp())
    assert pid == os.getpid() and began


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc for the contrast")
def test_a_zombie_is_invisible_without_proc(monkeypatch):
    """Stated, not hidden: an unreaped daemon reads as live on macOS.

    /proc is the only place the Z is written down, and `kill(pid, 0)` answers
    yes for a zombie, so every liveness test here says the daemon is running.
    The lock is what saves the case there: the kernel took it back the moment
    the process exited, whatever is left in the process table.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not exclusive.is_zombie(proc.pid):
            time.sleep(0.02)
        assert exclusive.is_zombie(proc.pid) is True
        monkeypatch.setattr(exclusive, "_HAVE_PROC", False)
        assert exclusive.is_zombie(proc.pid) is False
    finally:
        proc.wait(timeout=10)


def test_no_environment_can_be_read_without_proc(no_proc, sleeper):
    """And none is invented. `ps` does not offer one and nor does this."""
    assert exclusive.environ(sleeper.pid) == {}


def test_argv_survives_without_proc_through_ps(no_proc, sleeper, profile):
    words = exclusive.argv(sleeper.pid)
    assert d.DAEMON_MODULE in words
    assert profile.session_id in words


def test_argv_of_a_dead_pid_is_empty_without_proc(no_proc):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert exclusive.argv(proc.pid) == []


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc for the contrast")
def test_the_home_condition_can_be_met_here_and_not_without_proc(
        profile, sleeper, monkeypatch):
    """The whole macOS property, in one pair.

    With /proc the process is identified by argv AND by the home it was
    exec'd with. Without /proc the argv half still matches — that is what the
    `ps` fallback is for — and the home cannot be read at all, so this
    declines. THE HOME IS NOT OPTIONAL: two checkouts may share a session id,
    and the argv alone would reap a sibling repo's live daemon.
    """
    assert d._names_itself_our_daemon(sleeper.pid, profile) is True
    monkeypatch.setattr(exclusive, "_HAVE_PROC", False)
    assert d._names_itself_our_daemon(sleeper.pid, profile) is False


def test_an_orphan_leaks_rather_than_being_signalled_without_proc(
        no_proc, profile, sleeper):
    """A pre-lock orphan on macOS is left alone, and that is the safe end."""
    _record(profile, sleeper.pid)           # bare pid: the pre-lock shape
    assert d.provably_ours(profile) is None
    assert d.stop_orphans(profile.home) == []


def test_the_lock_still_identifies_a_daemon_without_proc(no_proc, profile):
    """Withholding must not have become the only answer.

    `flock` is POSIX and is the arm that survives the loss of /proc intact, so
    a daemon holding the lock is still provably this session's.
    """
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    try:
        _record(profile, os.getpid())
        assert exclusive.taken(profile.dir) is True
        assert d.provably_ours(profile) == os.getpid()
        assert d.is_running(profile) == os.getpid()
    finally:
        lock.release()


def test_a_dead_session_reads_as_dead_without_proc(no_proc, profile):
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    lock.release()
    _record(profile, os.getpid())
    assert exclusive.taken(profile.dir) is False
    assert d.is_running(profile) is None


def test_a_dead_watcher_costs_no_start_time_read(profile, monkeypatch):
    """The subprocess a machine with no /proc would have spent on a dead pid.

    `watchers` runs every STATUS_HEARTBEAT seconds, and where the start time
    comes from `ps` rather than /proc it is a process spawn each time. A pid
    that is already gone is settled by `kill(pid, 0)` alone, so it must never
    reach the start time at all.
    """
    directory = d.watchers_dir(profile)
    directory.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    (directory / str(proc.pid)).write_text("a start time from whenever")

    asked: list[int] = []
    # Patched where `watchers` looks it up. The daemon module re-exports both
    # names, so patching them there would leave the real one in use here and
    # this assertion passing whatever the code did.
    from collab.client import daemon_files
    monkeypatch.setattr(daemon_files, "_started_at", lambda pid: asked.append(pid) or "")
    assert d.watchers(profile) == []
    assert asked == [], "asked a dead pid when it had already been ruled out"
    assert not (directory / str(proc.pid)).exists(), "the stale file stayed"


def test_a_live_watcher_is_still_matched_against_its_stamp(profile):
    """And the saving must not have cost the reuse check it sits in front of."""
    directory = d.watchers_dir(profile)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / str(os.getpid())).write_text("not the start time of this one")
    assert d.watchers(profile) == []


def test_stop_reports_nothing_to_stop_without_proc(no_proc, profile):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)                   # reaped: the pid names nobody
    _record(profile, proc.pid)
    assert d.stop(profile) is False


def test_a_pid_file_of_zero_names_nobody(profile):
    """`kill(0, sig)` signals the caller's whole process group.

    Not a hypothetical: a test here wrote 0 into `daemon.pid`, `stop` read it
    back, and the SIGTERM took down the pytest process that was running it.
    `kill(-1, sig)` is the same mistake with a wider blast radius.
    """
    assert exclusive.parse("0\n") == (None, "")
    assert exclusive.parse("-1\n") == (None, "")
    _record(profile, 0)
    assert d.is_running(profile) is None
    assert d.provably_ours(profile) is None
    assert d.stop(profile) is False
