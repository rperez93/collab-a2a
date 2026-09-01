"""A session whose listener died must not report itself live.

`status.json` is the daemon's own account of itself, and a daemon that is killed
never gets to correct it: the last thing it wrote was `live`, and `live` is what
the file says for ever after. `collab status` printed that field verbatim, so a
session whose listener died hours ago answered with a state, a host, an unread
count and a monitor line — every figure of it history, and nothing anywhere
saying so. The status line had judged this properly all along, from the
heartbeat; the judgement simply did not reach the command.
"""

from __future__ import annotations

import json
import time

import pytest

from collab.client import daemon as d
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path):
    home = tmp_path / "collab"
    session = home / "sessions" / "s"
    session.mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    return p


def _wrote(profile, **status):
    """What the daemon left behind in its last write."""
    body = {"state": "live", "heartbeat": time.time(), "others_connected": 1,
            "unread": 0, **status}
    (profile.dir / "status.json").write_text(json.dumps(body))
    return body


def _pid(profile, pid):
    (profile.dir / "daemon.pid").write_text(str(pid))


# --- the judgement itself ---------------------------------------------------

def test_a_dead_pid_settles_it_whatever_the_file_says(profile):
    """No process, no daemon — a fresh heartbeat cannot argue with that."""
    status = _wrote(profile, state="live", heartbeat=time.time())
    assert d.effective_state(status, running=False) == "offline"


def test_a_live_daemon_that_just_wrote_is_live(profile):
    status = _wrote(profile, state="live", heartbeat=time.time())
    assert d.effective_state(status, running=True) == "live"


def test_without_a_pid_to_ask_the_heartbeat_decides(profile):
    """The status line never looks up the pid; it only has the file."""
    fresh = _wrote(profile, state="live", heartbeat=time.time())
    old = dict(fresh, heartbeat=time.time() - (d.DEAD_AFTER + 5))
    assert d.effective_state(fresh) == "live"
    assert d.effective_state(old) == "offline"


def test_a_quiet_daemon_is_not_a_dead_one(profile):
    """Between the two thresholds it is reconnecting, not gone."""
    status = _wrote(profile, state="live",
                    heartbeat=time.time() - (d.STALE_AFTER + 1))
    assert d.effective_state(status) == "reconnecting"


# --- is_running, which is what the command was missing ----------------------

def test_a_pid_that_is_not_a_process_is_not_running(profile):
    _pid(profile, 2 ** 22 - 1)          # above the usual pid_max, so unused
    assert d.is_running(profile) is None


def test_our_own_pid_counts_as_running(profile):
    import os

    _pid(profile, os.getpid())
    assert d.is_running(profile) == os.getpid()


# --- what `collab status` now says ------------------------------------------

def _status_payload(profile, monkeypatch, running):
    """Run cmd_status --json against this session and read what it printed."""
    import argparse
    import io
    import contextlib

    from collab import cli

    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda cls: profile))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242 if running else None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=True))
    return json.loads(out.getvalue())


def test_the_command_reports_a_dead_listener_as_offline(profile, monkeypatch):
    _wrote(profile, state="live", heartbeat=time.time())

    payload = _status_payload(profile, monkeypatch, running=False)
    assert payload["state"] == "offline"
    assert payload["daemon_running"] is False


def test_and_keeps_the_daemons_last_word_rather_than_hiding_it(profile, monkeypatch):
    """It is how you tell «stopped cleanly» from «killed while connected»."""
    _wrote(profile, state="live", heartbeat=time.time())

    payload = _status_payload(profile, monkeypatch, running=False)
    assert payload["recorded_state"] == "live"


def test_it_says_how_to_bring_the_listener_back(profile, monkeypatch):
    _wrote(profile, state="live")

    payload = _status_payload(profile, monkeypatch, running=False)
    assert "daemon start" in payload["hint"]


def test_a_healthy_session_is_still_reported_live(profile, monkeypatch):
    """The fix must not make every session look broken."""
    import os

    _wrote(profile, state="live", heartbeat=time.time())
    (profile.dir / "watchers").mkdir()
    (profile.dir / "watchers" / str(os.getpid())).write_text("")

    payload = _status_payload(profile, monkeypatch, running=True)
    assert payload["state"] == "live"
    assert payload["daemon_running"] is True
    assert "recorded_state" not in payload, "nothing to correct, nothing to say"
    assert "hint" not in payload


# --- and what the watch pane paints -----------------------------------------

def test_the_pane_badge_follows_the_same_judgement(profile, monkeypatch):
    """A pane left open after its listener died sat there saying `live`."""
    from collab.client import tui

    _wrote(profile, state="live", heartbeat=time.time())
    (profile.dir / "snapshot.json").write_text(json.dumps({"participants": []}))
    monkeypatch.setattr(tui, "is_running", lambda p: None)

    model = tui.Model(profile=profile)
    model.refresh_side()
    assert model.state() == "offline"
