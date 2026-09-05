"""Why nothing woke, answered without reading the source.

The wake runs on three clocks and printed none of them. `settle` holds a first
turn back while a burst finishes; `min_gap` decides how often other people's
messages may start a turn at all; the retry pause is what a failure buys, and
it grows with each one. Somebody whose agent has not been woken is in exactly
one of those three, and the page they open said «waiting 0 unread, 0
undelivered» — which is a fact about the queue and no answer at all.

So `collab wake show` prints all three, what each is currently doing, when a
delivery was last attempted and when one last arrived, and the reason `due()`
gives right now. `collab status` gets the same in two lines, because that is the
page an agent runs on a loop.

Which makes the read-only rule the thing to protect. `due()` starts the
reminder's interval the first time it is asked — deliberately, so that «never
reminded» and «reminded an hour ago» are not the same stored zero — and a page
that asked it while printing would have started a clock by being looked at.
An agent polling `collab status` would then have pushed its own reminder over
the horizon on every poll, and the reminder would never have fired at all.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json

import pytest

from collab import cli, config as cfg, wake
from collab.client import daemon as d


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    yield
    cfg._CACHE.clear()


def _waker(root, clock, **config):
    wake.write_config(root, wake.WakeConfig(command=["true"], **config))
    return wake.Waker(root, "s", now=lambda: clock[0])


# --- the clocks are separate facts ---------------------------------------------

def test_all_three_clocks_are_reported_apart(tmp_path):
    """One «not yet» would send somebody looking in the wrong one."""
    clock = [1000.0]
    told = _waker(tmp_path, clock, settle=15, min_gap=60, timeout=300).explain()
    assert told["settle"] == 15
    assert told["min_gap"] == 60
    assert told["timeout"] == 300
    assert told["retry_pause"] == wake.RETRY_PAUSE


def test_a_backoff_reports_the_pause_and_what_is_left_of_it(tmp_path):
    """The pause grows with the failures, so its length alone does not say
    whether anything is being held right now."""
    clock = [1000.0]
    waker = _waker(tmp_path, clock)
    waker.failed(None)
    waker.failed(None)
    clock[0] += 30
    told = waker.explain()
    assert told["failures"] == 2
    assert told["retry_pause"] == wake.RETRY_PAUSE * 2
    assert told["backing_off_for"] == pytest.approx(wake.RETRY_PAUSE * 2 - 30)


def test_nothing_is_backing_off_when_nothing_has_failed(tmp_path):
    told = _waker(tmp_path, [1000.0]).explain()
    assert told["backing_off_for"] == 0
    assert told["failures"] == 0


def test_the_attempt_and_the_arrival_are_kept_apart(tmp_path):
    """They were one field, and a wake that had never once succeeded still
    reported «last woke 2m ago» to somebody reading this for reassurance."""
    clock = [1000.0]
    waker = _waker(tmp_path, clock)
    waker.failed(None)
    told = waker.explain()
    assert told["last_attempt"] == 1000.0
    assert told["last_delivery"] == 0


def test_the_reason_a_turn_is_not_due_comes_back_with_the_clocks(tmp_path):
    told = _waker(tmp_path, [1000.0]).explain()
    assert told["due"] is False
    assert told["why"] == "nothing unread"


# --- and asking must not change anything ---------------------------------------

def test_explaining_does_not_start_the_reminders_interval(tmp_path):
    """`due` starts it the first time it is asked, so that «never reminded» and
    «reminded an hour ago» are not the same stored zero. A page that asked
    while printing would have started a clock by being looked at — and an agent
    polling that page would have pushed its own reminder over the horizon on
    every poll."""
    clock = [1000.0]
    waker = _waker(tmp_path, clock)
    for _ in range(5):
        waker.explain()
    assert waker._state["reminded_at"] == 0, "printing started the clock"
    assert waker.next_reminder_at() == 0

    # The daemon asking is what starts it, and then the answer is a real time.
    waker.due()
    assert waker._state["reminded_at"] == 1000.0
    assert waker.next_reminder_at() == 1000.0 + cfg.DEFAULT_REMIND_EVERY * 60


def test_the_reminder_line_says_off_rather_than_inventing_a_next_time(tmp_path):
    cfg.setting("remind_every").write(0)
    told = _waker(tmp_path, [1000.0]).explain()
    assert told["reminder"]["every"] == 0
    assert told["reminder"]["next_at"] == 0
    assert cli._reminder_line(told) == "off"


def test_a_reminder_never_yet_sent_says_so_and_names_no_time(tmp_path):
    told = _waker(tmp_path, [1000.0]).explain()
    line = cli._reminder_line(told)
    assert "never yet" in line
    assert "last" not in line, "there is no last one to name"


def test_a_reminder_that_went_out_says_when_and_when_the_next_is(tmp_path):
    clock = [1000.0]
    waker = _waker(tmp_path, clock)
    waker.reminded()
    told = waker.explain()
    line = cli._reminder_line(told)
    assert f"every {cfg.DEFAULT_REMIND_EVERY}m" in line
    assert "last " in line and "next " in line


def test_the_route_is_named_only_when_the_state_knows_it(tmp_path):
    """Empty is a real answer: a state file written before the route was
    recorded knows a reminder went out and does not know how, and «monitor»
    guessed there would be a reading invented to fill the sentence."""
    clock = [1000.0]
    waker = _waker(tmp_path, clock)
    waker.reminded()
    assert waker.reminded_via == ""
    assert " via " not in cli._reminder_line(waker.explain())


# --- what the command prints -----------------------------------------------------

def _show(profile, monkeypatch, **kwargs):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    fields = {"action": "show", "session": None, "json": False, "notify": None,
              "settle": None, "min_gap": None, "timeout": None, "run": [],
              "agent": None, "target": None, "yes": True, "to": None,
              "expect_pid": None, "expect_command": None}
    args = argparse.Namespace(**{**fields, **kwargs})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_wake(args)
    return code, out.getvalue()


def test_wake_show_prints_the_three_clocks_and_what_is_happening(
        profile, monkeypatch):
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    code, out = _show(profile, monkeypatch)
    assert code == 0
    assert "settle" in out and "burst window" in out
    assert "min gap" in out and "timeout" in out
    assert "backoff" in out
    assert "last tried" in out and "last arrived" in out
    assert "right now" in out


def test_wake_show_json_carries_the_same_facts(profile, monkeypatch):
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    code, out = _show(profile, monkeypatch, json=True)
    payload = json.loads(out)
    assert payload["why"]
    assert payload["due"] is False
    assert payload["retry_pause"] == wake.RETRY_PAUSE
    assert payload["reminder"]["every"] == cfg.DEFAULT_REMIND_EVERY


def test_wake_show_leaves_the_reminders_clock_where_it_found_it(
        profile, monkeypatch):
    """The whole point of the read-only path, asked through the command."""
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    for _ in range(3):
        _show(profile, monkeypatch)
    assert wake.Waker(root, "s")._state["reminded_at"] == 0


def _status(profile, monkeypatch, as_json=False):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=as_json))
    return out.getvalue()


def test_status_carries_the_wake_and_the_reminder_in_two_lines(
        profile, monkeypatch):
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    text = _status(profile, monkeypatch)
    lines = [line for line in text.splitlines()
             if line.strip().startswith(("wake ", "reminder "))]
    assert len(lines) == 2, text
    assert "settle" in lines[0] and "gap" in lines[0]
    assert f"every {cfg.DEFAULT_REMIND_EVERY}m" in lines[1]


def test_status_says_not_armed_rather_than_printing_clocks_nothing_runs_on(
        profile, monkeypatch):
    text = _status(profile, monkeypatch)
    assert "not armed" in text


def test_status_leaves_the_reminders_clock_where_it_found_it(profile, monkeypatch):
    """This is the page an agent runs on a loop. If looking at it started the
    interval, the reminder would never once have fired."""
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    for _ in range(4):
        _status(profile, monkeypatch)
    assert wake.Waker(root, "s")._state["reminded_at"] == 0
