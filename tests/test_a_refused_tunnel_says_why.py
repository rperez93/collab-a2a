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
    assert "collab kill" in remedy, "resuming over a running hub starts a second one"
    assert "keeps that domain" in remedy, "the fix must say it lasts"


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


# ── Through `collab host` itself, with a real hub and a fake ngrok ──────────
#
# The tests above pass with both headline features removed: three mutants —
# the hub never writing `tunnel_error`, and resume or create ignoring the
# default domain — survived them (adversarial review, 2026-11-13). These drive
# the real path: `collab host` starts a hub, the hub starts our fake ngrok,
# the fake records its argv and refuses, and the host prints what hub.json says.

import contextlib
import io


@pytest.fixture
def hosting(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    config._CACHE.clear()
    return tmp_path


def _fake_ngrok_on_path(root, monkeypatch, body: str) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "ngrok"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> '{root}/argv.txt'\n{body}")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _host(root, *extra) -> str:
    """One `collab host`, and the hub it started stopped again — every time,
    so no case ever leaves two hubs on one session."""
    from collab.cli import main
    from collab.server.session import HubConfig, hosted_sessions, stop_session

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            main(["host", "--no-daemon", "--no-update-check", "--name", "alice", *extra])
    finally:
        for cfg in hosted_sessions(root / ".collab"):
            stop_session(HubConfig.load(cfg.session_id, cfg.home) or cfg)
    return out.getvalue()


def _domains(root) -> list[str]:
    """The `--domain` each ngrok launch was given, "" for none, in order."""
    seen = []
    for line in (root / "argv.txt").read_text().splitlines():
        if not line.startswith("http "):
            continue
        words = line.split()
        seen.append(words[words.index("--domain") + 1] if "--domain" in words else "")
    return seen


def test_the_host_prints_the_refusal_the_hub_recorded_with_its_remedy(hosting, monkeypatch):
    _fake_ngrok_on_path(hosting, monkeypatch,
                        "echo \"ERROR:  failed to start tunnel: The endpoint 'https://a.ngrok-free.app' "
                        "is already online. Either\"\necho 'ERROR:  ERR_NGROK_334'\nexit 1\n")
    out = _host(hosting, "--fresh")

    assert "ngrok refused: ERR_NGROK_334: The endpoint 'https://a.ngrok-free.app' is already online" in out
    assert "collab kill" in out, "the remedy must not resume over a hub that is still running"
    assert "ngrok was not found" not in out
    assert "add-authtoken" not in out, "334 is not an authtoken problem"


def test_an_unknown_refusal_gets_its_words_not_the_authtoken_paragraph(hosting, monkeypatch):
    _fake_ngrok_on_path(hosting, monkeypatch,
                        "echo 'ERROR:  failed to start tunnel: The domain x.ngrok.app is "
                        "reserved for another account. Then'\necho 'ERROR:  ERR_NGROK_320'\nexit 1\n")
    out = _host(hosting, "--fresh")

    assert "ERR_NGROK_320: The domain x.ngrok.app is reserved for another account" in out
    assert "add-authtoken" not in out
    assert "ngrok.log" in out


def test_the_default_domain_reaches_ngrok_on_create_and_a_corrected_one_on_resume(
        hosting, monkeypatch):
    """Two hosts, ~10 s each: create uses the default, and resume uses a
    CORRECTED default over the session's old pin — the order that once kept a
    wrong domain through every fix of the setting. The rest of the precedence
    is `host_domain`'s, tested below without a hub."""
    _fake_ngrok_on_path(hosting, monkeypatch, "echo 'ERROR:  ERR_NGROK_334'\nexit 1\n")

    config.set_ngrok_domain("first.ngrok.app")
    _host(hosting, "--fresh")
    config.set_ngrok_domain("fixed.ngrok.app")
    _host(hosting)

    assert _domains(hosting) == ["first.ngrok.app", "fixed.ngrok.app"]


def test_the_334_remedy_lasts_past_the_next_plain_host(hosting, monkeypatch):
    """The remedy's own path: a session created FROM the default, then
    `--resume --domain own`, then a plain `collab host`. Deleting the resume
    path's provenance once left every test green while this went shared,
    own, shared — colliding again (review mutant n1c)."""
    _fake_ngrok_on_path(hosting, monkeypatch, "echo 'ERROR:  ERR_NGROK_334'\nexit 1\n")
    config.set_ngrok_domain("shared.ngrok.app")

    _host(hosting, "--fresh")
    _host(hosting, "--resume", "--domain", "own.ngrok.app")
    _host(hosting)

    assert _domains(hosting) == ["shared.ngrok.app", "own.ngrok.app", "own.ngrok.app"]


@pytest.mark.parametrize("flag, pinned, pinned_from, default, used", [
    ("flag.ngrok.app", "pin.ngrok.app", "flag", "dflt.ngrok.app", ("flag.ngrok.app", "flag")),
    ("https://Flag.ngrok.app/", "", "", "", ("flag.ngrok.app", "flag")),
    ("", "pin.ngrok.app", "flag", "dflt.ngrok.app", ("pin.ngrok.app", "flag")),
    ("", "pin.ngrok.app", "", "dflt.ngrok.app", ("pin.ngrok.app", "flag")),
    # a legacy pin equal to the default: 2.1.2 dropped `domain_from` on save
    ("", "dflt.ngrok.app", "", "dflt.ngrok.app", ("dflt.ngrok.app", "default")),
    ("", "pin.ngrok.app", "default", "dflt.ngrok.app", ("dflt.ngrok.app", "default")),
    # a defaulted pin follows a CLEARED default too
    ("", "pin.ngrok.app", "default", "", ("", "")),
    ("", "", "", "dflt.ngrok.app", ("dflt.ngrok.app", "default")),
    ("", "", "", "", ("", "")),
])
def test_a_chosen_pin_beats_the_default_and_a_defaulted_pin_follows_it(
        flag, pinned, pinned_from, default, used):
    """A pin from before `domain_from` existed reads as chosen: a flag was
    the only way to pin then."""
    assert config.host_domain(flag, pinned, pinned_from, default) == used


def test_a_bad_domain_flag_is_refused_before_any_hub_starts(hosting, monkeypatch, capsys):
    from collab.cli import main
    assert main(["host", "--no-daemon", "--no-update-check", "--domain", "not a domain"]) == 2
    said = capsys.readouterr()
    assert "not a domain ngrok can serve" in said.out + said.err


def test_an_invalid_stored_default_is_named_by_collab_host(hosting, monkeypatch):
    """Dropped silently, a hand-edited default meant a random domain with no
    word why; `collab host` must say it is ignoring the value."""
    import json
    _fake_ngrok_on_path(hosting, monkeypatch, "echo 'ERROR:  ERR_NGROK_334'\nexit 1\n")
    path = config.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ngrok_domain": "https://bad domain/"}))
    config._CACHE.clear()

    out = _host(hosting, "--fresh")

    assert "ignoring the ngrok_domain setting 'https://bad domain/'" in out
    config.set_ngrok_domain("ok.ngrok.app")
    assert config.ngrok_domain_ignored() == ""


def test_the_reason_is_read_from_the_end_of_a_long_run(tmp_path):
    """A head-capped read returned an early, superseded failure from a 92 KB log."""
    log = tmp_path / "ngrok.log"
    log.write_text("ERROR:  failed to start tunnel: early. x\nERROR:  ERR_NGROK_1\n"
                   + "t=1 lvl=info msg=noise\n" * 4000
                   + "ERROR:  failed to start tunnel: final. x\nERROR:  ERR_NGROK_2\n")
    assert t.failure_from_log(str(log), 0) == "ERR_NGROK_2: final"


def test_a_tunnel_that_never_came_up_is_not_called_an_authtoken_problem(tmp_path):
    """A missing authtoken exits at once with ERR_NGROK_4018; a 25 s silence
    is a network fault, and the authtoken paragraph would send people to fix
    the one thing that is fine."""
    from collab.cli import tunnel_report
    from collab.server.session import HubConfig

    cfg = HubConfig(session_id="s_x", host_name="h", port=9000, bind="127.0.0.1",
                    invite="i", host_token="tok", home=str(tmp_path),
                    tunnel_error="ngrok gave no public URL within 25 s")
    out = "\n".join(tunnel_report(cfg, installed=True))
    assert "within 25 s" in out
    assert "add-authtoken" not in out
    assert "ngrok.log" in out


def test_a_hand_edited_default_that_is_not_a_domain_never_reaches_ngrok(hosting, monkeypatch):
    import json

    _fake_ngrok_on_path(hosting, monkeypatch, "echo 'ERROR:  ERR_NGROK_334'\nexit 1\n")
    path = config.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ngrok_domain": "https://bad domain/"}))
    config._CACHE.clear()

    _host(hosting, "--fresh")

    assert _domains(hosting) == [""]


def test_the_last_failure_in_a_log_names_its_own_reason(tmp_path):
    """Code from the last error, sentence from the first, once read as one."""
    log = tmp_path / "ngrok.log"
    log.write_text("ERROR:  failed to start tunnel: first reason. More\nERROR:  ERR_NGROK_1\n"
                   "ERROR:  failed to start tunnel: second reason. More\nERROR:  ERR_NGROK_2\n")
    assert t.failure_from_log(str(log), 0) == "ERR_NGROK_2: second reason"


def test_a_reason_at_the_very_end_of_the_log_is_kept(tmp_path):
    log = tmp_path / "ngrok.log"
    log.write_text("ERROR:  ERR_NGROK_9\nERROR:  failed to start tunnel: cut off here")
    assert t.failure_from_log(str(log), 0) == "ERR_NGROK_9: cut off here"


def test_one_enormous_line_is_read_in_bounded_time(tmp_path):
    """A lazy match with no terminator is quadratic: 200 KB on one line took
    9.85 s in the review. Lines are capped, and so is the read."""
    import time as clock

    log = tmp_path / "ngrok.log"
    log.write_text("failed to start tunnel: " * 20_000)   # ~480 KB, one line
    started = clock.monotonic()
    t.failure_from_log(str(log), 0)
    assert clock.monotonic() - started < 0.5


@pytest.mark.parametrize("given, stored", [
    ("x.ngrok.app:443", "x.ngrok.app"),
    ("https://x.ngrok.app/?q=1", "x.ngrok.app"),
])
def test_a_port_or_query_is_stripped_from_a_pasted_domain(given, stored):
    assert config.set_ngrok_domain(given) == stored


def test_a_bare_ip_address_is_not_a_reservable_domain():
    with pytest.raises(ValueError):
        config.set_ngrok_domain("1.2.3.4")
