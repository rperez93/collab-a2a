"""Tests written against surviving mutants.

A mutation pass changed 67 lines one at a time and ran the suite after each.
21 died; 46 lived — meaning the suite could not tell the mutated code from the
real thing. A line nothing notices changing is a line nothing is protecting.

These cover the survivors worth covering, and each names the mutation it kills
so that anyone deleting one knows what they are giving up. Several guard a
promise the code makes about itself in a comment, which is the worst kind of
line to leave untested: the comment says it was fixed and nothing checks.
"""
from __future__ import annotations

import types

import pytest

from collab import config, themes
from collab.client import tui


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "themes"
    d.mkdir()
    monkeypatch.setattr(themes, "user_themes_dir", lambda home=None: d)
    themes._MD_CACHE.clear()
    tui._THEME_CACHE.clear()
    yield d
    themes._MD_CACHE.clear()
    tui._THEME_CACHE.clear()


def write(d, name, text):
    (d / f"{name}.md").write_text(text, encoding="utf-8")
    themes._MD_CACHE.clear()


# --- the format's central promise -------------------------------------------

@pytest.mark.parametrize("fence", ["python", "bash", "json", "", "text"])
def test_only_a_theme_block_counts_as_settings(folder, fence):
    """Kills: `elif label in ("theme", …)` → `elif True`.

    Reading Rule 2 is the whole reason this format is usable, and a mutation
    that made EVERY fenced block count survived the entire suite. A theme file
    explaining itself with a shell snippet would have had that snippet read as
    settings.
    """
    write(folder, "t", f"# Notes\n\n```{fence}\nfold: 9\n```\n")
    assert themes.resolve("t", folder)["fold"] != 9, \
        f"a ```{fence} block was read as settings"


def test_a_theme_block_still_counts(folder):
    """The control: the fix must not cost the case that is meant to work."""
    write(folder, "t", "# Notes\n\n```theme\nfold: 9\n```\n")
    assert themes.resolve("t", folder)["fold"] == 9


# --- values in a hand-written file are forgiving ----------------------------

@pytest.mark.parametrize("written,expected", [
    ("true", True), ("TRUE", True), ("  True  ", True), ("yes", True),
    ("false", False), ("FALSE", False), ("  off ", False),
])
def test_booleans_survive_capitals_and_spaces(folder, written, expected):
    """Kills: the bool branch's `.strip().lower()`.

    People write `tones: TRUE` in a file they are editing by hand, and a theme
    that rejects it is a theme that looks broken for no reason they can see.
    """
    write(folder, "t", f"---\nlayout: bubbles\ntones: {written}\n---\n")
    assert themes.resolve("t", folder)["tones"] is expected
    # AND validate() DIRECTLY. Going only through the file leaves
    # validate's own text branch unreached — _parse_value has already
    # turned "TRUE" into a bool by then — so a mutation there survived.
    # resolve() is called with hand-built dicts too, and that is the
    # path this covers.
    good, warning = themes.validate("tones", written)
    assert warning is None and good is expected


@pytest.mark.parametrize("written", ["log", "LOG", "  Log  "])
def test_options_survive_capitals_and_spaces(folder, written):
    """Kills: the option branch's `.strip().lower()`."""
    write(folder, "t", f"---\nlayout: {written}\n---\n")
    assert themes.resolve("t", folder)["layout"] == "log"


def test_an_empty_value_is_ignored_not_stored_as_a_string(folder):
    """Kills: dropping `""` from the null list.

    `fold:` with nothing after it would otherwise reach validate() as the
    string "" and earn a warning nobody deserves.
    """
    write(folder, "t", "---\nlayout: bubbles\nfold:\n---\n")
    assert themes.resolve("t", folder)["fold"] == themes.DEFAULTS["fold"]
    # THE SILENCE IS THE POINT, and asserting only the value missed it: an
    # empty line is not a mistake, so it must not produce a warning. Without
    # "" in the null list it reaches validate() as a string and earns one.
    assert themes.load_md_themes(folder)[1] == [], "an empty line warned"


# --- resolve() must never raise: an exception here kills the chat ------------

def test_resolving_an_unknown_theme_returns_defaults(folder):
    """Kills: `if theme is None or current in seen` → `and`.

    That mutation raises AttributeError, which comes out of curses.wrapper
    alive. `_current_theme` calls this on every frame with whatever name is in
    the config — including one whose file has just been deleted.
    """
    r = themes.resolve("nothing-by-this-name", folder)
    assert r["layout"] in ("bubbles", "log")
    assert r["fold"] == themes.DEFAULTS["fold"]


def test_resolving_a_theme_with_an_unknown_key_does_not_raise():
    """Kills: `if warning and key in DEFAULTS` → `if warning`.

    resolve() is also called with dicts built by hand — by tests, by other
    code — and one carrying a key it does not know must degrade, not raise.
    """
    themes.BUILTIN["_probe"] = {"layout": "log", "not_a_setting": 1}
    try:
        assert themes.resolve("_probe", None)["layout"] == "log"
    finally:
        themes.BUILTIN.pop("_probe", None)


