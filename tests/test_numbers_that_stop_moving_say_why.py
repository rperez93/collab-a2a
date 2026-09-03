"""A usage figure stops moving only with a visible reason.

The figures `collab stats` and the roster split work by travel three routes —
a status line payload stashed to a file, a command the daemon polls, a report
by hand — and every one of them was silent when it failed. Measured on a
scratch hub before this change: a fresh file took 7–9 s to reach the hub
(the report rode the 9 s snapshot timer, not the 3 s heartbeat); a payload the
status line could not attribute to a session was dropped with no trace; a usage
command that exited 1 left the old figure standing and `collab check` calling
it «current» for the next thirty minutes; a live route whose figures happened
not to change never moved `reported_at`, so an idle agent read as «old» while
reporting on schedule.

The invariant enforced here: a figure the room is reading is at most one
heartbeat behind the file, and when the file itself stops, the reason is in
`status.json`, in `collab check`, and under your own row in `collab stats`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import threading
import time

import httpx
import pytest

from collab import cli, config, lockfile, stats
from collab.client import daemon as d
from collab.config import SessionProfile
from collab.statusline import install as sli, render as r


def _wait(pred, *, timeout=15.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    config.set_share_stats(True)
    return tmp_path / "config.json"


# --- the daemon: one heartbeat, not one snapshot timer ------------------------------

def _join(base, session, name):
    rr = httpx.post(f"{base}/ext/collab/v1/join",
                    json={"invite": session["invite"], "name": name, "hello": {}},
                    timeout=10)
    rr.raise_for_status()
    return rr.json()


def _bob_on_hub(base, headers):
    rr = httpx.get(f"{base}/ext/collab/v1/participants", headers=headers, timeout=10)
    rr.raise_for_status()
    for p in rr.json()["participants"]:
        if p["name"] == "bob":
            return p.get("stats") or {}
    return {}


@pytest.fixture()
def beating_guest(live_server, session, tmp_path, isolated_config):
    """bob's real daemon running its real heartbeat against a real hub."""
    base = live_server["base"]
    joined = _join(base, session, "bob")
    home = tmp_path / "collab"
    (home / "sessions" / "s_test").mkdir(parents=True)
    profile = SessionProfile(session_id="s_test", url=base, name="bob", host_name="alice",
                             token=joined["token"], home=str(home),
                             participant_id=joined["id"])
    profile.save(make_current=False)
    daemon = d.Daemon(profile)
    daemon.state = "live"
    loop = asyncio.new_event_loop()
    holder: dict = {}

    async def go():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            daemon._http = client
            holder["task"] = asyncio.current_task()
            with contextlib.suppress(asyncio.CancelledError):
                await daemon._heartbeat_loop()

    thread = threading.Thread(target=lambda: loop.run_until_complete(go()), daemon=True)
    thread.start()
    _wait(lambda: (profile.dir / "status.json").exists(), what="the first heartbeat")
    yield {"daemon": daemon, "profile": profile, "base": base,
           "bob": {"Authorization": f"Bearer {joined['token']}"}}
    daemon._stop.set()
    if "task" in holder:
        loop.call_soon_threadsafe(holder["task"].cancel)
    thread.join(timeout=10)
    daemon.inbox.close()
    loop.close()


