"""«Is a daemon running» and «may I kill this» are not the same question.

`stop_orphans` runs from `collab host` and `collab join` with no flag and no
prompt, escalates to SIGKILL, and reports what it did afterwards. It reaped
whatever `is_running` believed — and `is_running` believes a pid file with no
start time, because it must: such a file was written by a collab from before
the lock existed, and calling it an impostor would make an upgrade look like a
crash and start a second daemon on top of a working one.

That is the right answer to «should I start one» and no answer at all to «may I
signal it». Nor does it age out: a directory that starts a daemon again gains a
lock and is safe from that moment, but the directories this reaps are the ones
where a daemon will never start again — that is what makes them orphans, and
their bare pid files stay bare for ever.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from collab.client import daemon as d
from collab.client import exclusive
from collab.config import SessionProfile

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


def _signals(monkeypatch):
    """Record the real signals, letting the liveness probes through."""
    sent: list[tuple[int, int]] = []
    real = os.kill
    monkeypatch.setattr(
        os, "kill",
        lambda pid, sig: sent.append((pid, sig)) if sig else real(pid, sig))
    return sent


def test_a_bare_pid_file_is_not_permission_to_signal(profile, monkeypatch):
    """The shape every pre-lock collab wrote, naming a live stranger."""
    (profile.dir / "daemon.pid").write_text(f"{os.getpid()}\n")

    assert d.is_running(profile) == os.getpid(), \
        "and it must still be believed for the purpose of not starting a second"
    assert d.provably_ours(profile) is None

    sent = _signals(monkeypatch)
    assert d.stop_orphans(profile.home, keep="other") == []
    assert sent == [], "an unidentified process was signalled"


def test_a_start_time_that_matches_is_permission(profile, monkeypatch):
    """An honest record identifies its process, so this one may be stopped."""
    (profile.dir / "daemon.pid").write_text(exclusive.stamp())

    assert d.provably_ours(profile) == os.getpid()

    sent = _signals(monkeypatch)
    d.stop_orphans(profile.home, keep="other")
    assert (os.getpid(), 15) in sent


def test_a_held_lock_is_permission(profile, monkeypatch):
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    try:
        (profile.dir / "daemon.pid").write_text(f"{os.getpid()}\n")
        assert d.provably_ours(profile) == os.getpid()
    finally:
        lock.release()


def test_a_start_time_that_does_not_match_is_not(profile):
    """The reuse case: alive, and not the process the record was about."""
    (profile.dir / "daemon.pid").write_text(f"{os.getpid()}\n1\n")

    assert d.provably_ours(profile) is None


def _stands_in_for_a_daemon(session_id, home):
    """A process presenting what `spawn_daemon` presents: the module and the
    session id in its argv, COLLAB_HOME pinned in its environment."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "collab.daemon_main", session_id],
        env={**os.environ, "COLLAB_HOME": str(home)})


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc/<pid>/cmdline")
def test_a_process_that_names_itself_our_daemon_may_still_be_reaped(profile):
    """So a genuine pre-lock orphan is not merely spared — it is still cleared.

    All three of the things `spawn_daemon` sets have to be there, which is what
    the stand-in presents. A stranger that inherited the number has none.
    """
    child = _stands_in_for_a_daemon(profile.session_id, profile.home)
    try:
        (profile.dir / "daemon.pid").write_text(f"{child.pid}\n")

        assert d.provably_ours(profile) == child.pid
        assert d.stop_orphans(profile.home, keep="other") == ["s"]
        assert not d._alive(child.pid)
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc/<pid>/environ")
def test_another_checkouts_daemon_for_the_same_session_is_not_ours(
        tmp_path, monkeypatch):
    """Two homes may legitimately share a session id, and two do here today.

    A host and a guest in different working copies is the arrangement `peers`
    exists to support. On the argv alone the sibling's LIVE daemon matched by
    construction, every time — not by unlucky reuse — so `stop_orphans` in one
    checkout reaped the listener in the other. The scan never leaves this home;
    the check it trusted did not know which home it was looking at.

    Sharpest form of it: without this arm that daemon is unidentifiable and
    nothing is signalled. The arm was the only thing granting permission, so
    it made collab's own daemons the preferred casualty of a test meant to
    spare everyone else's.
    """
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    theirs = tmp_path / "their-checkout" / ".collab"
    ours = tmp_path / "our-checkout" / ".collab"
    (ours / "sessions" / "s_shared").mkdir(parents=True)
    mine = SessionProfile(session_id="s_shared", url="http://h/", name="us",
                          host_name="them", token="t", home=str(ours))
    mine.save()

    child = _stands_in_for_a_daemon("s_shared", theirs)
    try:
        (mine.dir / "daemon.pid").write_text(f"{child.pid}\n")

        assert d.provably_ours(mine) is None
        assert d.stop_orphans(mine.home, keep="other") == []
        assert d._alive(child.pid), "another checkout's listener was killed"
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc/<pid>/cmdline")
def test_naming_another_sessions_daemon_is_not_permission(profile):
    """Both halves are read, not just the module: one repo's `join` must not
    reap another repo's listener because both are collab daemons."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              "collab.daemon_main", "s_somebody_else"])
    try:
        (profile.dir / "daemon.pid").write_text(f"{child.pid}\n")

        assert d.provably_ours(profile) is None
    finally:
        child.kill()
        child.wait()


def test_stopping_by_name_still_works_on_a_pre_lock_daemon(profile, tmp_path):
    """The recovery this leaves the user must actually be there.

    `stop_orphans` declines to reap what it cannot identify, so `collab daemon
    stop` — a person naming a session — has to keep working on exactly those,
    which is why `stop` asks `is_running` and not `provably_ours`.
    """
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(30)",
         str(ready)])
    try:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        (profile.dir / "daemon.pid").write_text(f"{child.pid}\n")

        assert d.stop(profile) is True
        assert not d._alive(child.pid)
    finally:
        child.kill()
        child.wait()
