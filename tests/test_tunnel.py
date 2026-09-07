"""Keeping the public address alive across a tunnel that ends on its own."""

from __future__ import annotations

import os
import pytest
import time

from collab.server import tunnel as t


class FakeTunnel:
    def __init__(self, url, alive=True):
        self.public_url = url
        self._alive = alive
        self.stopped = False

    def alive(self):
        return self._alive

    def stop(self):
        self.stopped = True


def test_a_healthy_tunnel_is_left_alone(monkeypatch):
    sup = t.TunnelSupervisor(9000)
    sup.tunnel = FakeTunnel("https://a.ngrok.app")
    def must_not_restart(*a, **k):
        raise AssertionError("must not restart a tunnel that is still up")

    monkeypatch.setattr(t, "start_tunnel", must_not_restart)

    url, changed = sup.ensure()
    assert url == "https://a.ngrok.app"
    assert changed is False
    assert sup.restarts == 0


def test_a_dead_tunnel_is_restarted_and_reports_the_new_address(monkeypatch):
    sup = t.TunnelSupervisor(9000)
    dead = FakeTunnel("https://old.ngrok.app", alive=False)
    sup.tunnel = dead
    monkeypatch.setattr(t, "start_tunnel",
                        lambda *a, **k: FakeTunnel("https://new.ngrok.app"))

    url, changed = sup.ensure()
    assert url == "https://new.ngrok.app"
    assert changed is True, "callers must know shared links are now dead"
    assert dead.stopped, "the old agent has to be cleaned up"
    assert sup.restarts == 1


def test_a_pinned_domain_comes_back_on_the_same_address(monkeypatch):
    """With a reserved domain nothing downstream has to change."""
    sup = t.TunnelSupervisor(9000, domain="fixed.ngrok.app")
    sup.tunnel = FakeTunnel("https://fixed.ngrok.app", alive=False)
    monkeypatch.setattr(t, "start_tunnel",
                        lambda *a, **k: FakeTunnel("https://fixed.ngrok.app"))

    url, changed = sup.ensure()
    assert url == "https://fixed.ngrok.app"
    assert changed is False, "same address means no link was invalidated"


def test_domain_is_passed_to_ngrok(monkeypatch):
    seen: dict[str, list[str]] = {}

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(t, "ngrok_path", lambda: "/usr/bin/ngrok")
    # No tunnel exists yet, so it has to spawn one.
    calls = iter([None, "https://fixed.ngrok.app"])
    monkeypatch.setattr(t, "_existing_tunnel", lambda port: next(calls, None))
    monkeypatch.setattr(t.subprocess, "Popen", fake_popen)

    tunnel = t.start_tunnel(9000, domain="fixed.ngrok.app")
    assert tunnel is not None and tunnel.public_url == "https://fixed.ngrok.app"
    assert "--domain" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--domain") + 1] == "fixed.ngrok.app"


def test_no_domain_means_no_domain_flag(monkeypatch):
    seen: dict[str, list[str]] = {}

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(t, "ngrok_path", lambda: "/usr/bin/ngrok")
    calls = iter([None, "https://random.ngrok.app"])
    monkeypatch.setattr(t, "_existing_tunnel", lambda port: next(calls, None))
    monkeypatch.setattr(t.subprocess, "Popen",
                        lambda argv, **k: (seen.__setitem__("argv", argv), FakeProc())[1])

    t.start_tunnel(9000)
    assert "--domain" not in seen["argv"]


def test_supervisor_without_ngrok_reports_no_url(monkeypatch):
    monkeypatch.setattr(t, "start_tunnel", lambda *a, **k: None)
    sup = t.TunnelSupervisor(9000)
    assert sup.start() == ""
    assert sup.ensure() == ("", False)