def test_a_theme_that_disappears_falls_back(folder, tmp_path, monkeypatch):
    """Kills: `return t if t in theme_names() else DEFAULT_THEME` → `return t`.

    Delete the file your theme lives in and the config still names it. Without
    the check the viewer keeps resolving a name that no longer exists.
    """
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: cfg)
    config._CACHE.clear()
    write(folder, "gone", "---\nfold: 5\n---\n")
    assert config.set_theme("gone") == "gone"
    (folder / "gone.md").unlink()
    themes._MD_CACHE.clear()
    config._CACHE.clear()
    assert config.theme() == config.DEFAULT_THEME


# --- colours: the forms people actually type --------------------------------


@pytest.mark.parametrize("written", ["١٢٣", "১২৩৪৫৬", "٣"])
def test_non_ascii_digits_are_refused(written):
    """Kills: `all("0" <= ch <= "9" …)` → `v.isdigit()`, and the hex guard.

    `int()` accepts Arabic-Indic and Bengali digits, so «١٢٣» came out as
    palette index 123 and «#১২৩৪৫৬» as a legitimate colour. Nobody meant
    either, and the comments above both lines say so.
    """
    assert config.parse_color(written) is None


@pytest.mark.parametrize("written", ["  #00cccc  ", "#00CCCC", " 00cccc"])
def test_hex_survives_surrounding_space(written):
    assert config.parse_color(written) == "#00cccc"


def test_a_hex_that_is_too_long_is_refused():
    """Kills: `if len(v) != 6` → `< 6`."""
    assert config.parse_color("#00cccc00") is None


# --- a literal colour in a theme has to be resolved -------------------------


@pytest.mark.parametrize("var", ["$GOOD", "$good", "$Good"])
def test_theme_variables_are_case_insensitive(var):
    """Kills: `_VARS.get(value.upper())` → `_VARS.get(value)`.

    `$good` silently painted as body text instead of green — a theme that
    looks right and is not.
    """
    assert tui._theme_colour(var, "someone") == tui.C_GOOD


def test_an_unknown_variable_falls_back_to_text():
    assert tui._theme_colour("$NOT_A_VARIABLE", "someone") == tui.C_TEXT


# --- the roster ---------------------------------------------------------------

def _roster(people, width=100):
    model = types.SimpleNamespace(
        snapshot={"participants": list(people)},
        profile=types.SimpleNamespace(name="me", participant_id="p_me"),
        participants=lambda: list(people),
        roster_is_current=lambda: True,
        snapshot_age=lambda: "just now",
    )
    return tui.roster_rows(model, width)


def test_the_roster_says_online_for_someone_online(monkeypatch):
    """Kills: `"online" if online else "offline"` → inverted.

    Trivial, and the single most misleading thing the panel could get wrong.
    """
    monkeypatch.setattr(tui, "_pair_for", lambda v: 900)
    rows = _roster([{"name": "up", "connected": True, "id": "p1", "meta": {}},
                    {"name": "down", "connected": False, "id": "p2", "meta": {}}])
    up = next(r for r in rows if "up" in r.text)
    down = next(r for r in rows if "down" in r.text)
    assert "online" in up.text and "offline" not in up.text
    assert "offline" in down.text


def test_an_offline_person_first_does_not_break_the_roster(monkeypatch):
    """Kills: `if not online and (seen := ago(...))` → `or`.

    With `or`, the walrus never binds for the first person when they are
    online, and the next offline row raises NameError — the panel dies on a
    roster whose order nobody chose.
    """
    monkeypatch.setattr(tui, "_pair_for", lambda v: 900)
    rows = _roster([
        {"name": "gone", "connected": False, "id": "p1", "meta": {},
         "last_seen": 0},
        {"name": "here", "connected": True, "id": "p2", "meta": {}},
    ])
    assert any("gone" in r.text for r in rows)
    assert any("here" in r.text for r in rows)


# --- curses pairs are a finite resource -------------------------------------

def test_the_same_colour_is_not_allocated_twice(monkeypatch):
    """Kills: `if color not in _PARES_LIBRES` → `if True`.

    Without the cache a fresh curses pair is allocated on every redraw, and a
    terminal runs out within seconds of a live session. It fails slowly, which
    is why nothing noticed.
    """
    calls: list = []
    monkeypatch.setattr(tui.curses, "init_pair",
                        lambda *a: calls.append(a) or None)
    monkeypatch.setattr(tui, "_colour_index", lambda v: 123)
    tui._PARES_LIBRES.pop(123, None)
    for _ in range(20):
        tui._pair_for("#123456")
    assert len(calls) == 1, f"allocated {len(calls)} pairs for one colour"
