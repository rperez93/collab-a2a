"""Keeping the public address alive across a tunnel that ends on its own."""

from __future__ import annotations

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
