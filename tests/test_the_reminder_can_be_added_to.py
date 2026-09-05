"""A standing reminder somebody can add to, not only replace.

The reminder was one setting per role and one way to change it: write the whole
thing. That is the right shape for «say this instead» and the wrong one for
«say this as well», which is what anybody actually wants after the first week —
one more standing instruction, on top of the four that are already working.

Appending had a trap in it worth building the feature around. The stored value
starts empty and the shipped text is what an empty value MEANS, so an append
that simply added to the key would store the addition alone — and the agent
would then be reminded of the new line and nothing else. Nobody would see it
happen: a reminder still arrives, still reads as a reminder, and is missing the
part that was doing the work. So `add` materialises the shipped text first.

The rest is about the two reminders being two. A host and a guest are told
different things, and an edit to one must not touch the other; the role is
taken from the session when there is one and demanded when there is not,
because both keys exist and both accept anything, so a guess would edit the
wrong text quietly.

That it is live needs no mechanism and is tested rather than assumed:
`Waker.reminder()` reads the config at every delivery, so an append between two
reminders reaches the second one on either route, with nothing restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys

import pytest

from collab import cli, config, wake
from collab.client import daemon as d
from collab.config import SessionProfile

from test_reminder_on_the_monitor import (MINUTE, a_daemon, arm, beat,
                                          woken_reminders)

MINUTES = MINUTE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    config._CACHE.clear()
    yield tmp_path
    config._CACHE.clear()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="bob",
                           host_name="alice", token="t", home=str(home))
    saved.save()
    return saved


def _run(monkeypatch, *, profile=None, **flags):
    """`collab remind <action>`, with both streams captured."""
    monkeypatch.setattr(cli.SessionProfile, "current",
                        classmethod(lambda c: profile))
    fields = {"action": "show", "text": [], "host": False, "guest": False,
              "file": None, "session": None}
    args = argparse.Namespace(**{**fields, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_remind(args)
    return code, out.getvalue()


def _text(is_host):
    return config.reminder_settings(is_host)["text"]


# --- adding keeps what was already there -------------------------------------------

@pytest.mark.parametrize("role,is_host", [("host", True), ("guest", False)])
def test_adding_to_the_shipped_reminder_keeps_the_shipped_reminder(
        role, is_host, monkeypatch):
    """The trap this feature is built around. Appending to an empty key would
    store the addition alone, and «add one more instruction» would silently
    delete the four that were already being followed."""
    shipped = config.shipped_reminder(is_host)

    code, out = _run(monkeypatch, action="add", host=is_host, guest=not is_host,
                     text=["Check the board before you ask anybody anything."])

    assert code == 0
    now_says = _text(is_host)
    assert shipped in now_says, "the shipped instructions were discarded"
    assert "Check the board" in now_says
    assert "added to" in out


def test_a_second_addition_keeps_the_first(monkeypatch):
    _run(monkeypatch, action="add", host=True, text=["One."])
    _run(monkeypatch, action="add", host=True, text=["Two."])

    now_says = _text(True)
    assert "One." in now_says and "Two." in now_says
    assert now_says.index("One.") < now_says.index("Two."), "in the order added"


def test_additions_are_paragraphs_and_not_run_together(monkeypatch):
    """Two standing instructions run together read as one sentence that
    contradicts itself."""
    _run(monkeypatch, action="add", host=True, text=["One."])

    assert "\n\nOne." in _text(True)


def test_adding_nothing_changes_nothing(monkeypatch):
    before = _text(True)
    code, out = _run(monkeypatch, action="add", host=True, text=["   "])

    assert code == 1 and "nothing to add" in out
    assert _text(True) == before


# --- and replacing still replaces ----------------------------------------------------

@pytest.mark.parametrize("role,is_host", [("host", True), ("guest", False)])
def test_set_replaces_the_whole_thing(role, is_host, monkeypatch):
    shipped = config.shipped_reminder(is_host)

    code, out = _run(monkeypatch, action="set", host=is_host, guest=not is_host,
                     text=["Only this."])

    assert code == 0 and "replaced" in out
    assert _text(is_host) == "Only this."
    assert shipped not in _text(is_host)


def test_set_after_add_replaces_the_additions_too(monkeypatch):
    """«Instead of all that» has to be sayable, or the only way back from a long
    reminder is editing the config file by hand."""
    _run(monkeypatch, action="add", host=True, text=["An addition."])

    _run(monkeypatch, action="set", host=True, text=["Start again."])

    assert _text(True) == "Start again."


def test_clear_gives_back_the_shipped_one(monkeypatch):
    _run(monkeypatch, action="add", host=True, text=["An addition."])

    code, out = _run(monkeypatch, action="clear", host=True)

    assert code == 0 and "shipped one again" in out
    assert _text(True) == config.shipped_reminder(True)
    assert "remind_host" not in json.loads(
        (config.global_config_path()).read_text()), \
        "cleared means the key is gone, not that it holds the shipped text"


# --- the two roles are two -----------------------------------------------------------

def test_editing_one_role_leaves_the_other_alone(monkeypatch):
    """A host adding a line for themselves must not change what their guests
    are told; the two are told different things on purpose."""
    _run(monkeypatch, action="add", host=True, text=["For the host only."])

    assert "For the host only." in _text(True)
    assert _text(False) == config.shipped_reminder(False)


def test_the_role_defaults_to_this_session(monkeypatch, profile):
    """Asking somebody to name a role collab already knows is asking them to
    repeat themselves."""
    profile.is_host = True

    _run(monkeypatch, profile=profile, action="add", text=["Ours."])

    assert "Ours." in _text(True)
    assert "Ours." not in _text(False)


def test_a_guest_session_edits_the_guest_reminder(monkeypatch, profile):
    profile.is_host = False

    _run(monkeypatch, profile=profile, action="add", text=["Ours."])

    assert "Ours." in _text(False)
    assert "Ours." not in _text(True)


def test_outside_a_session_the_role_is_required(monkeypatch):
    """There is nothing to infer it from, and both keys accept anything — so a
    guess would edit the wrong reminder quietly."""
    before = (_text(True), _text(False))

    code, out = _run(monkeypatch, action="add", text=["Which?"])

    assert code == 1
    assert "say whose reminder you mean" in out and "--host" in out
    assert (_text(True), _text(False)) == before


def test_both_roles_at_once_is_refused(monkeypatch):
    code, out = _run(monkeypatch, action="add", host=True, guest=True,
                     text=["x"])

    assert code == 1 and "pick one" in out


# --- what show says ------------------------------------------------------------------

def test_show_says_the_text_is_shipped_when_nobody_has_written_one(monkeypatch):
    _code, out = _run(monkeypatch, action="show", host=True)

    assert "shipped" in out
    assert "You are the host" in out


def test_show_says_it_is_yours_once_you_have_touched_it(monkeypatch):
    _run(monkeypatch, action="add", host=True, text=["Mine."])

    _code, out = _run(monkeypatch, action="show", host=True)

    assert "yours" in out and "Mine." in out


def test_show_numbers_the_lines(monkeypatch):
    """The reason to look at this is usually to change one line of it, and «the
    third line» is how a person says which."""
    _code, out = _run(monkeypatch, action="show", host=True)

    assert "  1  " in out and "  2  " in out


def test_show_says_how_much_room_is_left(monkeypatch):
    _code, out = _run(monkeypatch, action="show", host=True)

    assert f"of {config.MAX_REMIND_TEXT} characters" in out


# --- the size limit ------------------------------------------------------------------

def test_an_addition_that_would_overflow_is_refused_with_both_numbers(
        monkeypatch):
    """«Too long» leaves somebody guessing whether to trim a line or start
    again, and this is a text people add to over weeks."""
    before = _text(True)

    code, out = _run(monkeypatch, action="add", host=True,
                     text=["x" * config.MAX_REMIND_TEXT])

    assert code == 1
    assert str(config.MAX_REMIND_TEXT) in out
    assert "characters" in out
    assert _text(True) == before, "and nothing was written"


def test_a_replacement_that_is_too_long_is_refused_the_same_way(monkeypatch):
    code, out = _run(monkeypatch, action="set", host=True,
                     text=["y" * (config.MAX_REMIND_TEXT + 1)])

    assert code == 1 and str(config.MAX_REMIND_TEXT) in out
    assert _text(True) == config.shipped_reminder(True)


def test_a_reminder_right_at_the_limit_is_allowed(monkeypatch):
    code, _out = _run(monkeypatch, action="set", host=True,
                      text=["z" * config.MAX_REMIND_TEXT])

    assert code == 0
    assert len(_text(True)) == config.MAX_REMIND_TEXT


# --- where the text comes from --------------------------------------------------------

def test_the_text_can_come_from_a_file(monkeypatch, tmp_path):
    """A reminder is a paragraph, and paragraphs are awkward to type at a
    shell."""
    where = tmp_path / "extra.txt"
    where.write_text("Two lines\nof standing instruction.\n", encoding="utf-8")

    code, _out = _run(monkeypatch, action="add", host=True, file=str(where))

    assert code == 0
    assert "Two lines\nof standing instruction." in _text(True)


def test_the_text_can_come_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("From a pipe."))

    code, _out = _run(monkeypatch, action="set", host=True, file="-")

    assert code == 0 and _text(True) == "From a pipe."


def test_a_file_that_is_not_there_is_said_so(monkeypatch, tmp_path):
    code, out = _run(monkeypatch, action="add", host=True,
                     file=str(tmp_path / "nope.txt"))

    assert code == 1 and "could not read" in out


# --- and it is live, on both routes ----------------------------------------------------
#
# `Waker.reminder()` reads the config at every delivery, so this needs no
# mechanism of its own. It is tested rather than assumed because it is the whole
# promise of the feature: an agent told to add a standing instruction expects
# the next reminder to carry it, and would restart its daemon if it did not.

def test_an_addition_between_two_reminders_reaches_the_second_on_the_monitor(
        profile, monkeypatch, isolated):
    (isolated / "config.json").write_text(json.dumps({"remind_every": 10}))
    config._CACHE.clear()
    clock = [10_000.0]
    monkeypatch.setattr(d, "watchers", lambda _p: [1])
    daemon = a_daemon(profile, clock=clock, is_host=True)
    seen = []

    beat(daemon, seen)                       # starts the interval
    clock[0] += 10 * MINUTES
    beat(daemon, seen)                       # the first reminder
    assert seen and "Snapshot the board" not in seen[-1]["text"]

    _run(monkeypatch, action="add", host=True, text=["Snapshot the board."])

    clock[0] += 10 * MINUTES
    beat(daemon, seen)

    assert "Snapshot the board." in seen[-1]["text"], \
        "the daemon was still carrying the reminder it started with"


def test_an_addition_reaches_the_next_woken_turn_too(profile, monkeypatch,
                                                     isolated, tmp_path):
    """The other route, and the one that costs a turn — so an agent woken with
    the old text has spent that turn on out-of-date instructions."""
    (isolated / "config.json").write_text(json.dumps({"remind_every": 10}))
    config._CACHE.clear()
    landed = tmp_path / "woken.txt"
    clock = [10_000.0]
    monkeypatch.setattr(d, "watchers", lambda _p: [])
    daemon = a_daemon(profile, clock=clock, is_host=True)
    arm(daemon, landed)

    beat(daemon)
    clock[0] += 10 * MINUTES
    beat(daemon)
    assert woken_reminders(landed) == 1

    _run(monkeypatch, action="add", host=True, text=["Snapshot the board."])

    clock[0] += 10 * MINUTES
    beat(daemon)

    assert "Snapshot the board." in landed.read_text()


def test_a_guests_daemon_carries_the_guest_reminder(profile, monkeypatch,
                                                    isolated):
    """Per role all the way through: a host's edit changes nothing about what
    a guest's own daemon delivers."""
    (isolated / "config.json").write_text(json.dumps({"remind_every": 10}))
    config._CACHE.clear()
    clock = [10_000.0]
    monkeypatch.setattr(d, "watchers", lambda _p: [1])
    daemon = a_daemon(profile, clock=clock, is_host=False)
    seen = []

    _run(monkeypatch, action="add", host=True, text=["For the host only."])
    _run(monkeypatch, action="add", guest=True, text=["For the guest only."])

    beat(daemon, seen)
    clock[0] += 10 * MINUTES
    beat(daemon, seen)

    carried = seen[-1]["text"]
    assert "For the guest only." in carried
    assert "For the host only." not in carried


