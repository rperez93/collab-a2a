"""A session with nothing attached must not come up saying it is listening.

`wait_until_live` asked `status["state"] == "live"` and nothing else. That file
is the daemon's own account of itself and a daemon that was killed never gets
to correct it, so with no daemon running and no pid file at all a join returned
live off a heartbeat hours old — instantly, which also made the twenty-second
timeout dead code whenever such a file existed.

It is the fault that hid the others: a session that reports itself listening
gives nobody a reason to look at why nothing is arriving. `effective_state` had
judged this properly from the heartbeat all along and was simply not asked.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import cli
from collab.client import exclusive, onboard
from collab.client.hub_client import HubError
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    return p


def _wrote(profile, **status):
    """What the daemon left behind in its last write."""
    body = {"state": "live", "heartbeat": time.time(), "others_connected": 0,
            "unread": 0, **status}
    (profile.dir / "status.json").write_text(json.dumps(body))
    return body


def _really_live(profile):
    """A daemon that is running: a held lock, a pid file, a fresh heartbeat."""
    lock = exclusive.DaemonLock(profile.dir)
    assert lock.acquire()
    (profile.dir / "daemon.pid").write_text(exclusive.stamp())
    _wrote(profile, state="live", heartbeat=time.time())
    return lock


def test_a_status_left_by_a_killed_daemon_does_not_satisfy_the_wait(profile):
    """No daemon, no pid file, and a heartbeat two hours old — and it returned
    live in no measurable time at all."""
    _wrote(profile, state="live", heartbeat=time.time() - 7200)

    started = time.monotonic()
    last = onboard.wait_until_live(profile, timeout=0.5)
    waited = time.monotonic() - started

    assert waited >= 0.5, "it took the file's word and did not wait at all"
    assert not cli._is_listening(profile, last)


def test_a_daemon_that_is_really_there_satisfies_it_at_once(profile):
    """The guard must not turn into a delay on every join."""
    lock = _really_live(profile)
    try:
        started = time.monotonic()
        last = onboard.wait_until_live(profile, timeout=5.0)

        assert time.monotonic() - started < 1.0
        assert last["state"] == "live"
        assert cli._is_listening(profile, last)
    finally:
        lock.release()


def test_a_rejected_token_is_still_raised_rather_than_waited_out(profile):
    """Being removed from a session is an answer, not a slow start-up."""
    _wrote(profile, state="unauthorized")

    with pytest.raises(HubError):
        onboard.wait_until_live(profile, timeout=5.0)


def test_a_fresh_heartbeat_is_not_enough_without_a_daemon(profile):
    """A file can be fresh and still be the last thing a dead process wrote:
    `daemon stop` writes `stopped`, but SIGKILL writes nothing at all, and
    everything after that is the clock catching up with a file that stopped
    moving."""
    _wrote(profile, state="live", heartbeat=time.time())

    assert not cli._is_listening(profile, cli.read_status(profile))


def test_a_join_that_finds_nothing_running_says_so(profile):
    """`ensure_daemon` now declines to start a second daemon, so a wait that a
    stale file also satisfies would let a join report live having neither found
    a daemon nor started one."""
    _wrote(profile, state="live", heartbeat=time.time() - 7200)

    assert not cli._is_listening(profile, onboard.read_status(profile))


def test_daemon_start_does_not_announce_a_listener_that_never_came_up(
        profile, monkeypatch, capsys):
    """It printed the raw field, so after `wait_until_live` had spent twenty
    seconds NOT being satisfied by a stale file, it announced «daemon live»
    from that same file — the one command a person runs when they suspect the
    listener is gone, answering with the thing that misled them."""
    import argparse

    stale = _wrote(profile, state="live", heartbeat=time.time() - 7200)
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr(cli.onboard, "ensure_daemon", lambda p: stale)

    cli.cmd_daemon(argparse.Namespace(action="start"))

    printed = capsys.readouterr().out
    assert "daemon live" not in printed
    assert "daemon offline" in printed


def test_the_banner_does_not_head_a_dead_session_as_live(
        profile, monkeypatch, capsys):
    """Over every bare `collab`, and it read `· live` for a session that had
    been dead for days."""
    _wrote(profile, state="live", heartbeat=time.time() - 7200)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda cls: profile))

    cli.print_overview()

    printed = capsys.readouterr().out
    assert "· live" not in printed
    assert "· offline" in printed