def test_a_fresh_figure_reaches_the_hub_within_one_heartbeat(beating_guest, monkeypatch):
    """Measured before: 7.6 s and 8.6 s from file to hub — the report rode the
    9 s snapshot timer. The heartbeat is 3 s, and that is the promise."""
    daemon, profile, base, bob = (beating_guest[k] for k in ("daemon", "profile", "base", "bob"))
    # Off the phase of the first tick, which reports whatever it finds at once.
    time.sleep(1.0)
    t0 = time.time()
    stats.write_stats(profile, {"model": "opus", "cost_usd": 4.2})
    _wait(lambda: _bob_on_hub(base, bob).get("cost_usd") == 4.2, timeout=12,
          what="the figure on the hub")
    latency = time.time() - t0
    assert latency < d.STATUS_HEARTBEAT + 1.0, f"took {latency:.1f}s"

    # AND AN UNCHANGED FIGURE FRESHLY WRITTEN MOVES `reported_at`. The status
    # line rewrites the file every refresh; a route that is alive but whose
    # numbers stand still must not read as «old».
    monkeypatch.setattr(d, "STATS_REASSERT", 0.5, raising=False)
    first = _bob_on_hub(base, bob)["reported_at"]
    time.sleep(0.6)
    stats.write_stats(profile, {"model": "opus", "cost_usd": 4.2})
    _wait(lambda: _bob_on_hub(base, bob)["reported_at"] > first, timeout=8,
          what="reported_at to move for an unchanged, re-written figure")

    # And status.json says what the daemon did with it.
    status = json.loads((profile.dir / "status.json").read_text())
    assert status["stats"]["sent_at"] is not None
    assert status["stats"]["file_written_at"] is not None
    assert status["stats"]["post_error"] is None


# --- the status line: no proof is not silence -------------------------------------------

def _claim(home, name, *, session_id="s"):
    home.mkdir(parents=True, exist_ok=True)
    (home / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
    p = SessionProfile(session_id=session_id, url="http://h/", name=name, host_name="alice",
                       token="t", home=str(home), participant_id=f"p_{name}")
    p.save(make_current=False)
    (home / "current").write_text(session_id + "\n")
    lockfile.acquire(lockfile.Lock(name=name, session_id=session_id, role="guest",
                                   participant_id=f"p_{name}", state_dir=str(home),
                                   listener_pid=os.getpid(),
                                   owner_pids=[424242]), home)
    return p


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    # A process chain that meets no lock: what a sandbox looks like from inside.
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: [os.getpid(), 1])
    return tmp_path


PAYLOAD = json.dumps({"model": {"display_name": "Opus 5"}, "cost": {"total_cost_usd": 3.2}})


def test_the_only_claim_in_the_repo_is_not_taken_on_trust(repo):
    """A's lock, with a chain that is not ours; B's payload arrives.

    «One claim, so it must be ours» reads as safe and is not: B's lock is
    taken only AFTER `join_session` returns, so during B's own join the
    repository holds exactly one claim — A's. B's status line, unable to prove
    its directory in that window, would write B's usage into A's file, stamped
    with A's own owner, and A's daemon would publish B's spend as A's on the
    next heartbeat. That is the leak `_own_profile` exists to stop, silently
    reintroduced. A's file stays untouched and the marker says what happened.
    """
    _claim(repo / ".collab", "alice")
    assert config.claimed_home(repo) is None, "the fixture must leave nothing proven"

    r.stash_agent_stats(PAYLOAD, repo)

    assert not (repo / ".collab" / "sessions" / "s" / stats.STATS_FILE).exists(), \
        "B's figures landed in A's file"
    marker = stats.unattributed(repo)
    assert marker["figures"]["cost_usd"] == 3.2
    assert marker["homes"] == [str(repo / ".collab")]


def test_two_claims_and_no_proof_leave_a_marker_not_silence(repo):
    alice = _claim(repo / ".collab", "alice")
    bob = _claim(repo / ".collab-bob", "bob")

    r.stash_agent_stats(PAYLOAD, repo)

    for home in (repo / ".collab", repo / ".collab-bob"):
        assert not (home / "sessions" / "s" / stats.STATS_FILE).exists(), "nothing guessed"
    marker = stats.unattributed(repo)
    assert marker["figures"]["cost_usd"] == 3.2
    assert time.time() - marker["at"] < 5
    assert set(marker["homes"]) == {str(repo / ".collab"), str(repo / ".collab-bob")}

    # Both agents are told, with the fix, from their own directory.
    for profile in (alice, bob):
        (profile.dir / "status.json").write_text(json.dumps(
            {"state": "live", "heartbeat": time.time(), "stats": {}}))
        verdict, detail, fix = cli._stats_health(profile)
        assert verdict == cli.CHECK_WARN, (profile.name, detail)
        assert "attribut" in detail, detail
        assert f"COLLAB_HOME={profile.home}" in fix, fix


