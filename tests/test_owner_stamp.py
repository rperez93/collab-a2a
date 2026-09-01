"""A file this agent wrote must stay readable to this agent.

Usage figures and «what I am working on» are stamped, so that two agents sharing
a repo cannot read each other's. The stamp is the participant id — it survives a
rename, which a name does not — and the id is not always known when the file is
first written: `collab host` looks it up from the hub and carries on when the
hub does not answer.

Reading back with only the id is what turned that hiccup into a permanent fault.
The file gets stamped with the directory, `_adopt_identity` fills the id in
seconds later, the stamp no longer matches, and the file is unreadable for the
life of the session — the agent shows as «last said working … not since» while
it is working, and nothing short of deleting the file recovers it.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import activity, stats
from collab.config import SessionProfile, owner_ids


@pytest.fixture()
def home(tmp_path):
    (tmp_path / "collab" / "sessions" / "s").mkdir(parents=True)
    return tmp_path / "collab"


def _profile(home, participant_id=""):
    p = SessionProfile(session_id="s", url="u", name="alice", host_name="alice",
                       token="t", home=str(home), participant_id=participant_id)
    p.save()
    return p


# --- the identities that mean "me" ------------------------------------------

def test_both_the_id_and_the_directory_are_ours(home):
    profile = _profile(home, participant_id="p_me")
    assert owner_ids(profile) == ("p_me", str(profile.dir))


def test_the_id_is_preferred_because_it_survives_a_rename(home):
    profile = _profile(home, participant_id="p_me")
    assert stats.owner_of(profile) == "p_me"


def test_without_an_id_the_directory_stands_in(home):
    profile = _profile(home)
    assert owner_ids(profile) == (str(profile.dir),)
    assert stats.owner_of(profile) == str(profile.dir)


# --- the window that broke it -----------------------------------------------

def test_an_activity_written_before_the_id_arrived_is_still_ours(home):
    """`collab host` carries on when the hub does not answer the id probe. A
    session started through that hiccup wrote files nobody could read again."""
    early = _profile(home)                       # no id yet
    activity.write_local(early, activity.sanitise(
        {"state": "working", "what": "the token refresh"}))

    later = _profile(home, participant_id="p_me")   # the id arrives
    assert activity.read_local(later)["what"] == "the token refresh"


def test_and_usage_figures_written_in_that_window_too(home):
    early = _profile(home)
    stats.write_stats(early, {"model": "Opus 5", "cost_usd": 1.5})

    later = _profile(home, participant_id="p_me")
    assert stats.read_stats(later)["model"] == "Opus 5"


def test_the_reverse_order_works_as_well(home):
    """Written with the id, read by something that has not learnt it yet."""
    known = _profile(home, participant_id="p_me")
    activity.write_local(known, activity.sanitise({"state": "idle"}))

    assert activity.read_local(known)["state"] == "idle"


# --- and it must still refuse somebody else's -------------------------------

def test_another_agents_activity_is_still_refused(home, tmp_path):
    """The whole reason for the stamp: two agents in one repo."""
    mine = _profile(home, participant_id="p_me")
    (tmp_path / "collab-bob" / "sessions" / "s").mkdir(parents=True)
    theirs = _profile(tmp_path / "collab-bob", participant_id="p_bob")
    activity.write_local(theirs, activity.sanitise(
        {"state": "working", "what": "their work"}))

    (mine.dir / activity.ACTIVITY_FILE).write_text(
        (theirs.dir / activity.ACTIVITY_FILE).read_text())

    assert activity.read_local(mine) == {}


def test_another_agents_figures_are_still_refused(home, tmp_path):
    mine = _profile(home, participant_id="p_me")
    (tmp_path / "collab-bob" / "sessions" / "s").mkdir(parents=True)
    theirs = _profile(tmp_path / "collab-bob", participant_id="p_bob")
    stats.write_stats(theirs, {"model": "gpt-5"})
    (mine.dir / stats.STATS_FILE).write_text(
        (theirs.dir / stats.STATS_FILE).read_text())

    assert stats.read_stats(mine) == {}


def test_an_unstamped_file_is_still_nobody_s(home):
    profile = _profile(home, participant_id="p_me")
    (profile.dir / activity.ACTIVITY_FILE).write_text(json.dumps({"state": "working"}))

    assert activity.read_local(profile) == {}
