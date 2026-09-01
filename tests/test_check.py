"""One command an agent can loop on to prove it is still collaborating.

Arming a watcher once and forgetting it is the failure the whole listening
section exists to prevent, and nothing tells you when it stops. No single thing
answers it either: the daemon can be live while nothing reads the feed, the feed
can be read while nobody acts on it, and an agent can act on everything while
never saying what it is doing.

So each is asked separately, each answer carries the command that fixes it, and
the whole thing is SILENT when there is nothing to do — a loop that reports
success fills the context it exists to protect, and then the agent stops reading
it, which is the same as not running it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time

import pytest

from collab import activity, cli
from collab.client import daemon as d
from collab.config import SessionProfile


@pytest.fixture()
def session(tmp_path, monkeypatch):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home),
                             participant_id="p_bob")
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread": 0}))
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    return profile


def _status(profile, **changes):
    body = json.loads((profile.dir / "status.json").read_text())
    body.update(changes)
    (profile.dir / "status.json").write_text(json.dumps(body))


def _run(**flags):
    fields = {"json": False, "verbose": False, "session": None, **flags}
    args = argparse.Namespace(**fields)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.cmd_check(args)
    return code, out.getvalue()


def _healthy(profile):
    """Listening, nothing waiting, and having said what it is doing."""
    activity.write_local(profile, activity.sanitise(
        {"state": "working", "what": "the client side"}))
    return d.watching(profile)


# --- silence is the whole point ---------------------------------------------

def test_it_says_nothing_at_all_when_there_is_nothing_to_do(session):
    with _healthy(session):
        code, out = _run()

    assert code == 0
    assert out == "", "a loop that reports success fills the context it protects"


def test_but_verbose_shows_the_working_for_a_person(session):
    with _healthy(session):
        code, out = _run(verbose=True)

    assert code == 0
    assert "listener" in out and "watching" in out and "activity" in out


# --- and what it says when something is wrong -------------------------------

def test_nothing_reading_is_a_failure_with_an_exit_code(session):
    activity.write_local(session, activity.sanitise({"state": "working", "what": "x"}))

    code, out = _run()

    assert code == 1, "a hook or a timer can carry a non-zero exit"
    assert "nothing is reading this session" in out
    assert "listen --follow" in out, "and what to do about it"


def test_unread_is_reported_as_not_acting(session):
    """Messages arrived and nobody took them — the only honest evidence that an
    agent is listening without acting."""
    _status(session, unread=3)
    with _healthy(session):
        code, out = _run()

    assert "3 unread" in out
    assert "recv" in out and "DO what they ask" in out


def test_saying_nothing_about_your_work_is_worth_a_line(session):
    with d.watching(session):
        code, out = _run()

    assert "you have not said what you are doing" in out
    assert "working" in out


def test_a_warning_alone_is_not_a_failure(session):
    """`fail` is «this session is not working»; `warn` is «you are not holding
    up your end». A loop that treats an unanswered message as a crash stops
    being run."""
    _status(session, unread=2)
    with _healthy(session):
        code, out = _run()

    assert code == 0
    assert out, "it still says so"


def test_a_dead_listener_is_a_failure(session, monkeypatch):
    monkeypatch.setattr(cli, "is_running", lambda p: None)

    code, out = _run()

    assert code == 1
    assert "listener is not running" in out
    assert "daemon start" in out


def test_polling_passes_but_says_what_it_is(session):
    d.polled(session)
    activity.write_local(session, activity.sanitise({"state": "idle"}))

    code, out = _run()

    assert code == 0, "polling is the documented fallback, not a fault"
    assert "polling" in out


def test_no_session_at_all_fails_with_the_way_out(session, monkeypatch, capsys):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: None))

    assert cli.cmd_check(argparse.Namespace(json=False, verbose=False,
                                            session=None)) == 1
    assert "join" in capsys.readouterr().out


# --- for a hook rather than a reader ----------------------------------------

def test_json_is_always_printed_and_carries_the_verdicts(session):
    with _healthy(session):
        code, out = _run(json=True)

    payload = json.loads(out)
    assert payload["ok"] is True and payload["verdict"] == "ok"
    assert {c["check"] for c in payload["checks"]} == {
        "listener", "watching", "acting", "activity"}


def test_json_says_which_one_failed(session):
    code, out = _run(json=True)

    payload = json.loads(out)
    assert payload["ok"] is False
    broken = [c for c in payload["checks"] if c["verdict"] == "fail"]
    assert [c["check"] for c in broken] == ["watching"]
    assert broken[0]["fix"], "every failure carries its own fix"


# --- what the host is told to say -------------------------------------------

def test_the_host_is_offered_an_opening_message(session, capsys):
    """The first message decides whether the next hour is collaboration or two
    monologues, and it is always the same message."""
    cli._opening_message(session)

    out = capsys.readouterr().out
    assert "listen --follow" in out, "arm a watcher"
    assert "working" in out, "and say what you are on"
    assert "check" in out, "and keep checking"


def test_the_monitor_hint_names_the_loop(session, capsys):
    cli._monitor_hint(session, {"bridge_port": 45855})

    out = capsys.readouterr().out
    assert "on a loop" in out
    assert "SILENT" in out, "so an agent knows silence is the good case"