def test_url_is_read_from_our_own_agent_log(tmp_path):
    """The log is definitive for our agent, whichever API port it landed on.

    ngrok only uses 4040 if it was free; a second agent moves to 4041+, so
    polling 4040 reads someone else's agent and reports no tunnel while ours
    is up and serving.
    """
    log = tmp_path / "ngrok.log"
    log.write_text(
        't=1 lvl=warn msg="can\'t bind default web address" addr=127.0.0.1:4040\n'
        't=2 lvl=info msg="starting web service" addr=127.0.0.1:4041\n'
        't=3 lvl=info msg="started tunnel" addr=http://localhost:9000 '
        'url=https://ours.ngrok-free.app\n'
    )
    assert t._url_from_log(str(log), 0) == "https://ours.ngrok-free.app"


def test_a_previous_runs_url_is_not_mistaken_for_ours(tmp_path):
    """We read from where our own agent's output began, not the whole file."""
    log = tmp_path / "ngrok.log"
    old = 't=1 lvl=info msg="started tunnel" url=https://stale.ngrok-free.app\n'
    log.write_text(old)
    offset = log.stat().st_size
    with log.open("a") as fh:
        fh.write('t=2 lvl=info msg="started tunnel" url=https://fresh.ngrok-free.app\n')

    assert t._url_from_log(str(log), offset) == "https://fresh.ngrok-free.app"
    assert t._url_from_log(str(log), 0) == "https://fresh.ngrok-free.app"


def test_no_url_in_the_log_yet(tmp_path):
    log = tmp_path / "ngrok.log"
    log.write_text('t=1 lvl=info msg="starting"\n')
    assert t._url_from_log(str(log), 0) is None
    assert t._url_from_log(None, 0) is None


def test_agents_on_other_api_ports_are_found(monkeypatch):
    """A tunnel published by an agent on 4041 still counts as ours."""
    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_get(url, **kw):
        if url.endswith("4041/api/tunnels"):
            return R({"tunnels": [{"public_url": "https://found.ngrok-free.app",
                                   "config": {"addr": "http://localhost:9000"}}]})
        raise httpx.HTTPError("nothing here")

    # On the httpx module, not `t.httpx`: the tunnel module imports httpx
    # where it probes, so cli.py can import it without importing httpx.
    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    assert t._existing_tunnel(9000) == "https://found.ngrok-free.app"
    assert "https://found.ngrok-free.app" in t._tunnel_urls()


def test_a_relaunched_tunnel_on_the_same_address_is_still_recorded():
    """A reserved domain brings the tunnel back on the SAME url, so `changed`
    is False — and recording only on `changed` left `hub.json` naming a process
    that had already died, which is a tunnel `collab kill` walks past.

    Two facts, not one: the address is what the room cares about, the process is
    what a later `kill` needs.
    """
    from collab.server.app import tunnel_worth_recording

    # the address moved: always worth writing down
    assert tunnel_worth_recording(True, 100, 100)
    assert tunnel_worth_recording(True, 100, 200)
    # same address, new process: the case that was being dropped
    assert tunnel_worth_recording(False, 100, 200)
    # nothing at all happened
    assert not tunnel_worth_recording(False, 100, 100)


def test_a_relaunch_on_the_same_address_is_not_logged_as_a_move(tmp_path,
                                                               caplog):
    """`remember_url` ends in a WARNING that the address changed, and the pid
    path calls it too — so a reserved domain would log a move that did not
    happen, in the one log troubleshooting sends people to.

    Driven, not echoed. An earlier version of this test defined its own local
    stand-in and asserted that a list contained what it had just appended, which
    passes with the defect fully restored.
    """
    import logging

    from collab import hub_main
    from collab.server.session import HubConfig

    class Supervisor:
        def own_pid(self):
            return os.getpid()

    cfg = HubConfig(session_id="s1", host_name="h", port=8123, bind="127.0.0.1",
                    invite="i", host_token="t", home=str(tmp_path))
    cfg.dir.mkdir(parents=True, exist_ok=True)
    cfg.save()

    with caplog.at_level(logging.INFO, logger="collab.hub_main"):
        hub_main.remember_url(cfg, Supervisor(), "https://pinned.example", False)
    said = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert said, "it said nothing at all"
    assert not any(level >= logging.WARNING for level, _ in said), \
        "it warned that an address had moved when it had not"
    assert any("same address" in text for _, text in said)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="collab.hub_main"):
        hub_main.remember_url(cfg, Supervisor(), "https://moved.example", True)
    assert any(r.levelno >= logging.WARNING for r in caplog.records), \
        "a real move must still be warned about"

    # And either way the process serving it is written down, which is what
    # `collab kill` needs.
    back = HubConfig.load("s1", str(tmp_path))
    assert back.public_url == "https://moved.example"
    assert back.tunnel_process().alive()


