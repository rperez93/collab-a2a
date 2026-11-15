"""`collab kill` stops a hub that never got to record its own pid
(T_cf1f2c12b363): `collab host` records the child's pid and start stamp the
moment it spawns it, so there is no window in which hub.json says 0."""

from __future__ import annotations

import os
import sys
import time

from collab.server.session import HubConfig, create_session, spawn_hub, stop_session


def test_a_hub_that_never_records_its_pid_is_still_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    cfg.save()
    # The slow path: a stand-in that lives on and never writes hub.json itself.
    pid = spawn_hub(cfg, dict(os.environ), cfg.dir / "hub.log", [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        recorded = HubConfig.load(cfg.session_id, cfg.home)
        assert recorded.pid == pid and recorded.hub_stamp().pid == pid
        assert recorded.hub_stamp().alive()
        result = stop_session(recorded)             # what `collab kill` does
        assert result["hub_stopped"]
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if open(f"/proc/{pid}/stat").read().split()[2] == "Z":
                break  # exited; only waiting to be reaped
            time.sleep(0.1)
        else:
            raise AssertionError("the hub is still running after stop_session")
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_recording_the_pid_leaves_every_other_field_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    cfg.public_url = "https://example.test/s/x"
    cfg.save()
    pid = spawn_hub(cfg, dict(os.environ), cfg.dir / "hub.log", [sys.executable, "-c", "pass"])
    after = HubConfig.load(cfg.session_id, cfg.home)
    assert after.pid == pid and after.public_url == "https://example.test/s/x"


def test_a_pid_whose_start_time_cannot_be_read_is_recorded_as_0(tmp_path, monkeypatch):
    """Lead gate LOW: a bare number is trusted as-is, and once reused it names
    a stranger; without a start time the hub's pid is not recorded at all."""
    from collab.client import exclusive
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    cfg.save()
    monkeypatch.setattr(exclusive, "stamp_for", lambda pid=None: exclusive.Stamp(pid=pid or 0))
    spawn_hub(cfg, dict(os.environ), cfg.dir / "hub.log", [sys.executable, "-c", "pass"])
    after = HubConfig.load(cfg.session_id, cfg.home)
    assert (after.pid, after.pid_stamp) == (0, "")


def test_the_daemon_revives_a_dead_hub_with_its_pid_on_record_at_once(tmp_path, monkeypatch):
    """Lead gate (5829): a revived hub goes through spawn_hub, so hub.json names
    it immediately, not the dead hub until the new one first saves."""
    from types import SimpleNamespace
    from collab.client import daemon as daemon_mod
    from collab.server import session as session_mod
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    dead = __import__("subprocess").Popen([sys.executable, "-c", "pass"]); dead.wait()
    cfg.pid = dead.pid
    cfg.save()
    real = session_mod.spawn_hub
    monkeypatch.setattr(session_mod, "spawn_hub", lambda c, env, log, command=None, **kw:
                        real(c, env, log, [sys.executable, "-c", "import time; time.sleep(30)"], **kw))
    fake = SimpleNamespace(profile=SimpleNamespace(is_host=True, session_id=cfg.session_id, home=cfg.home),
                           paths=SimpleNamespace(root=cfg.dir))
    daemon_mod.Daemon._revive_hub_if_host(fake)
    revived = HubConfig.load(cfg.session_id, cfg.home)
    try:
        assert revived.pid not in (0, dead.pid) and revived.hub_stamp().alive()
    finally:
        stop_session(revived)

