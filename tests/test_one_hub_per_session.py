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
    # The log names the hub that HOLDS the lock (this test process), not
    # whatever hub.json says.
    assert "another hub already runs session" in out and f"pid {os.getpid()}" in out, out
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
        "assert lock.acquire(); lock.note(exclusive.stamp_for().encode()); "
        "print('HELD', flush=True); time.sleep(60)")],
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
            except (FileNotFoundError, ProcessLookupError):
                break  # reaped between the signal and the read
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


def test_stop_session_stops_the_lock_holder_even_when_the_record_names_the_loser(tmp_path, monkeypatch):
    """Two hubs spawned at once can each record themselves before either holds
    hub.lock, so hub.json may name the one that lost. collab kill also stops
    whoever holds the lock now."""
    import os
    from collab.server.session import stop_session
    cfg = _session(tmp_path, monkeypatch)
    loser = subprocess.Popen([sys.executable, "-c", "pass"]); loser.wait()
    cfg.pid, cfg.pid_stamp = loser.pid, ""        # the record names the hub that exited
    cfg.save()
    holder = subprocess.Popen([sys.executable, "-c", (
        "import time; from collab.client import exclusive; "
        f"lock = exclusive.DaemonLock({str(cfg.dir)!r}, name=exclusive.HUB_LOCK_FILE); "
        "assert lock.acquire(); lock.note(exclusive.stamp_for().encode()); "
        "print('HELD', flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True, env={**os.environ})
    try:
        assert holder.stdout.readline().strip() == "HELD"
        from collab.server.session import lock_holder
        assert lock_holder(cfg).pid == holder.pid
        assert stop_session(HubConfig.load(cfg.session_id, cfg.home))["hub_stopped"]
        assert holder.wait(10) is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    from collab.server.session import lock_holder
    assert lock_holder(cfg) is None, "a released lock names nobody"


def test_the_daemon_does_not_revive_while_another_hub_holds_the_lock(tmp_path, monkeypatch):
    """The record names a dead hub, but a live one holds hub.lock (it won a
    race and has not saved yet): reviving would start a third."""
    import os
    from types import SimpleNamespace
    from collab.client import daemon as daemon_mod
    from collab.server import session as session_mod
    cfg = _session(tmp_path, monkeypatch)
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    cfg.pid, cfg.pid_stamp = dead.pid, ""
    cfg.save()
    holder = subprocess.Popen([sys.executable, "-c", (
        "import time; from collab.client import exclusive; "
        f"lock = exclusive.DaemonLock({str(cfg.dir)!r}, name=exclusive.HUB_LOCK_FILE); "
        "assert lock.acquire(); lock.note(exclusive.stamp_for().encode()); "
        "print('HELD', flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True, env={**os.environ})
    try:
        assert holder.stdout.readline().strip() == "HELD"
        spawned = []
        monkeypatch.setattr(session_mod, "spawn_hub", lambda *a, **k: spawned.append(a) or 0)
        fake = SimpleNamespace(profile=SimpleNamespace(is_host=True, session_id=cfg.session_id, home=cfg.home),
                               paths=SimpleNamespace(root=cfg.dir))
        daemon_mod.Daemon._revive_hub_if_host(fake)
        assert spawned == []
    finally:
        holder.kill()
        holder.wait()



def test_rewriting_the_lock_body_is_never_seen_empty(tmp_path):
    """Lead gate on #205: note() truncated, then wrote, so concurrent
    lock_holder reads found an empty stamp. Rewritten in a tight loop while
    another thread reads it: no read is ever empty."""
    import threading
    lock = exclusive.DaemonLock(tmp_path, name=exclusive.HUB_LOCK_FILE)
    assert lock.acquire()
    stamp = stamp_for().encode()
    lock.note(stamp)
    done, empty, reads = threading.Event(), [], [0]

    def writer():
        while not done.is_set():
            lock.note(stamp)

    def reader():
        path = tmp_path / exclusive.HUB_LOCK_FILE
        while not done.is_set():
            reads[0] += 1
            if not path.read_text().strip():
                empty.append(1)
    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    import time
    time.sleep(1.0)
    done.set()
    for t in threads:
        t.join(5)
    assert reads[0] > 1000 and not empty, f"{len(empty)} empty reads in {reads[0]}"
