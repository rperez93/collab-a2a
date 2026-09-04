"""Somebody else holding a name I also answer to is still somebody else.

Names are handed out by the hub and reused by people: you join as `alice`,
somebody already holds it, and the hub calls you `alice-2` — so your own
history, signed `alice`, reads as a stranger's unless the viewer knows better.

It knows better from the roster: an entry with a different participant id is a
different person, whatever it is called. That identifier is minted per session,
which is its limit — it says nothing about who you were yesterday — but it is
the one the protocol has, and it settles every case where both are present.
"""
from __future__ import annotations

import types

import pytest

from collab.client import tui


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(tui, "_pair_for", lambda v: 900)
    monkeypatch.setattr(tui, "default_color", lambda: "#00cccc")
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    tui._CHOSEN.clear()
    tui._OTHERS.clear()
    tui._SLOTS.clear()
    yield
    tui._CHOSEN.clear()
    tui._OTHERS.clear()
    tui._SLOTS.clear()


def participants(session_name, my_pid, roster):
    model = types.SimpleNamespace(
        snapshot={"participants": list(roster)},
        profile=types.SimpleNamespace(name=session_name, participant_id=my_pid),
        events=[],
    )
    return tui.Model.participants.__get__(model, tui.Model)


# --- the case this exists for -------------------------------------------------

def test_somebody_else_holding_my_old_name_is_somebody_else():
    """I am `alice-2` because another person is `alice`.

    Without this my colour is painted onto them and their messages are aligned
    to my side of the screen — the viewer says they are me.
    """
    participants("alice-2", "p_me", [
        {"name": "alice", "id": "p_them"},
        {"name": "alice-2", "id": "p_me"},
    ])()
    assert "alice" in tui._OTHERS
    assert tui._CHOSEN.get("alice") is None, "their name took my colour"
    assert not tui._is_mine("alice", "alice-2")


def test_my_own_old_name_is_still_mine():
    """The other half, and the reason a blanket rule is not enough.

    The hub suffixed me and nobody else is holding the original: my history
    signed `alice` is mine, and must keep my colour and my side.
    """
    participants("alice-2", "p_me", [{"name": "alice-2", "id": "p_me"}])()
    assert "alice" not in tui._OTHERS
    assert tui._is_mine("alice", "alice-2")


def test_two_agents_in_one_room_are_two_people():
    participants("alice", "p_me", [
        {"name": "alice", "id": "p_me"},
        {"name": "bob", "id": "p_bob"},
    ])()
    assert "bob" in tui._OTHERS
    assert not tui._is_mine("bob", "alice")


def test_a_roster_with_no_ids_claims_nobody():
    """Nothing to go on is not a licence to guess."""
    participants("alice", "p_me", [{"name": "alice"}, {"name": "bob"}])()
    assert tui._OTHERS == set()


def test_my_own_entry_is_never_a_stranger():
    """The control: excluding others must not exclude me."""
    participants("alice", "p_me", [
        {"name": "alice", "id": "p_me"},
        {"name": "bob", "id": "p_bob"},
    ])()
    assert "alice" not in tui._OTHERS


# --- the colour follows the same rule -----------------------------------------

def test_my_colour_reaches_my_old_name_and_not_theirs():
    participants("alice-2", "p_me", [
        {"name": "alice", "id": "p_them"},
        {"name": "alice-2", "id": "p_me"},
    ])()
    assert tui._CHOSEN.get("alice-2") == "#00cccc"
    assert tui._CHOSEN.get("alice") is None


def test_their_published_colour_is_theirs():
    """Excluding them from my names must not blind me to their own colour."""
    participants("alice-2", "p_me", [
        {"name": "alice", "id": "p_them", "color": "#ff7f50"},
        {"name": "alice-2", "id": "p_me"},
    ])()
    assert tui._CHOSEN.get("alice") == "#ff7f50"


def test_a_roster_that_says_nothing_about_colour_does_not_erase_mine():
    """`color: ""` means "not published yet", not "has none".

    The roster carries that for everyone who has not published anything —
    which is everyone at the start.
    """
    participants("alice", "p_me", [{"name": "alice", "id": "p_me", "color": ""}])()
    assert tui._CHOSEN.get("alice") == "#00cccc"
