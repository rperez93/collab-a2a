"""Whether anybody is actually reading the feed, and saying so when nobody is.

The daemon delivers into a file and a socket whether or not a monitor is
attached. So an agent whose watcher was dropped — a restart, a context
compaction, a closed shell — looks exactly like an agent in a quiet
conversation, both to itself and to the person waiting for an answer, and the
first anybody knows of it is a question that went unanswered for an hour.

Nothing can force an agent to keep a monitor armed. What it can do is make the
absence of one visible, and say so at the two moments an agent is listening:
when it hosts or joins, and whenever it asks for status.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time

import pytest

from collab.client import daemon as d
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    (p.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time()}))
    return p


# --- the register -----------------------------------------------------------

def test_a_reader_is_registered_while_it_reads(profile):
    assert d.watchers(profile) == []
    with d.watching(profile):
        assert d.watchers(profile) == [os.getpid()]
    assert d.watchers(profile) == [], "and is gone when it stops"


def test_a_reader_that_died_without_cleaning_up_does_not_count(profile):
    """A killed Monitor cannot tidy after itself; the pid tells on it anyway."""
    directory = d.watchers_dir(profile)
    directory.mkdir(parents=True)
    (directory / str(2 ** 22 - 1)).write_text("")     # above the usual pid_max

    assert d.watchers(profile) == []
    assert not (directory / str(2 ** 22 - 1)).exists(), "and the file is swept up"


def test_the_register_survives_an_unwritable_state_directory(profile, monkeypatch):
    """Registering is bookkeeping. Failing at it must not stop the stream."""
    monkeypatch.setattr(d.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    with d.watching(profile):
        pass                                           # no raise is the assertion


# --- what `collab status` says ----------------------------------------------

def _status(profile, monkeypatch, *, running=True):
    from collab import cli

    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda cls: profile))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242 if running else None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=True))
    return json.loads(out.getvalue())


def test_status_says_when_nothing_is_listening(profile, monkeypatch):
    payload = _status(profile, monkeypatch)
    assert payload["watching"] is False
    assert payload["watchers"] == 0
    assert "NOTHING IS READING" in payload["hint"]
    assert "listen --follow" in payload["hint"], "and how to fix it"


def test_status_counts_a_live_reader(profile, monkeypatch):
    with d.watching(profile):
        payload = _status(profile, monkeypatch)
    assert payload["watching"] is True
    assert payload["watchers"] == 1
    assert "hint" not in payload


def test_a_websocket_subscriber_counts_too(profile, monkeypatch):
    """A Monitor on the bridge is armed as much as one tailing the lines, and
    only the daemon can see it — so the daemon reports it."""
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "ws_clients": 1}))

    payload = _status(profile, monkeypatch)
    assert payload["watching"] is True
    assert payload["watchers"] == 1


def test_a_dead_daemon_is_the_louder_problem(profile, monkeypatch):
    """Both are wrong at once; the one to fix first is the one that is said."""
    payload = _status(profile, monkeypatch, running=False)
    assert "daemon start" in payload["hint"]


# --- what host and join print -----------------------------------------------

def _hint(profile, capsys, status=None):
    from collab.cli import _monitor_hint

    _monitor_hint(profile, status or {"bridge_port": 45855})
    return capsys.readouterr().out


def test_arming_is_an_instruction_not_an_option(profile, capsys):
    """«To receive messages in real time, arm a Monitor on one of these» reads
    as a suggestion, and as Claude Code vocabulary at that."""
    out = _hint(profile, capsys)
    assert "NOW" in out and "keep it armed" in out


def test_it_says_the_arming_lasts_the_whole_session(profile, capsys):
    out = _hint(profile, capsys)
    assert "until the session ends" in out
    assert "arm it again" in out, "because nothing re-arms it for you"


def test_it_names_the_fallback_for_an_agent_with_no_watcher(profile, capsys):
    out = _hint(profile, capsys)
    assert "recv --wait 60" in out


def test_it_points_at_the_way_to_check(profile, capsys):
    """And at running that check on a LOOP: arming once is the failure this
    whole section exists to prevent, so pointing at a one-off would repeat it."""
    out = _hint(profile, capsys)
    assert "check" in out
    assert "on a loop" in out


@pytest.mark.parametrize("command", ["host", "join"])
def test_both_ways_in_print_it(command):
    """The obligation is the same either way, so it is the same text."""
    import inspect

    from collab import cli

    source = inspect.getsource(getattr(cli, f"cmd_{command}"))
    assert "_monitor_hint(" in source
