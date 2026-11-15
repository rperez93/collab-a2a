"""T_60789ecd1e22: `collab host --resume` against a session whose hub is still
running started a second hub beside it, and hub.json named only one of them."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

from collab import cli, config
from collab.client.exclusive import stamp_for
from collab.server import session as session_mod
from collab.server.session import HubConfig, create_session


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_NO_TUNNEL", "1")
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    config._CACHE.clear()
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)


def test_a_hub_still_exiting_after_kill_does_not_block_the_resume(tmp_path, monkeypatch):
    """`collab kill`, then `collab host --resume` at once: the old hub is still
    exiting. The resume waits a bounded moment for it instead of refusing."""
    _setup(tmp_path, monkeypatch)
    cfg = create_session("alice", 9000)
    exiting = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    try:
        cfg.pid, cfg.pid_stamp = exiting.pid, stamp_for(exiting.pid).encode()
        cfg.save()
        spawned = []
        monkeypatch.setattr(session_mod, "spawn_hub", lambda *a, **k: spawned.append(a) or 0)
        # The stand-in never comes up, so host gives up waiting. Its 45 s
        # wall-clock deadline is skipped forward; the settle wait above it
        # uses the monotonic clock and stays real.
        import time as real_time
        ticks = iter(range(0, 10_000, 20))
        monkeypatch.setattr(cli, "time", type("T", (), {
            "time": staticmethod(lambda: real_time.time() + next(ticks)),
            "__getattr__": lambda self, name: getattr(real_time, name)})())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            cli.main(["host", "--resume", cfg.session_id, "--no-daemon",
                      "--no-update-check", "--no-tunnel"])
        said = out.getvalue()
        assert "already has a running hub" not in said
        assert len(spawned) == 1, "the resume went ahead once the old hub had gone"
        # And a hub that never comes up is named, not a NameError (regression
        # from #202, which dropped the variable this message reads).
        assert "the hub did not come up" in said and "hub.log" in said, said
    finally:
        exiting.kill()
        exiting.wait()


def test_resuming_a_session_whose_hub_is_alive_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_NO_TUNNEL", "1")
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    config._CACHE.clear()
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    cfg = create_session("alice", 9000)
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        cfg.pid, cfg.pid_stamp = running.pid, stamp_for(running.pid).encode()
        cfg.save()
        spawned = []
        monkeypatch.setattr(session_mod, "spawn_hub", lambda *a, **k: spawned.append(a) or 0)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = cli.main(["host", "--resume", cfg.session_id, "--no-daemon",
                             "--no-update-check", "--no-tunnel"])
        assert code == 1, out.getvalue()
        assert spawned == [], "no second hub is started"
        assert "already has a running hub" in out.getvalue()
        assert HubConfig.load(cfg.session_id, cfg.home).pid == running.pid
    finally:
        running.kill()
        running.wait()
