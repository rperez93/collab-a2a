"""Each person's colour in the participants panel, whatever the theme.

The colour identified people in the conversation and not in the list of people —
which is the one place whose entire job is telling them apart.
"""
from __future__ import annotations

import types

import pytest

from collab.client import tui


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(tui, "_pair_for", lambda v: 900)
    tui._CHOSEN.clear()
    tui._SLOTS.clear()
    yield
    tui._CHOSEN.clear()
    tui._SLOTS.clear()


def roster(people, width=100):
    model = types.SimpleNamespace(
        snapshot={"participants": list(people)},
        profile=types.SimpleNamespace(name="me"),
        participants=lambda: list(people),
        roster_is_current=lambda: True,
        snapshot_age=lambda: "just now",
    )
    return tui.roster_rows(model, width)


PEOPLE = [
    {"name": "alice", "connected": True, "focus": "the viewer", "meta": {}},
    {"name": "bob", "connected": False, "focus": "the hub", "meta": {}},
]


def test_the_name_carries_the_person_s_colour(monkeypatch):
    rows = roster(PEOPLE)
    names = [r for r in rows if "alice" in r.text or "bob" in r.text]
    assert names, "nobody was listed"
    for r in names:
        assert r.edge, f"no colour on {r.text.strip()[:24]!r}"
        assert r.head > 0, "nothing was marked to paint in that colour"


def test_two_people_get_two_colours():
    """A palette that gave everyone the same colour would list nothing."""
    rows = roster(PEOPLE)
    mine = next(r for r in rows if "alice" in r.text)
    theirs = next(r for r in rows if "bob" in r.text)
    assert mine.edge != theirs.edge


def test_a_chosen_colour_wins_over_the_dealt_one():
    """`collab color` has to show up here too, or it is not global."""
    tui.record_colours([{"name": "alice", "color": "#00cccc"}])
    rows = roster(PEOPLE)
    mine = next(r for r in rows if "alice" in r.text)
    assert mine.edge == 900, "the chosen colour did not reach the roster"


def test_the_head_covers_the_dot_and_the_name_but_not_the_state():
    """Two meanings on one line, and each keeps its own.

    Whether someone is online is not a matter of who they are: a dot carrying
    both would tell you neither.
    """
    rows = roster(PEOPLE)
    r = next(r for r in rows if "alice" in r.text)
    head = r.text[:r.head]
    assert "alice" in head
    assert "●" in head or "○" in head
    assert "online" not in head
    assert "the viewer" not in head


def test_the_description_line_belongs_to_the_person_too():
    rows = roster(PEOPLE)
    i = next(i for i, r in enumerate(rows) if "alice" in r.text)
    detail = rows[i + 1]
    assert detail.edge == rows[i].edge
    assert detail.head == len(detail.text)


def test_the_state_colour_still_says_online_or_offline():
    """The control: adding identity must not cost the availability signal."""
    rows = roster(PEOPLE)
    mine = next(r for r in rows if "alice" in r.text)
    theirs = next(r for r in rows if "bob" in r.text)
    assert mine.pair != theirs.pair, "online and offline look the same now"
