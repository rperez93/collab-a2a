"""`collab kill` stops a hub that never got to record its own pid
(T_cf1f2c12b363): `collab host` records the child's pid and start stamp the
moment it spawns it, so there is no window in which hub.json says 0."""

from __future__ import annotations

import os
import sys
import time

from collab import cli
from collab.server.session import HubConfig, create_session, stop_session


def test_a_hub_that_never_records_its_pid_is_still_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    cfg.save()
    # The slow path: a stand-in that lives on and never writes hub.json itself.
    pid = cli._spawn_hub(cfg, dict(os.environ), [sys.executable, "-c", "import time; time.sleep(60)"])
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
    pid = cli._spawn_hub(cfg, dict(os.environ), [sys.executable, "-c", "pass"])
    after = HubConfig.load(cfg.session_id, cfg.home)
    assert after.pid == pid and after.public_url == "https://example.test/s/x"
