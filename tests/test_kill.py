"""Ending a session. Stopping is not losing; purging is.

Processes are ended by the pid each one recorded, never by matching command
lines — a pattern like "collab.hub_main" also matches the shell you typed it
in, which is a good way to kill your own terminal.
"""

from __future__ import annotations

import os

import pytest

from collab.server.session import create_session, hosted_sessions, stop_session
from collab.server.store import Store


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    return tmp_path


def _with_history(cfg):
    from collab.protocol import Envelope

    store = Store(cfg.db_path)
    try:
        store.append(Envelope(kind="chat", text="hello", sender="alice",
                              room="general"))
    finally:
        store.close()
    return cfg


def test_stopping_keeps_the_data():
    cfg = _with_history(create_session("alice", 9000))
    stop_session(cfg)

    assert cfg.db_path.exists(), "stopping is not losing"
    assert [c.session_id for c in hosted_sessions()] == [cfg.session_id]


def test_purging_removes_it():
    cfg = _with_history(create_session("alice", 9000))
    result = stop_session(cfg, purge=True)

    assert result["purged"] is True
    assert not cfg.dir.exists()
    assert hosted_sessions() == []


def _only_signals(monkeypatch) -> list[int]:
    """Record the pids actually signalled, and none of the pids merely asked about.

    `stop_session` now puts every recorded pid to `Stamp.alive` before reaching
    for it — a number from a previous boot names nobody and must not be
    signalled — and asking is `os.kill(pid, 0)`. A recorder that counted those
    would report the question as the act.
    """
    sent: list[int] = []
    monkeypatch.setattr(os, "kill",
                        lambda pid, sig: sent.append(pid) if sig else None)
    return sent


def test_it_signals_the_recorded_pids(monkeypatch):
    """By pid, never by command-line pattern."""
    cfg = create_session("alice", 9000)
    cfg.pid = 4242
    cfg.save()
    (cfg.dir / "daemon.pid").write_text("4243")

    signalled = _only_signals(monkeypatch)

    result = stop_session(cfg)
    assert signalled == [4242, 4243]
    assert result["hub_stopped"] and result["daemon_stopped"]


def test_a_process_that_is_already_gone_is_not_an_error(monkeypatch):
    cfg = create_session("alice", 9000)
    cfg.pid = 999999
    cfg.save()

    def gone(pid, sig):
        raise ProcessLookupError

    # Raised for the liveness question as well as for the signal, which is what
    # a process that is genuinely gone does: `process_alive` reads ESRCH there
    # and answers no, so `stop_session` never reaches for it at all.
    monkeypatch.setattr(os, "kill", gone)
    result = stop_session(cfg)
    assert result["hub_stopped"] is False


def test_stopping_a_session_with_no_pids_recorded():
    cfg = create_session("alice", 9000)
    result = stop_session(cfg)
    assert result["hub_stopped"] is False
    assert result["daemon_stopped"] is False
    assert cfg.db_path.exists()


def test_stopping_takes_the_tunnel_with_it(monkeypatch):
    """A leaked agent leaves a public URL pointing at a dead port.

    On a free plan it also occupies the one slot the next session needs, so
    the next `collab host` silently gets no tunnel at all.
    """
    cfg = create_session("alice", 9000)
    cfg.pid = 4242
    cfg.tunnel_pid = 4244
    cfg.save()
    (cfg.dir / "daemon.pid").write_text("4243")

    signalled = _only_signals(monkeypatch)

    result = stop_session(cfg)
    assert signalled == [4242, 4243, 4244]
    assert result["tunnel_stopped"] is True


def test_a_tunnel_we_did_not_start_is_left_alone(monkeypatch):
    """Reusing someone else's agent does not make it ours to stop."""
    cfg = create_session("alice", 9000)
    cfg.pid = 4242
    cfg.tunnel_pid = 0          # we reused one rather than launching it
    cfg.save()

    signalled = _only_signals(monkeypatch)

    result = stop_session(cfg)
    assert signalled == [4242]
    assert result["tunnel_stopped"] is False


def test_the_supervisor_reports_only_its_own_agent():
    from collab.server.tunnel import Tunnel, TunnelSupervisor

    class Proc:
        pid = 999

    sup = TunnelSupervisor(9000)
    assert sup.own_pid() == 0, "nothing started yet"

    sup.tunnel = Tunnel(public_url="https://x", process=None)
    assert sup.own_pid() == 0, "a reused agent is not ours"

    sup.tunnel = Tunnel(public_url="https://x", process=Proc())
    assert sup.own_pid() == 999
