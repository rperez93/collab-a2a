"""A process you may not signal is a process that exists.

`os.kill(pid, 0)` answers two different things with two different errors:
ESRCH (`ProcessLookupError`) when there is no such process, and EPERM
(`PermissionError`) when there is one and the caller is not allowed to signal
it. Both are `OSError`, and every liveness check here caught `OSError` and read
it as «dead».

An agent running in a sandbox — Codex confines its commands so they cannot
signal anything outside the sandbox — gets EPERM for every other agent's
process on the machine. So from inside it, every peer record read as stale and
`collab discover` deleted them; every lock read as stale and `lockfile.holder`
RELEASED the other agent's live lock; `collab join` saw a free `.collab`, moved
in on top of the other agent, and then could not start a daemon because the
other agent's daemon still held the slot — «someone else is listening».
"""

from __future__ import annotations

import errno
import json
import os
import time

import pytest

from collab import cli, config, lockfile, peers
from collab.cli import main
from collab.client import daemon


def _not_ours_to_signal(pid, sig):
    raise PermissionError(errno.EPERM, "Operation not permitted")


def _gone(pid, sig):
    raise ProcessLookupError(errno.ESRCH, "No such process")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    return tmp_path


# --- the one answer, in one place -------------------------------------------

def test_a_process_we_may_not_signal_is_alive(monkeypatch):
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)
    assert lockfile.process_alive(999_999) is True


def test_a_process_that_is_not_there_is_dead(monkeypatch):
    monkeypatch.setattr(os, "kill", _gone)
    assert lockfile.process_alive(999_999) is False


def test_no_pid_is_not_a_process():
    """`kill(0, 0)` is this whole process group and always succeeds."""
    assert lockfile.process_alive(0) is False
    assert lockfile.process_alive(-1) is False


# --- the lock: it must not be released by an agent that cannot see it -------

def test_a_lock_behind_an_unsignallable_process_is_held(tmp_path, monkeypatch):
    home = tmp_path / ".collab"
    home.mkdir()
    lockfile.acquire(lockfile.Lock(name="alice", session_id="s_1",
                                   hub_pid=999_999), home)
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)

    lock = lockfile.holder(home)

    assert lock is not None and lock.held, "alive, just not ours to signal"
    assert (home / lockfile.LOCK_NAME).exists(), "and NOT released"


def test_a_lock_behind_a_dead_process_is_still_cleared(tmp_path, monkeypatch):
    home = tmp_path / ".collab"
    home.mkdir()
    lockfile.acquire(lockfile.Lock(name="alice", session_id="s_1",
                                   hub_pid=999_999), home)
    monkeypatch.setattr(os, "kill", _gone)

    assert lockfile.holder(home) is None
    assert not (home / lockfile.LOCK_NAME).exists()


# --- the registry: it must not be pruned by an agent that cannot see it -----

def _announce(**kw):
    fields = dict(session_id="s_1", name="alice", role="host",
                  url="http://127.0.0.1:5000", repo="/repo/api",
                  home="/repo/api/.collab", invite="INV", pid=999_999)
    fields.update(kw)
    return peers.announce(**fields)


def test_a_peer_we_may_not_signal_is_online(monkeypatch):
    path = _announce()
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)

    found = peers.discover()

    assert [p.name for p in found] == ["alice"]
    assert found[0].alive and found[0].joinable
    assert path.exists(), "discover prunes dead records; this one is not dead"


def test_a_peer_whose_process_is_gone_is_pruned(monkeypatch):
    path = _announce()
    monkeypatch.setattr(os, "kill", _gone)

    assert peers.discover() == []
    assert not path.exists()


# --- the daemon's own check ---------------------------------------------------

def test_the_daemon_liveness_check_agrees(monkeypatch):
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)
    assert daemon._alive(999_999) is True
    monkeypatch.setattr(os, "kill", _gone)
    assert daemon._alive(999_999) is False


# --- join: the sandboxed agent must still be sent to its own directory -------

def test_a_sandboxed_join_is_still_sent_to_its_own_directory(tmp_path, monkeypatch):
    """The bug as the user met it: from inside the sandbox the other agent's
    lock read as stale, so `join` walked into their `.collab`."""
    base = tmp_path / ".collab"
    base.mkdir()
    lockfile.acquire(lockfile.Lock(name="alice", session_id="s_1", role="host",
                                   hub_pid=999_999, state_dir=str(base)), base)
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)
    args = cli.build_parser().parse_args(["join", "--name", "bob"])

    assert cli._own_state_dir(args, "bob") is None, "carry on with the join"
    assert os.environ.get("COLLAB_HOME") == str(tmp_path / ".collab-bob")


# --- discover: say the state on every row, in words ---------------------------

def _stale_record(tmp_path, session_id="s_old", seconds_ago=240.0):
    """A record nobody has refreshed for a while; its process is beside the
    point, since staleness by age comes first."""
    _announce(session_id=session_id, name="carol", pid=os.getpid())
    for child in (tmp_path / "peers").glob(f"{session_id}-*.json"):
        data = json.loads(child.read_text())
        data["updated_at"] = time.time() - seconds_ago
        child.write_text(json.dumps(data))


def test_every_row_says_whether_it_is_online(tmp_path, capsys):
    _announce(pid=os.getpid())
    _stale_record(tmp_path)

    main(["discover", "--all"])
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if "  as " in line]

    assert len(rows) == 2
    alice = next(r for r in rows if "alice" in r)
    carol = next(r for r in rows if "carol" in r)
    assert "online" in alice, "not inferred from the absence of a word"
    assert "stale" in carol and "last seen 4m ago" in carol


def test_json_carries_the_age_and_the_status(tmp_path, capsys):
    _announce(pid=os.getpid())
    _stale_record(tmp_path, seconds_ago=300.0)

    main(["discover", "--all", "--json"])
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}

    assert rows["alice"]["alive"] is True and rows["alice"]["joinable"] is True
    assert rows["alice"]["status"] == "online"
    assert rows["alice"]["last_seen"] < 5
    assert rows["carol"]["alive"] is False and rows["carol"]["status"] == "stale"
    assert 299 <= rows["carol"]["last_seen"] <= 310


def test_from_a_sandbox_the_other_agents_session_reads_online(capsys, monkeypatch):
    """What Codex saw: every session on the machine listed as stale."""
    _announce()
    monkeypatch.setattr(os, "kill", _not_ours_to_signal)

    main(["discover"])
    out = capsys.readouterr().out

    assert "s_1" in out and "online" in out
    assert "stale" not in out
    assert "join --local s_1" in out, "and the join line is offered"
