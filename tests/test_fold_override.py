"""`collab fold` — folding is yours, not the theme's.

A theme decides how a conversation LOOKS, and folding is part of that, so every
theme carries a `fold`. But the person reading is the one who knows whether
they want four lines or the whole message, and making them edit a shared theme
file to say so means editing something they may have been given.

So it sits where `collab color` sits: a setting of yours that applies whichever
theme is on. The theme still names a default; this overrules it.
"""

from __future__ import annotations

import pytest

from collab import config


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A config of our own. These tests write settings, and the real file is
    the user's: a test that edits it is a test that changes their viewer."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    config._CACHE.clear()
    yield tmp_path
    config._CACHE.clear()


# --- the setting itself ------------------------------------------------------

def test_nothing_is_set_until_somebody_sets_it():
    """No override is not the same as an override of zero. Zero means «never
    fold», which is a thing a person can choose; None means the theme decides,
    and collapsing the two makes `collab fold auto` unable to say anything."""
    assert config.fold_override() is None


def test_a_number_is_kept():
    config.set_fold_override(8)
    assert config.fold_override() == 8


def test_off_is_stored_as_zero_and_survives_a_reload():
    """`off` is a real value, not the absence of one — it has to outlast the
    process that wrote it, or the folding comes back on the next redraw."""
    config.set_fold_override(0)
    assert config.fold_override() == 0


def test_auto_clears_it_and_hands_the_decision_back():
    config.set_fold_override(6)
    config.set_fold_override(None)
    assert config.fold_override() is None


def test_a_value_that_makes_no_sense_is_refused_not_rounded():
    """The same rule the theme parser follows: what cannot be understood is
    reported, never approximated. Someone who typed a mistake has to hear it
    rather than get a folding they did not ask for."""
    for bad in (-1, "six", "", 10_001):
        with pytest.raises(ValueError):
            config.set_fold_override(bad)
    assert config.fold_override() is None


# --- what the viewer does with it --------------------------------------------

def _rows(monkeypatch, declared_fold, override):
    """The fold the renderer would use, asked of the renderer."""
    from collab import themes
    from collab.client import tui as _t

    resolved = dict(themes.DEFAULTS) | {"fold": declared_fold}
    monkeypatch.setattr(_t, "_current_theme", lambda: resolved)
    monkeypatch.setattr(_t, "fold_override", lambda: override)
    return _t.effective_fold()


def test_the_theme_decides_when_nothing_is_set(monkeypatch):
    assert _rows(monkeypatch, declared_fold=4, override=None) == 4


def test_the_override_beats_the_theme(monkeypatch):
    assert _rows(monkeypatch, declared_fold=4, override=9) == 9


def test_an_override_of_zero_beats_a_theme_that_folds(monkeypatch):
    """The case that a falsy check gets wrong, and the reason the override is
    None-or-a-number rather than a number with zero standing in for «unset»."""
    assert _rows(monkeypatch, declared_fold=4, override=0) == 0


def test_a_theme_of_zero_is_not_overridden_by_absence(monkeypatch):
    """A theme file that says `fold: 0` has asked for no folding. Nobody
    setting an override must not turn its folding on."""
    assert _rows(monkeypatch, declared_fold=0, override=None) == 0


# --- the command -------------------------------------------------------------

def _run(argv):
    from collab.cli import build_parser

    args = build_parser().parse_args(argv)
    return args.func(args)


def test_the_command_exists_and_takes_the_three_forms():
    """`chat.md` in the wild has documented `collab fold <n|off|auto>` for
    longer than the command has existed. These are the three it promises."""
    assert _run(["fold", "8"]) == 0
    assert config.fold_override() == 8

    assert _run(["fold", "off"]) == 0
    assert config.fold_override() == 0

    assert _run(["fold", "auto"]) == 0
    assert config.fold_override() is None


def test_asking_with_no_value_changes_nothing(capsys):
    config.set_fold_override(5)
    assert _run(["fold"]) == 0
    assert config.fold_override() == 5
    assert "5" in capsys.readouterr().out


def test_a_bad_value_is_refused_and_leaves_the_old_one_standing(capsys):
    config.set_fold_override(5)
    assert _run(["fold", "six"]) == 2
    assert config.fold_override() == 5, "a refused command changes nothing"