# --- the polling route: a failing command is a reported failure -----------------------

def _live_status(profile, **stats_block):
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread": 0,
         "stats": stats_block}))


def test_a_failing_usage_command_is_written_down_and_checked(profile, isolated_config,
                                                              monkeypatch):
    config.set_stats_source(command="sh -c 'echo quota endpoint said 401 >&2; exit 1'",
                            interval=15)
    daemon = d.Daemon(profile)
    try:
        asyncio.run(daemon._refresh_stats_from_command())
        daemon.write_status()
        status = json.loads((profile.dir / "status.json").read_text())
        error = status["stats"]["source_error"]
        assert "401" in error["detail"], error
        assert error["command"].startswith("sh -c")

        monkeypatch.setattr(cli, "is_running", lambda p: 4242)
        verdict, detail, fix = cli._stats_health(profile)
        assert verdict == cli.CHECK_WARN
        assert "401" in detail, detail
        assert "sh -c" in fix or "stats --source" in fix, fix
    finally:
        daemon.inbox.close()


def test_a_command_that_recovers_clears_the_reason(profile, isolated_config):
    config.set_stats_source(command="sh -c 'exit 1'", interval=15)
    daemon = d.Daemon(profile)
    try:
        asyncio.run(daemon._refresh_stats_from_command())
        config.set_stats_source(command="echo '{\"model\":\"x\",\"cost_usd\":1}'", interval=15)
        daemon._stats_ran_at = 0.0
        asyncio.run(daemon._refresh_stats_from_command())
        daemon.write_status()
        status = json.loads((profile.dir / "status.json").read_text())
        assert status["stats"]["source_error"] is None
        assert stats.read_stats(profile)["cost_usd"] == 1
    finally:
        daemon.inbox.close()


# --- collab check and collab stats say the same thing ---------------------------------

def test_figures_written_but_not_accepted_by_the_hub_are_a_warning(profile, isolated_config,
                                                                    monkeypatch):
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    now = time.time()
    _live_status(profile, file_written_at=now - 60, sent_at=now - 400,
                 post_error="hub answered 502", source_error=None, route="file")
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    verdict, detail, fix = cli._stats_health(profile)
    assert verdict == cli.CHECK_WARN
    assert "502" in detail, detail


def test_sharing_off_with_figures_in_hand_is_said(profile, isolated_config, monkeypatch):
    config.set_share_stats(False)
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    _live_status(profile)
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    verdict, detail, fix = cli._stats_health(profile)
    assert verdict == cli.CHECK_WARN
    assert "sharing" in detail and "--share on" in fix, (detail, fix)


def test_nothing_ever_reported_and_nothing_configured_is_not_a_complaint(profile,
                                                                         isolated_config,
                                                                         monkeypatch):
    """Not setting up a route is a decision, not a fault."""
    _live_status(profile)
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    assert cli._stats_health(profile) is None


def test_a_current_figure_says_when_it_was_sent(profile, isolated_config, monkeypatch):
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    now = time.time()
    _live_status(profile, file_written_at=now - 2, sent_at=now - 2, post_error=None,
                 source_error=None, route="file")
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    verdict, detail, fix = cli._stats_health(profile)
    assert verdict == cli.CHECK_OK
    assert "ago" in detail, detail


def test_check_carries_the_reason(profile, isolated_config, monkeypatch):
    config.set_stats_source(command="sh -c 'echo boom >&2; exit 1'", interval=15)
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    _live_status(profile, file_written_at=time.time() - 3000, sent_at=time.time() - 3000,
                 post_error=None, route="command",
                 source_error={"at": time.time() - 30, "command": "sh -c …",
                               "detail": "boom"})
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_check(argparse.Namespace(json=False, verbose=False, session=None))
    assert "boom" in out.getvalue(), out.getvalue()


