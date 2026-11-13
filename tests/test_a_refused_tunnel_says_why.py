"""A tunnel ngrok refuses to start is reported with ngrok's reason, not a guess.

On 2026-11-13 `collab host` ran on a machine with ngrok installed whose free
account's one endpoint was already online in another ngrok process. ngrok
exited with ERR_NGROK_334; the host printed «no public tunnel» followed by
«ngrok was not found» — true of nothing — and the session went local-only with
no hint of the cause or of the reserved domain that fixed it.
"""

from __future__ import annotations

import os
import stat

import pytest

from collab import config
from collab.server import tunnel as t

#: What ngrok 3.39 wrote to its log for ERR_NGROK_334, both the logfmt line
#: and the plain ERROR lines, as captured from the live failure.
REFUSAL = (
    't=2026-11-13T17:25:59-0500 lvl=crit msg="command failed" err="failed to '
    "start tunnel: The endpoint 'https://willing-settled-iguana.ngrok-free.app'"
    " is already online. Either\\n1. stop your existing endpoint first, or\\n2."
    ' start both endpoints\\r\\n\\r\\nERR_NGROK_334\\r\\n"\n'
    "ERROR:  failed to start tunnel: The endpoint "
    "'https://willing-settled-iguana.ngrok-free.app' is already online. Either\n"
    "ERROR:  ERR_NGROK_334\n"
)


def _fake_ngrok(tmp_path, body: str, status: int = 1) -> str:
    """An executable that prints `body` and exits — a real process, not a mock,
    so the log is written and read back the way the hub's is."""
    payload = tmp_path / "refusal.txt"
    payload.write_text(body)
    exe = tmp_path / "ngrok"
    exe.write_text(f"#!/bin/sh\ncat '{payload}'\nexit {status}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_ngroks_reason_is_read_back_whole_including_the_dotted_domain(tmp_path):
    """The message quotes a domain; a terminator on any «.» cut it at the first
    label and left «https://willing-settled-iguana» as the reason."""
    log = tmp_path / "ngrok.log"
    log.write_text("earlier run: ERR_NGROK_108\n" + REFUSAL)
    offset = len("earlier run: ERR_NGROK_108\n")

    reason = t.failure_from_log(str(log), offset)

    assert reason == ("ERR_NGROK_334: The endpoint "
                      "'https://willing-settled-iguana.ngrok-free.app' "
                      "is already online")


def test_a_log_with_no_recognisable_cause_yields_no_invented_one(tmp_path):
    log = tmp_path / "ngrok.log"
    log.write_text("t=1 lvl=info msg=starting\n")
    assert t.failure_from_log(str(log), 0) == ""
    assert t.failure_from_log(None, 0) == ""
    assert t.remedy_for("") == ""


def test_the_endpoint_in_use_refusal_names_the_reserved_domain_remedy():
    remedy = t.remedy_for("ERR_NGROK_334: The endpoint 'x' is already online")
    assert "--domain" in remedy
    assert "ngrok_domain" in remedy, "the remedy must name the setting that fixes it for good"


def test_a_real_ngrok_that_exits_hands_its_reason_to_the_supervisor(tmp_path, monkeypatch):
    exe = _fake_ngrok(tmp_path, REFUSAL)
    monkeypatch.setattr(t, "ngrok_path", lambda: exe)
    monkeypatch.setattr(t, "_existing_tunnel", lambda port: None)

    sup = t.TunnelSupervisor(9000, log_path=str(tmp_path / "ngrok.log"))
    assert sup.start() == ""

    assert sup.last_error.startswith("ERR_NGROK_334: ")
    assert sup.own_pid() == 0


def test_an_exit_with_nothing_in_the_log_still_says_ngrok_exited(tmp_path, monkeypatch):
    exe = _fake_ngrok(tmp_path, "", status=3)
    monkeypatch.setattr(t, "ngrok_path", lambda: exe)
    monkeypatch.setattr(t, "_existing_tunnel", lambda port: None)

    sup = t.TunnelSupervisor(9000, log_path=str(tmp_path / "ngrok.log"))
    sup.start()

    assert sup.last_error == "ngrok exited with status 3"


def test_a_start_that_succeeds_clears_the_previous_reason(monkeypatch):
    sup = t.TunnelSupervisor(9000)
    sup.last_error = "ERR_NGROK_334: stale"
    monkeypatch.setattr(t, "start_tunnel",
                        lambda *a, **k: t.Tunnel(public_url="https://a.ngrok.app"))

    assert sup.start() == "https://a.ngrok.app"
    assert sup.last_error == "", "a reason left behind would be printed by the next host"


def test_the_failure_reaches_the_hub_record_and_a_recovery_clears_it(tmp_path):
    """`collab host` prints what `hub.json` says; the watcher's recovery must
    not leave the old reason there for the next host to repeat."""
    import json

    from collab import hub_main
    from collab.server.session import HubConfig

    cfg = HubConfig(session_id="s_x", host_name="h", port=9000, bind="127.0.0.1",
                    invite="i", host_token="tok", home=str(tmp_path),
                    tunnel_error="ERR_NGROK_334: busy")
    cfg.dir.mkdir(parents=True)
    cfg.save()
    assert json.loads((cfg.dir / "hub.json").read_text())["tunnel_error"]

    hub_main.remember_url(cfg, None, "https://back.ngrok.app", True)

    assert json.loads((cfg.dir / "hub.json").read_text())["tunnel_error"] == ""


@pytest.mark.parametrize("given, stored", [
    ("rafael.example.ngrok.app", "rafael.example.ngrok.app"),
    ("https://Rafael.Example.ngrok.app/", "rafael.example.ngrok.app"),
    ("https://rafael.example.ngrok.app#secret", "rafael.example.ngrok.app"),
])
def test_the_default_ngrok_domain_is_stored_as_the_bare_host(given, stored):
    """People paste the URL out of a browser; ngrok's `--domain` wants the host."""
    assert config.set_ngrok_domain(given) == stored
    assert config.ngrok_domain() == stored


@pytest.mark.parametrize("bad", ["not a domain", "localhost", "-x.ngrok.app", "a..b"])
def test_a_default_ngrok_domain_ngrok_could_not_serve_is_refused(bad):
    with pytest.raises(ValueError):
        config.set_ngrok_domain(bad)
    assert config.ngrok_domain() == "", "a refused value must not be half-written"


def test_an_empty_default_ngrok_domain_clears_it():
    config.set_ngrok_domain("a.ngrok.app")
    assert config.set_ngrok_domain("") == ""
    assert "ngrok_domain" not in config.load_config()


def test_the_setting_is_in_the_registry_beside_rules():
    names = [s.name for s in config.settings()]
    assert "ngrok_domain" in names
    assert names.index("ngrok_domain") == names.index("rules") + 1
