"""The record of who is using a repo's collab state.

Occupancy used to be inferred by scanning pid files — correct, but invisible.
An agent could not see that another agent was in a session here, nor who, nor
in which state directory, so it went ahead and collided.

The classic failure of a lock file is outliving its process, so nothing here
trusts the file alone: it carries the pids behind it and counts as held only
while one of them is alive.
"""

from __future__ import annotations

import json
import os

import pytest

from collab import lockfile


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / ".collab"
    h.mkdir()
    monkeypatch.setenv("COLLAB_HOME", str(h))
    return h


def _lock(**kw):
    fields = dict(name="alice", session_id="s_1", role="host",
                  url="http://127.0.0.1:9000", hub_pid=os.getpid())
    fields.update(kw)
    return lockfile.Lock(**fields)


# --- writing -----------------------------------------------------------------

def test_it_records_who_and_where(home):
    lockfile.acquire(_lock(state_dir="/repo/.collab-bob"))
    data = json.loads((home / "agent.lock").read_text())

    assert data["name"] == "alice"
    assert data["session_id"] == "s_1"
    assert data["state_dir"] == "/repo/.collab-bob"


def test_it_is_private_and_written_whole(home):
    path = lockfile.acquire(_lock())
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert not list(home.glob("*.tmp")), "no half-written file left behind"


def test_taking_it_twice_leaves_one_lock(home):
    lockfile.acquire(_lock())
    first = json.loads((home / "agent.lock").read_text())["created_at"]
    lockfile.acquire(_lock())

    assert len(list(home.glob("*.lock"))) == 1
    assert json.loads((home / "agent.lock").read_text())["updated_at"] >= first


# --- what makes it real ------------------------------------------------------

def test_a_lock_with_a_live_process_is_held():
    lockfile.acquire(_lock(hub_pid=os.getpid()))
    held = lockfile.holder()
    assert held is not None and held.name == "alice"


def test_a_lock_whose_processes_are_gone_is_not(monkeypatch):
    lockfile.acquire(_lock(hub_pid=999999, listener_pid=999998))
    monkeypatch.setattr(os, "kill", _gone)
    assert lockfile.holder() is None


def test_reading_a_stale_lock_clears_it(home, monkeypatch):
    """Otherwise every future agent has to be told to clean up by hand."""
    lockfile.acquire(_lock(hub_pid=999999))
    monkeypatch.setattr(os, "kill", _gone)

    lockfile.holder()
    assert not (home / "agent.lock").exists()


def test_a_host_still_holds_it_when_only_the_listener_stopped(monkeypatch):
    """The hub is what makes the session joinable; the listener is not."""
    live, dead = os.getpid(), 999999

    def selective(pid, sig):
        if pid == dead:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", selective)
    lockfile.acquire(_lock(hub_pid=live, listener_pid=dead))
    assert lockfile.holder() is not None


def test_a_guest_holds_it_with_only_a_listener():
    lockfile.acquire(_lock(role="guest", hub_pid=0, listener_pid=os.getpid()))
    assert lockfile.holder() is not None


# --- letting go --------------------------------------------------------------

def test_releasing_removes_it(home):
    lockfile.acquire(_lock())
    assert lockfile.release()
    assert not (home / "agent.lock").exists()


def test_releasing_a_lock_that_is_not_there_is_not_an_error():
    assert lockfile.release() is False, "nothing to do, and nothing to raise"


def test_refreshing_updates_the_pid_without_retaking_it():
    """A listener restarts more often than a session does."""
    lockfile.acquire(_lock(listener_pid=111))
    lockfile.refresh(listener_pid=222)

    held = lockfile.read()
    assert held.listener_pid == 222
    assert held.name == "alice", "everything else survives"


def test_our_own_session_is_not_somebody_else():
    lock = _lock(session_id="s_mine")
    assert lockfile.is_ours(lock, "s_mine")
    assert not lockfile.is_ours(lock, "s_other")


# --- damaged files -----------------------------------------------------------

def test_an_unreadable_lock_reads_as_no_lock(home):
    (home / "agent.lock").write_text("{ this is not json")
    assert lockfile.read() is None
    assert lockfile.holder() is None


def test_a_lock_from_a_newer_collab_is_read_not_rejected(home):
    """Unknown fields are someone else's business, not a parse failure."""
    (home / "agent.lock").write_text(json.dumps({
        "name": "alice", "session_id": "s_1", "hub_pid": os.getpid(),
        "something_we_do_not_know_about": True,
    }))
    held = lockfile.holder()
    assert held is not None and held.name == "alice"


def test_it_describes_itself_for_a_person():
    lock = _lock(state_dir="/repo/.collab-bob")
    text = lock.describe()
    assert "alice" in text and "s_1" in text and ".collab-bob" in text


def _gone(pid, sig):
    raise ProcessLookupError


def _a_lock(**kw):
    return lockfile.Lock(name="a", session_id="s", role="host", **kw)


def test_two_writers_of_the_lock_do_not_fight_over_one_temp_file(tmp_path):
    """`agent.lock` has two writers by design — `_take_lock` on every host and
    join, and the daemon's heartbeat through `refresh`, in another process —
    and both wrote `agent.tmp`.

    The record is small enough that it never came back torn. What happened
    instead is that `replace` raised, because the other writer had already
    renamed the shared file away: two processes writing until done, 26.8% of
    `acquire` calls and 22.1% of the heartbeat's. `_refresh_lock` catches
    OSError so the daemon half was invisible; `_take_lock` does not, so the
    other half left `collab host` exiting with a traceback naming a temp file.
    """
    lockfile.acquire(_a_lock(hub_pid=1), tmp_path)
    path = lockfile.lock_path(tmp_path)

    # The other writer's temp file, sitting in the directory mid-write.
    theirs = path.with_suffix(".tmp")
    theirs.write_text("half a document")

    lockfile.acquire(_a_lock(hub_pid=2), tmp_path)
    assert json.loads(path.read_text())["hub_pid"] == 2, "the write did not land"
    assert theirs.read_text() == "half a document", "it wrote over the other writer"


def test_a_lock_write_that_fails_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """A unique name leaves a NEW file on every failure where the shared one
    left the same file over and over, so the tidy-up is what makes uniqueness
    affordable. `acquire` had no cleanup at all.
    """
    lockfile.acquire(_a_lock(hub_pid=1), tmp_path)
    path = lockfile.lock_path(tmp_path)

    monkeypatch.setattr(lockfile.Path, "replace",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no")))
    for _ in range(5):
        with pytest.raises(OSError):
            lockfile.acquire(_a_lock(hub_pid=2), tmp_path)

    left = sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp"))
    assert left == [], f"temp files accumulated: {left}"
