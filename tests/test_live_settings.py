"""A visual setting is changed in one place and shows up in every open pane.

This is not a convenience: without it `collab theme classic` looks like it does
nothing — the command says ok and the screen does not move — and the next thing
anyone does is run it again rather than restart the viewer.

It is tested against the same calls the draw loop makes on every frame, because
those are what decide whether the change lands.
"""
from __future__ import annotations

import json
import types

import pytest

from collab import config, themes
from collab.client import tui


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A throwaway global config and themes folder.

    The folder is redirected as well as the config file. `set_theme` refuses a
    name `theme_names()` does not know, and `theme_names()` reads the themes
    folder — so left pointing at the real one, whether these tests pass would
    depend on which .md files the person running them happens to keep in
    ~/.config/collab/themes.

    `other` exists so there is a second name to switch to: `classic` is the
    only theme that ships, and a test that moves between two themes needs two.
    """
    path = tmp_path / "config.json"
    folder = tmp_path / "themes"
    folder.mkdir()
    (folder / "other.md").write_text("---\nlayout: bubbles\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    monkeypatch.setattr(themes, "user_themes_dir", lambda home=None: folder)
    themes._MD_CACHE.clear()
    config._CACHE.clear()
    yield path
    config._CACHE.clear()
    themes._MD_CACHE.clear()


# --- the config is re-read when the file changes ----------------------------

def test_a_change_from_outside_is_seen_without_restarting(cfg):
    """Another process writes the config; this one reads it knowing nothing."""
    config.set_theme("other")
    assert config.theme() == "other"
    cfg.write_text(json.dumps({"theme": "classic"}), encoding="utf-8")
    assert config.theme() == "classic", "the cache kept the old value"


def test_two_changes_in_the_same_second_are_not_lost(cfg):
    """mtime has one-second resolution on some filesystems.

    `collab color #008080 && collab theme classic` lands inside the same second
    without trying. If the stamp were mtime alone, the second change would be
    lost.
    """
    config.set_default_color("#008080")
    config.set_theme("classic")
    assert config.theme() == "classic"
    assert config.default_color() == "#008080"


def test_the_file_is_not_re_read_when_nothing_changed(cfg, monkeypatch):
    """The other half: reading four settings per frame cannot mean disk."""
    config.set_theme("other")
    config.theme()                                   # fills the cache
    reads = []
    real = type(cfg).read_text
    monkeypatch.setattr(type(cfg), "read_text",
                        lambda self, *a, **k: (reads.append(1),
                                               real(self, *a, **k))[1])
    for _ in range(20):
        config.theme(), config.default_color()
    assert reads == [], f"{len(reads)} disk reads too many"


# --- a theme of your own can be chosen, not merely resolved -----------------

def test_a_user_theme_can_be_selected(cfg, tmp_path, monkeypatch):
    """Resolving one without being able to choose it left the folder as decoration."""
    folder = tmp_path / "own-themes"          # its own, not the fixture's
    folder.mkdir()
    (folder / "mine.md").write_text("---\nlayout: bubbles\n---\n", encoding="utf-8")
    monkeypatch.setattr(themes, "user_themes_dir", lambda home=None: folder)
    themes._MD_CACHE.clear()
    assert "mine" in config.theme_names()
    assert config.set_theme("mine") == "mine"
    assert config.theme() == "mine"


def test_a_theme_that_does_not_exist_is_refused(cfg):
    assert config.set_theme("made-up") is None
    assert config.theme() == config.DEFAULT_THEME


# --- my own colour, re-read like everyone else's ----------------------------

def _participants(name="alice", roster=()):
    model = types.SimpleNamespace(
        snapshot={"participants": list(roster)},
        profile=types.SimpleNamespace(name=name),
        events=[],
    )
    return tui.Model.participants.__get__(model, tui.Model)


@pytest.mark.parametrize("value,expected", [("#008080", "#008080"), ("#00cccc", "#00cccc")])
def test_my_colour_lands_without_restarting_the_viewer(cfg, value, expected,
                                                       monkeypatch):
    monkeypatch.setattr(tui, "_pair_for", lambda v: 99)
    tui._CHOSEN.clear()
    call = _participants()
    call()
    assert tui._CHOSEN.get("alice") is None
    config.set_default_color(config.parse_color(value))
    call()                                            # the next frame
    assert tui._CHOSEN.get("alice") == expected


def test_clearing_my_colour_also_lands_without_restarting(cfg, monkeypatch):
    """Setting without clearing is half a feature: you have to get back to random."""
    monkeypatch.setattr(tui, "_pair_for", lambda v: 99)
    tui._CHOSEN.clear()
    config.set_default_color("#008080")
    call = _participants()
    call()
    assert tui._CHOSEN.get("alice") == "#008080"
    config.set_default_color(None)
    call()
    assert tui._CHOSEN.get("alice") is None


def test_what_is_published_wins_over_what_is_local(cfg, monkeypatch):
    """If the roster carries another colour for me, the roster wins.

    That is right: what is published is what everyone else sees, and my screen
    has to show me the same thing they get — not what I just typed and has not
    travelled yet.
    """
    monkeypatch.setattr(tui, "_pair_for", lambda v: 99)
    tui._CHOSEN.clear()
    config.set_default_color("#008080")
    _participants(roster=[{"name": "alice", "meta": {"color": "#ff7f50"}}])()
    assert tui._CHOSEN.get("alice") == "#ff7f50"