def test_clearing_between_two_reminders_reaches_the_second(profile, monkeypatch,
                                                           isolated):
    """The way back has to be live too, or somebody who added a line by mistake
    is stuck with it until they restart."""
    (isolated / "config.json").write_text(json.dumps({"remind_every": 10}))
    config._CACHE.clear()
    clock = [10_000.0]
    monkeypatch.setattr(d, "watchers", lambda _p: [1])
    daemon = a_daemon(profile, clock=clock, is_host=True)
    seen = []
    _run(monkeypatch, action="add", host=True, text=["Remove me."])

    beat(daemon, seen)
    clock[0] += 10 * MINUTES
    beat(daemon, seen)
    assert "Remove me." in seen[-1]["text"]

    _run(monkeypatch, action="clear", host=True)
    clock[0] += 10 * MINUTES
    beat(daemon, seen)

    assert "Remove me." not in seen[-1]["text"]


# --- and `remind now` still does what it did -------------------------------------------

def test_now_is_still_the_default_free_action(monkeypatch, profile):
    """The command grew a group around it; the action that was there before
    must still be spelled the same way."""
    from collab.cli import build_parser

    args = build_parser().parse_args(["remind", "now"])
    assert args.action == "now"


def test_the_bare_command_shows_rather_than_asking_for_one(monkeypatch):
    """A bare `collab remind` used to be an error. Showing is the harmless
    reading of it, and asking for a delivery is not something to do by
    accident: it spends a turn."""
    from collab.cli import build_parser

    args = build_parser().parse_args(["remind"])
    assert args.action == "show"