def test_the_watcher_records_a_relaunch_and_says_the_address_did_not_move(
        tmp_path, monkeypatch):
    """The watcher driven, not read.

    A source-text assertion passes with the condition inverted, with the block
    under `if False:`, or with `on_url_change` called with the wrong arity — and
    it fails on a rename that changes nothing. This runs the loop against a
    supervisor that relaunches on the same address and checks what came out.
    """
    from starlette.testclient import TestClient

    from collab.server import app as appmod
    from collab.server.auth import new_secret
    from collab.server.store import Store

    class Supervisor:
        """Same address, new process — what a reserved domain gives."""

        public_url = "https://pinned.example"
        restarts = 0

        def __init__(self):
            self.pid = 100

        def own_pid(self):
            return self.pid

        def ensure(self):
            self.pid += 1               # relaunched
            return self.public_url, False

    seen: list[tuple[str, bool]] = []
    supervisor = Supervisor()
    monkeypatch.setattr(appmod, "TUNNEL_CHECK_SECONDS", 0.01)
    store = Store(tmp_path / "hub.db")
    try:
        app = appmod.create_app(
            store=store, session_id="s_t", host_name="alice",
            public_url=supervisor.public_url, supervisor=supervisor,
            on_url_change=lambda url, changed=True: seen.append((url, changed)))
        with TestClient(app):
            for _ in range(200):
                if seen:
                    break
                time.sleep(0.02)
    finally:
        store.close()

    assert seen, "a relaunch on the same address was never recorded"
    url, changed = seen[0]
    assert url == supervisor.public_url
    assert changed is False, "it told the room the address had moved"




