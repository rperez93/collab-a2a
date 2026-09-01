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


@pytest.mark.skipif(not HAVE_PROC, reason="needs /proc/<pid>/cmdline")
def test_a_process_that_names_itself_our_daemon_may_still_be_reaped(profile):
    """So a genuine pre-lock orphan is not merely spared — it is still cleared.

    The daemon is launched as `python -m collab.daemon_main <session id>`, and
    the stand-in below carries the same two words in its own argv, which is the
    whole of what is being read. A stranger that inherited the number has
    neither of them.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              "collab.daemon_main", profile.session_id])
    try:
        (profile.dir / "daemon.pid").write_text(f"{child.pid}\n")

        assert d.provably_ours(profile) == child.pid
        assert d.stop_orphans(profile.home, keep="other") == ["s"]
        assert not d._alive(child.pid)
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