def test_stats_prints_the_reason_under_your_own_row(profile, isolated_config, monkeypatch):
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    _live_status(profile, file_written_at=time.time() - 60, sent_at=time.time() - 400,
                 post_error="hub answered 502", source_error=None, route="file")
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    profile.participant_id = "p_me"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def participants(self):
            return {"participants": [
                {"name": profile.name, "id": "p_me", "connected": True,
                 "stats": {"model": "x", "cost_usd": 1, "reported_at": time.time() - 400}},
                {"name": "alice", "id": "p_alice", "connected": True, "is_host": True,
                 "stats": {"model": "y", "reported_at": time.time() - 5}},
            ]}

    monkeypatch.setattr(cli, "_client", lambda p: FakeClient())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_stats(argparse.Namespace(report=None, source=None, interval=None,
                                         share=None, json=False, session=None))
    text = out.getvalue()
    assert "502" in text, text
    # Under OUR row and not alice's: the reason follows the row it is about.
    assert text.index(profile.name) < text.index("502") < text.index("alice"), text


# --- the installer carries the proof it has ----------------------------------------------

def test_install_carries_collab_home_into_the_hook_when_it_has_one(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("COLLAB_HOME", "/repo/.collab-bob")
    result = sli.install_claude_code(executable="/opt/collab")
    body = result.script.read_text()
    assert "COLLAB_HOME=/repo/.collab-bob '/opt/collab' statusline render" in body, body
    assert body.index("COLLAB_HOME=") < body.index("statusline render")
    assert any("COLLAB_HOME" in note for note in result.notes), result.notes


# --- junk in status.json never takes the check down ----------------------------------

JUNK = (float("nan"), float("inf"), float("-inf"), 1e400, -5, "lots", True, [], {})


def test_junk_timestamps_in_status_json_never_raise(profile, isolated_config, monkeypatch):
    """`sent_at: NaN` passed `float()` and then reached `_ago_seconds`, which
    raised — from a value the daemon wrote and nobody types. Every timestamp
    the check reads is a remote-ish value and is judged the same way."""
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    stats.write_stats(profile, {"model": "x", "cost_usd": 1})
    for junk in JUNK:
        _live_status(profile, file_written_at=time.time() - 2, sent_at=junk,
                     post_error=None, source_error=None, route="file")
        verdict, detail, fix = cli._stats_health(profile)     # must not raise
        assert verdict == cli.CHECK_OK, (junk, detail)
        assert "nan" not in detail.lower() and "inf" not in detail.lower(), (junk, detail)

        _live_status(profile, file_written_at=time.time() - 2, sent_at=time.time() - 2,
                     post_error=None, route="command",
                     source_error={"at": junk, "command": "c", "detail": "boom"})
        verdict, detail, fix = cli._stats_health(profile)
        assert verdict == cli.CHECK_WARN and "boom" in detail, (junk, detail)

    for junk in JUNK:
        marker = config.base_home(profile.dir.parent.parent.parent) / stats.UNATTRIBUTED_FILE
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"at": junk, "figures": {}, "homes": []}))
        _live_status(profile, file_written_at=time.time() - 2, sent_at=time.time() - 2,
                     post_error=None, source_error=None, route="file")
        verdict, detail, fix = cli._stats_health(profile)
        assert verdict == cli.CHECK_OK, (junk, detail)


# --- the shipped numbers are pinned --------------------------------------------------------

def test_the_reassert_cadence_is_sixty_seconds():
    """Sixty, because the roster refreshes every nine seconds and a stamp that
    moves once a minute is enough to tell alive from stalled; re-sending on
    every beat would be a POST every three seconds from every agent for no new
    information. Every test above sets its own value; this one reads the
    shipped one."""
    assert d.STATS_REASSERT == 60.0


def test_install_without_a_home_adds_none(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    result = sli.install_claude_code(executable="/opt/collab")
    assert "COLLAB_HOME" not in result.script.read_text()