def test_a_tunnel_that_will_not_start_is_stopped_rather_than_asked(monkeypatch):
    """`start_tunnel`'s timeout path sent one SIGTERM to a process in its own
    session and returned. A slow ngrok survives that: `own_pid()` reports 0,
    nothing records it, and `collab kill` never stops it — the same leak the
    pid stamps exist for, at the one moment collab knows which process it is.
    """
    import subprocess

    class Proc:
        """Ignores the first request to stop, as a busy process does."""

        pid = 4242
        returncode = None

        def __init__(self):
            self.asked = 0
            self.killed = False
            self.waits = 0

        def poll(self):
            return None

        def terminate(self):
            self.asked += 1

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waits += 1
            if not self.killed:
                raise subprocess.TimeoutExpired("ngrok", timeout or 0)
            return 0

    proc = Proc()
    monkeypatch.setattr(t, "ngrok_path", lambda: "/usr/bin/ngrok")
    monkeypatch.setattr(t, "_existing_tunnel", lambda port: None)
    monkeypatch.setattr(t, "_url_from_log", lambda path, offset: None)
    monkeypatch.setattr(t.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(t, "START_TIMEOUT", 0.01)
    monkeypatch.setattr(t.time, "sleep", lambda seconds: None)

    assert t.start_tunnel(9000) is None
    assert proc.asked == 1, "it never asked"
    assert proc.killed, "it asked and walked away"
    assert proc.waits >= 2, "it did not wait for the process to actually go"


def test_recording_an_address_does_not_revert_a_rotated_invite(tmp_path):
    """Two writers, and one of them used to lose.

    The watcher rewrites the address whenever the tunnel is relaunched, and
    `collab url --rotate` rewrites the invite on a hub that is already running.
    Written as whole-object saves they race: the watcher loads, rotate saves a
    new invite, the watcher saves what it loaded, and the invite on disk is the
    retired one while the store holds the new — so `collab url` prints a link
    that opens nothing.

    `reboot._sweep_hub` states the rule this now follows: change only the keys
    you own and write every other one back exactly as it was read.
    """
    import json as jsonlib

    from collab import hub_main
    from collab.server.session import HubConfig

    class Supervisor:
        def own_pid(self):
            return os.getpid()

    cfg = HubConfig(session_id="s1", host_name="h", port=8123, bind="127.0.0.1",
                    invite="INVITE-OLD", host_token="t", home=str(tmp_path))
    cfg.dir.mkdir(parents=True, exist_ok=True)
    cfg.save()

    # somebody rotates the invite while this process is still holding the old one
    rotated = HubConfig.load("s1", str(tmp_path))
    rotated.invite = "INVITE-NEW"
    rotated.save()

    # THE INTERLEAVE, MADE DETERMINISTIC. The race needs the watcher to have
    # READ before the rotate wrote, which no single synchronous call reproduces
    # on its own: a whole-object save that re-reads first happens to pick the
    # new invite up. Handing it the stale object is exactly the state it would
    # have been holding, and is what makes this a test of the rule rather than
    # of the timing.
    def stale(session_id, home=None):
        return cfg
    HubConfig.load = staticmethod(stale)  # type: ignore[method-assign]
    try:
        hub_main.remember_url(cfg, Supervisor(), "https://pinned.example", False)
    finally:
        del HubConfig.load

    on_disk = jsonlib.loads((cfg.dir / "hub.json").read_text())
    assert on_disk["invite"] == "INVITE-NEW", "the rotated invite was reverted"
    assert on_disk["public_url"] == "https://pinned.example"
    assert on_disk["host_token"] == "t"


def test_a_hub_json_that_cannot_be_read_is_left_alone(tmp_path):
    """On a transient read failure the old code wrote the object it happened to
    be holding from start-up, which is a whole-file revert."""
    from collab.server.session import update_fields

    missing = tmp_path / "nowhere"
    assert update_fields(missing, public_url="x") is False

    session = tmp_path / "s"
    session.mkdir()
    (session / "hub.json").write_text("{not json")
    assert update_fields(session, public_url="x") is False
    assert (session / "hub.json").read_text() == "{not json"


def test_two_writers_of_one_hub_json_never_share_a_scratch_file(tmp_path):
    """`with_suffix(".tmp")` is one name per directory, and `hub.json` has two
    writers on purpose: `update_fields` exists because the tunnel watcher and
    `collab url --rotate` overlap in different processes. Sharing `hub.tmp`,
    one truncated the other's half-written document and renamed the mixture
    into place — `json.loads` then raising «Extra data».

    Measured with two processes writing until done: 1 run in 10 left the file
    unreadable at the real ~450 bytes, 3 in 5 padded to 2 MB. Nought in ten and
    nought in five once the names are unique.
    """
    from collab.atomic import scratch as _scratch

    final = tmp_path / "hub.json"
    names = {_scratch(final) for _ in range(20)}
    assert len(names) == 20, "two writers can hold the same scratch file"
    for name in names:
        assert name.parent == final.parent, "the rename must not cross a device"
        assert name.name.startswith(".hub.json."), name.name
        assert name.stat().st_mode & 0o777 == 0o600, "it holds the host token"


def test_a_failed_write_leaves_no_scratch_file_behind(tmp_path, monkeypatch):
    """A unique name leaves a NEW file on every failure where the fixed one left
    the same file over and over — so the tidy-up is what makes uniqueness
    affordable, not a nicety. `save` had no cleanup at all before.
    """
    from collab.server import session as s

    cfg = s.HubConfig(session_id="s", host_name="h", port=1, bind="127.0.0.1",
                      invite="i", host_token="t", home=str(tmp_path), pid=0)
    cfg.save()

    def refuse(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(s.Path, "write_text", refuse)
    for _ in range(5):
        with pytest.raises(OSError):
            cfg.save()
        assert s.update_fields(cfg.dir, public_url="x") is False

    left = sorted(p.name for p in cfg.dir.iterdir())
    assert left == ["hub.json"], f"scratch files accumulated: {left}"
