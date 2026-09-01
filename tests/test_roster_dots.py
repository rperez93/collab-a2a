"""The dot in front of a name says whether that agent is at work.

The roster already carried a dot per person, in that person's own colour,
saying whether they were connected — which is the question you ask once, when
you arrive. The one you ask all day is whether they are *doing* something, and
it was answered by typing a message and waiting.

So the shape carries it: **filled** while working, **hollow** while idle or
away. The colour stays the person's, because the dot's job in this pane is to
say who; a second colour on it would compete with that, and the state word
beside it already says online or offline.
"""

from __future__ import annotations

import time
import types

import pytest

from collab import activity
from collab.client import tui
from collab.config import SessionProfile


def _model(people):
    profile = SessionProfile(session_id="s", url="u", name="me", host_name="host",
                             token="t", home="/tmp")
    return types.SimpleNamespace(
        profile=profile,
        participants=lambda: people,
        snapshot={"participants": people},
    )


def _person(name="bob", *, connected=True, doing=None, **extra):
    person = {"name": name, "connected": connected, "id": f"p_{name}",
              "activity": doing or {}}
    person.update(extra)
    return person


def _head(person):
    """The first row of that person's block: the dot, the name, the state."""
    rows = tui.roster_rows(_model([person]), 120)
    return rows[0].text


WORKING = {"state": "working", "what": "the token refresh",
           "files": ["src/api/auth.py"], "since": time.time()}
IDLE = {"state": "idle", "since": time.time()}


def test_a_working_agent_gets_a_filled_dot():
    assert "●" in _head(_person(doing=WORKING))


def test_an_idle_agent_gets_a_hollow_one():
    head = _head(_person(doing=IDLE))
    assert "○" in head and "●" not in head


def test_an_agent_that_has_said_nothing_is_not_shown_as_working():
    """Silence is not work. It reads as free, which is the safer error: it
    invites a question rather than suppressing one."""
    assert "●" not in _head(_person())


def test_somebody_offline_is_hollow_whatever_they_last_said():
    """Their last word was «working»; they are not there to be working."""
    assert "●" not in _head(_person(connected=False, doing=WORKING))


def test_the_dot_keeps_the_persons_own_colour():
    """It is what tells people apart at a glance; the shape carries the state."""
    rows = tui.roster_rows(_model([_person(doing=WORKING)]), 120)
    assert rows[0].edge == tui._speaker_pair("bob")


def test_the_line_says_what_they_are_doing():
    head = _head(_person(doing=WORKING))
    assert "the token refresh" in head
    assert "src/api/auth.py" in head


def test_what_they_are_doing_now_beats_what_they_said_on_arrival():
    """The focus is a sentence from the join; by lunchtime the two are
    different questions, and only one of them is current."""
    head = _head(_person(doing=WORKING, focus="the client side"))
    assert "the token refresh" in head
    assert "the client side" not in head


def test_with_nothing_current_the_focus_is_still_shown():
    head = _head(_person(focus="the client side"))
    assert "the client side" in head


def test_an_idle_note_reaches_the_line():
    head = _head(_person(doing={"state": "idle", "what": "waiting on your review",
                                "since": time.time()}))
    assert "waiting on your review" in head


def test_a_long_objective_does_not_push_the_row_past_the_pane():
    person = _person(doing={"state": "working", "what": "x" * 200,
                            "since": time.time()})
    rows = tui.roster_rows(_model([person]), 80)
    assert tui._w(rows[0].text) <= 80
