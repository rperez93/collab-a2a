"""Your own colour: how it can be written, and which name it ends up under.

The two bugs these cover looked identical from outside — "I have a global colour
and it does not show up in the chat" — and had nothing to do with each other.
That is why they sit together: the symptom does not tell the causes apart, and
neither does whoever reports it.
"""
from __future__ import annotations

import types

import pytest

from collab import config
from collab.client import tui


# --- the ways a colour can be written ---------------------------------------

@pytest.mark.parametrize("written", [
    "#00cccc", "#00CCCC", "  #00cccc  ", "#0cc", "#0CC", "00cccc",
])
def test_the_same_colour_written_several_ways(written):
    """Hex, in the forms people paste it.

    With or without the `#`, in either case, and the three-digit short form
    half the stylesheets produce — `#0cc` is `#00cccc` with each digit doubled,
    not a half-written hex.
    """
    assert config.parse_color(written) == "#00cccc"


@pytest.mark.parametrize("written", [
    "teal", "rgb(0,204,204)", "hsl(180,100%,40%)", "37", "0", "255",
])
def test_anything_that_is_not_hex_is_refused(written):
    """A colour is a hex triplet and nothing else.

    A table of names inside the tool is a table somebody has to keep, and it
    answers a question it was never asked: `teal` is one colour here, another
    in CSS, another again on the next machine. rgb() and hsl() went with the
    names — a conversion inside a settings parser is a second place for a
    colour to come out slightly wrong.

    Whoever knows the name and not the number looks the hex up; so can an agent
    doing it on their behalf.
    """
    assert config.parse_color(written) is None


@pytest.mark.parametrize("bad", [
    "#00cc", "#gggggg", "#00cccc00", "", "   ", "#١٢٣", "١٢٣",
])
def test_what_cannot_be_read_is_refused_rather_than_approximated(bad):
    """A mis-typed colour has to warn, not land on something close.

    If it fell to the nearest readable thing, whoever wrote it would be left
    convinced they have the colour they asked for.
    """
    assert config.parse_color(bad) is None


# --- which name it is registered under --------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    monkeypatch.setattr(tui, "_pair_for", lambda v: 99)
    config._CACHE.clear()
    tui._CHOSEN.clear()
    # The viewer remembers the global name for a couple of seconds — resolving
    # it costs two `git` calls and it is asked for once per message. A test
    # that changes the name has to say so, like the config cache above.
    tui._OWN_NAME.clear()
    yield path
    config._CACHE.clear()
    tui._CHOSEN.clear()
    tui._OWN_NAME.clear()


def _participants(session_name, roster=()):
    model = types.SimpleNamespace(
        snapshot={"participants": list(roster)},
        profile=types.SimpleNamespace(name=session_name),
        events=[],
    )
    return tui.Model.participants.__get__(model, tui.Model)


def test_my_colour_also_covers_the_name_i_used_to_sign_with(cfg, monkeypatch):
    """The hub suffixes you when your name is taken.

    You join as `alice-tmp`, your older messages are still signed `alice`,
    and a colour registered only under the session name does not reach them:
    your own messages come out in the dealt colour and it looks as though the
    global setting does nothing. That is what was happening.
    """
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    config.set_default_color("#00cccc")
    _participants("alice-tmp")()
    assert tui._CHOSEN.get("alice-tmp") == "#00cccc"
    assert tui._CHOSEN.get("alice") == "#00cccc"


def test_a_roster_that_says_nothing_does_not_erase_my_colour(cfg, monkeypatch):
    """`meta.color = None` means "not published yet", not "has none".

    The roster carries that None for everyone who has not published anything —
    which is everyone at the start. Reading it as a refusal wiped the local
    colour right after it was seeded.
    """
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    config.set_default_color("#00cccc")
    _participants("alice-tmp", roster=[{"name": "alice", "meta": {}},
                                         {"name": "bob", "meta": {}}])()
    assert tui._CHOSEN.get("alice") == "#00cccc"
    assert "bob" not in tui._CHOSEN


def test_a_published_colour_does_win_over_the_local_one(cfg, monkeypatch):
    """What is published is what everyone else sees, so it is what I must see."""
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    config.set_default_color("#00cccc")
    _participants("alice-tmp",
                  roster=[{"name": "alice", "meta": {"color": "#ff7f50"}}])()
    assert tui._CHOSEN.get("alice") == "#ff7f50"


def test_clearing_the_colour_clears_it_from_all_my_names(cfg, monkeypatch):
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    config.set_default_color("#00cccc")
    call = _participants("alice-tmp")
    call()
    config.set_default_color(None)
    call()
    assert tui._CHOSEN == {}


def test_with_no_global_name_none_is_invented(cfg, monkeypatch):
    monkeypatch.setattr(tui, "resolve_name", lambda: "")
    config.set_default_color("#008080")
    _participants("alice-tmp")()
    assert list(tui._CHOSEN) == ["alice-tmp"]


def test_if_resolving_the_global_name_fails_the_viewer_stays_up(cfg, monkeypatch):
    def explode():
        raise OSError("git is not here")
    monkeypatch.setattr(tui, "resolve_name", explode)
    config.set_default_color("#008080")
    _participants("alice-tmp")()
    assert tui._CHOSEN.get("alice-tmp") == "#008080"
