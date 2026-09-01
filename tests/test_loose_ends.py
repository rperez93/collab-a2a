"""Three things that were true, reported wrongly.

Each of these was written down as known-and-not-fixed when it shipped, and each
has the same shape: a fact that was correct when it was recorded, still being
read as though it were current.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
import types

import pytest

from collab import activity, cli, config
from collab.client import daemon as d
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="alice",
                       host_name="jarvis", token="t", home=str(home),
                       participant_id="p_me")
    p.save()
    (p.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time()}))
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: p))
    monkeypatch.setattr(cli, "is_running", lambda _p: 4242)
    return p


def _status(monkeypatch):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=True))
    return json.loads(out.getvalue())


def _status_text():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=False))
    return out.getvalue()


# --- 1. polling is a way of listening ---------------------------------------

def test_a_poll_is_recorded(profile):
    assert d.last_poll(profile) == 0.0

    d.polled(profile)

    assert time.time() - d.last_poll(profile) < 5


def test_an_agent_that_polls_is_not_told_nobody_is_listening(profile, monkeypatch):
    """It was doing exactly what the instructions told it to do, and was
    answered in red with the same advice."""
    d.polled(profile)

    payload = _status(monkeypatch)
    assert payload["watching"] is True
    assert payload["polling"] is True
    assert "NOTHING IS READING" not in (payload.get("hint") or "")


def test_but_polling_is_not_counted_as_an_armed_watcher(profile, monkeypatch):
    """A watcher hears a message as it lands; a poller hears it next turn.
    Flattening the two would hide the difference that matters."""
    d.polled(profile)

    payload = _status(monkeypatch)
    assert payload["watchers"] == 0
    assert "polling, not watching" in payload["hint"]


def test_a_poll_from_last_week_is_not_listening(profile, monkeypatch):
    (profile.dir / d.POLL_FILE).write_text(str(time.time() - 60 * 60 * 24 * 7))

    payload = _status(monkeypatch)
    assert payload["polling"] is False
    assert payload["watching"] is False
    assert "NOTHING IS READING" in payload["hint"]


def test_the_monitor_line_says_which_it_is(profile, monkeypatch):
    d.polled(profile)
    assert "polling" in _status_text()


def test_with_a_watcher_armed_the_poll_is_a_footnote(profile, monkeypatch):
    d.polled(profile)
    with d.watching(profile):
        line = _status_text()
    assert "1 armed" in line
    assert "polled" in line


def test_recv_registers_the_poll_itself(profile, monkeypatch):
    """The whole point: no agent has to remember to do this."""
    cli.cmd_recv(argparse.Namespace(session=None, wait=0, limit=10, json=False,
                                    peek=False, mine_too=False))

    assert time.time() - d.last_poll(profile) < 5


# --- 2. an activity that nothing renews is not current ----------------------

FRESH = {"state": "working", "what": "the token refresh",
         "since": time.time() - 300, "updated_at": time.time()}
ABANDONED = {"state": "working", "what": "the token refresh",
             "since": time.time() - 9000, "updated_at": time.time() - 9000}


def test_a_recent_statement_is_current():
    assert activity.is_working(FRESH)
    assert not activity.is_stale(FRESH)


def test_one_nothing_has_renewed_is_not(profile):
    """The agent said «working» and was killed. Nothing retracts it, so it
    stood — and «who is free» is the question this was built to answer."""
    assert activity.is_stale(ABANDONED)
    assert not activity.is_working(ABANDONED)


def test_a_stale_line_reads_as_a_last_word_not_a_present_tense():
    said = activity.describe(ABANDONED)
    assert said.startswith("last said")
    assert "not since" in said


def test_an_activity_with_no_stamp_is_left_alone():
    """From a collab that predates the heartbeat: unknown, not stale."""
    assert not activity.is_stale({"state": "working", "what": "x"})


def test_the_daemon_re_asserts_it_so_it_stays_current(profile):
    """`updated_at` is only a heartbeat if something beats it."""
    assert d.ACTIVITY_REFRESH < activity.STALE_AFTER, \
        "renewed less often than it goes stale would mark working agents dead"


def test_who_does_not_print_the_last_words_of_somebody_offline(capsys):
    cli._print_snapshot({"participants": [
        {"name": "bob", "connected": False, "activity": FRESH},
    ]}, "alice")

    assert "the token refresh" not in capsys.readouterr().out


def test_who_does_print_it_for_somebody_here(capsys):
    cli._print_snapshot({"participants": [
        {"name": "bob", "connected": True, "activity": FRESH},
    ]}, "alice")

    assert "the token refresh" in capsys.readouterr().out


def test_the_roster_dot_goes_hollow_when_the_statement_goes_stale():
    from collab.client import tui

    model = types.SimpleNamespace(
        profile=SessionProfile(session_id="s", url="u", name="me",
                               host_name="h", token="t", home="/tmp"),
        participants=lambda: [{"name": "bob", "connected": True,
                               "activity": ABANDONED}])
    assert "●" not in tui.roster_rows(model, 120)[0].text


# --- 3. whoami answers about this session -----------------------------------

def test_whoami_reports_the_name_this_session_uses(profile, monkeypatch, capsys):
    """An agent that joined as `alice` through COLLAB_NAME was told it was
    `rafael-perez` — true of the machine, and not the question asked."""
    monkeypatch.setattr(cli, "resolve_name", lambda *a, **k: "rafael-perez")
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: profile.home)

    cli.cmd_whoami(argparse.Namespace())

    out = capsys.readouterr().out
    assert "alice" in out.split("colour")[0], "the session name is the headline"
    assert "in this session" in out


def test_whoami_says_both_when_they_disagree(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_name", lambda *a, **k: "rafael-perez")

    cli.cmd_whoami(argparse.Namespace())

    out = capsys.readouterr().out
    assert "rafael-perez" in out and "alice" in out
    assert "everyone else sees" in out


def test_whoami_outside_a_session_still_answers(monkeypatch, capsys):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: None))
    monkeypatch.setattr(cli, "resolve_name", lambda *a, **k: "rafael-perez")

    cli.cmd_whoami(argparse.Namespace())

    assert "rafael-perez" in capsys.readouterr().out
