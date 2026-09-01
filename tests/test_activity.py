"""What an agent is doing, published rather than asked for.

Two agents spend their attention asking each other the same two questions —
*are you working?* and *on what?* — and every answer is stale by the time it is
read. The agent that just started editing `api/auth.py` is the only thing that
knows, and it knows before anyone thinks to ask. So it says so, once, and the
roster and the feed carry it.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import activity
from collab.config import SessionProfile
from collab.protocol import KIND_ACTIVITY, Envelope


# --- the shape --------------------------------------------------------------

def test_a_state_that_is_not_one_of_the_two_is_refused():
    assert activity.sanitise({"state": "busy"}) == {}
    assert activity.sanitise({"what": "no state at all"}) == {}
    assert activity.sanitise("working") == {}


def test_working_carries_the_objective_and_the_files():
    out = activity.sanitise({"state": "working", "what": "the token refresh",
                             "files": ["src/api/auth.py", "tests/test_auth.py"]})
    assert out["state"] == "working"
    assert out["what"] == "the token refresh"
    assert out["files"] == ["src/api/auth.py", "tests/test_auth.py"]


def test_going_idle_drops_the_objective_and_the_files():
    """An idle agent's last objective is finished work; leaving it on the
    roster reads as still doing it."""
    out = activity.sanitise({"state": "idle", "files": ["src/api/auth.py"]})
    assert "files" not in out
    assert out["state"] == "idle"


def test_an_idle_note_is_kept_because_waiting_is_worth_saying():
    out = activity.sanitise({"state": "idle", "what": "waiting on your review"})
    assert out["what"] == "waiting on your review"


def test_it_is_a_roster_line_not_a_design_document():
    out = activity.sanitise({"state": "working", "what": "x" * 500,
                             "files": [f"f{i}.py" for i in range(50)]})
    assert len(out["what"]) <= activity.MAX_WHAT
    assert len(out["files"]) <= activity.MAX_FILES


def test_rewording_what_you_are_doing_does_not_restart_the_clock():
    """«working for 40 minutes» has to survive a change of wording, or it
    quietly becomes «last spoke 2 minutes ago», which is a lesser fact."""
    first = activity.sanitise({"state": "working", "what": "the refresh"})
    first["since"] = time.time() - 2400

    second = activity.sanitise({"state": "working", "what": "the token refresh"},
                               previous=first)
    assert second["since"] == first["since"]
    assert activity.elapsed(second) == "40m"


def test_but_changing_state_does_restart_it():
    working = activity.sanitise({"state": "working", "what": "the refresh"})
    working["since"] = time.time() - 2400

    idle = activity.sanitise({"state": "idle"}, previous=working)
    assert idle["since"] > working["since"]


def test_the_line_a_person_reads():
    out = activity.sanitise({"state": "working", "what": "the token refresh",
                             "files": ["src/api/auth.py"], "task": "T_9d63"})
    said = activity.describe(out)
    assert "working on the token refresh" in said
    assert "T_9d63" in said
    assert "src/api/auth.py" in said


def test_nothing_said_is_not_the_same_as_idle():
    assert activity.describe({}) == ""
    assert activity.describe({"state": "idle"}).startswith("idle")


# --- on the feed ------------------------------------------------------------

def test_it_arrives_as_a_line_an_agent_can_read():
    env = Envelope(kind=KIND_ACTIVITY, sender="bob", body=activity.sanitise(
        {"state": "working", "what": "the client side", "files": ["app.tsx"]}))

    line = env.render_line()
    assert line.startswith("[working] bob:")
    assert "the client side" in line and "app.tsx" in line


# --- the local copy ---------------------------------------------------------

@pytest.fixture()
def profile(tmp_path):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="u", name="me", host_name="host",
                       token="t", home=str(home), participant_id="p_me")
    p.save()
    return p


def test_what_was_published_is_kept_for_the_reconnect(profile):
    """A hub that was unreachable for a minute must not leave the room
    believing you are still on the thing you finished."""
    stored = activity.sanitise({"state": "working", "what": "the refresh"})
    activity.write_local(profile, stored)

    assert activity.read_local(profile) == stored


def test_somebody_elses_activity_is_not_read_as_yours(profile, tmp_path):
    """Two agents in one repo: the same trap the usage figures fell into."""
    theirs = SessionProfile(session_id="s", url="u", name="bob", host_name="host",
                            token="t", home=str(tmp_path / "collab-bob"),
                            participant_id="p_bob")
    theirs.save()
    activity.write_local(theirs, activity.sanitise({"state": "working",
                                                    "what": "their work"}))
    (profile.dir / activity.ACTIVITY_FILE).write_text(
        (theirs.dir / activity.ACTIVITY_FILE).read_text())

    assert activity.read_local(profile) == {}
