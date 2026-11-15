"""One hub per session, enforced by the hub (T_60789ecd1e22, lead seq 5829):
a second hub on a session exits at once and leaves the first one's record."""

from __future__ import annotations

import subprocess
import sys

from collab.client import exclusive
from collab.client.exclusive import Stamp, stamp_for
from collab.server.session import HubConfig, create_session


def _session(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    cfg = create_session("alice", 9000)
    cfg.save()
    return cfg


def test_a_second_hub_on_a_session_exits_and_leaves_the_first_ones_record(tmp_path, monkeypatch):
    """A real second hub process, with a deadline: if it does not exit at once
    (it started serving instead), the test fails rather than hangs."""
    import os
    cfg = _session(tmp_path, monkeypatch)
    cfg.pid = 4242
    cfg.save()
    first = exclusive.DaemonLock(cfg.dir, name=exclusive.HUB_LOCK_FILE)
    assert first.acquire()                       # the running hub's lock
    second = subprocess.Popen([sys.executable, "-m", "collab.hub_main", cfg.session_id],
                              env={**os.environ, "COLLAB_HOME": cfg.home},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = second.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        second.kill()
        second.communicate()
        raise AssertionError("a second hub kept running on a session that already has one")
    assert second.returncode == 0
    assert "another hub already runs session" in out and "4242" in out, out
    assert HubConfig.load(cfg.session_id, cfg.home).pid == 4242, "the first hub's pid is untouched"


def test_the_hub_lock_does_not_collide_with_the_daemons(tmp_path):
    hub = exclusive.DaemonLock(tmp_path, name=exclusive.HUB_LOCK_FILE)
    daemon = exclusive.DaemonLock(tmp_path)
    assert hub.acquire() and daemon.acquire()
    assert hub.path != daemon.path


def test_a_reused_pid_does_not_stop_the_daemon_reviving_a_dead_hub(tmp_path, monkeypatch):
    """The revive check is by stamp: a live process that merely has the dead
    hub's number (a different start time) is not the hub."""
    from types import SimpleNamespace
    from collab.client import daemon as daemon_mod
    from collab.server import session as session_mod
    cfg = _session(tmp_path, monkeypatch)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real = stamp_for(stranger.pid)
        cfg.pid = stranger.pid
        cfg.pid_stamp = Stamp(pid=stranger.pid, started=(real.started or "") + "-not-this-one",
                              boot=real.boot).encode()
        cfg.save()
        spawned = []
        monkeypatch.setattr(session_mod, "spawn_hub", lambda *a, **k: spawned.append(a) or 0)
        fake = SimpleNamespace(profile=SimpleNamespace(is_host=True, session_id=cfg.session_id, home=cfg.home),
                               paths=SimpleNamespace(root=cfg.dir))
        daemon_mod.Daemon._revive_hub_if_host(fake)
        assert len(spawned) == 1, "the dead hub is revived even though its number is in use"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_losing_hub_spawned_for_real_never_takes_over_the_record(tmp_path, monkeypatch):
    """Lead gate on #205: the real spawn_hub against a held hub.lock. The new
    hub loses and exits; the record keeps naming the holder; collab kill's
    stop_session still stops the holder."""
    import os
    import time
    from collab.server.session import spawn_hub, stop_session
    cfg = _session(tmp_path, monkeypatch)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; from collab.client import exclusive; "
        f"lock = exclusive.DaemonLock({str(cfg.dir)!r}, name=exclusive.HUB_LOCK_FILE); "
        "assert lock.acquire(); print('HELD', flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True, env={**os.environ})
    try:
        assert holder.stdout.readline().strip() == "HELD"
        cfg.pid, cfg.pid_stamp = holder.pid, stamp_for(holder.pid).encode()
        cfg.save()
        loser = spawn_hub(cfg, {**os.environ, "COLLAB_HOME": cfg.home}, cfg.dir / "hub.log")
        for _ in range(200):
            try:
                os.kill(loser, 0)
                if open(f"/proc/{loser}/stat").read().split()[2] == "Z":
                    break
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the losing hub is still running")
        record = HubConfig.load(cfg.session_id, cfg.home)
        assert record.pid == holder.pid, "the record still names the hub that holds the lock"
        assert stop_session(record)["hub_stopped"]
        holder.wait(10)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
