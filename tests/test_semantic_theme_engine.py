"""Appearance controls must be live, bounded and independent of behavior."""
import curses
import os
import time

import pytest

from collab import themes
from collab.client import tui


def test_new_themes_expose_every_appearance_key_without_changing_classic(folder):
    for name in ("classic", "cyberpunk", "matrix"):
        resolved = themes.resolve(name, folder)
        assert set(resolved) == set(themes.KEYS) == set(themes.TYPES)
        parsed, warnings = themes.parse_md(themes.template(name, resolved))
        assert not warnings and parsed == resolved
    assert themes.resolve("classic", folder)["background"] == "default"
    assert themes.resolve("classic", folder)["text"] == "$DEFAULT_COLOR"
    assert themes.resolve("matrix", folder)["status_bg"] != themes.resolve("cyberpunk", folder)["status_bg"]


@pytest.mark.parametrize("key,value", [("background", "not-a-colour"), ("background", "$SPEAKER"),
    ("foreground", "\x1b[31m"), ("divider_char", "\u0301"), ("divider_char", "\u093e"),
    ("divider_char", "界"), ("scrollbar_chars", "a\tb"), ("chars", "abcde\n"),
    ("fold", float("inf")), ("roster_spacing", -1), ("roster_columns", "execute")])
def test_invalid_appearance_values_are_reported_before_rendering(key, value):
    assert themes.validate(key, value)[1]


def test_theme_files_are_bounded_and_a_fifo_never_waits_for_a_writer(folder):
    os.mkfifo(folder / "pipe.md")
    (folder / "huge.md").write_bytes(b"x" * (themes.MAX_THEME_BYTES + 1))
    started = time.monotonic()
    found, warnings = themes.load_md_themes(folder)
    assert not found and len(warnings) == 2
    assert "regular" in " ".join(warnings) and "256 KiB" in " ".join(warnings)
    assert time.monotonic() - started < 1
    for i in range(themes.MAX_THEME_FILES):
        (folder / f"{i}.md").write_text("---\nfold: 2\n---")
    assert "128" in themes.load_md_themes(folder)[1][0]


def test_palette_updates_once_per_generation_and_drops_old_dynamic_pairs(folder, monkeypatch):
    selected = ["cyberpunk"]
    monkeypatch.setattr(tui, "theme", lambda: selected[0])
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 256, raising=False)
    monkeypatch.setattr(curses, "can_change_color", lambda: False)
    pairs = []
    monkeypatch.setattr(curses, "init_pair", lambda *args: pairs.append(args))
    monkeypatch.setattr(tui, "_PALETTE_VERSION", [None])
    monkeypatch.setattr(tui, "_THEME_POLLING", [False])
    tui._apply_theme_palette()
    count = len(pairs)
    for _ in range(20):
        tui._apply_theme_palette()
    assert len(pairs) == count
    tui._pair_for("#ba9876")
    assert tui._PAIRS_BY_COLOUR
    selected[0] = "matrix"
    tui._apply_theme_palette()
    assert not tui._PAIRS_BY_COLOUR
    assert tui._NEXT_PAIR[0] == tui.C_SPEAKER_BASE + 40
    assert dict((pair, (fg, bg)) for pair, fg, bg in pairs)[tui.C_SELECTION][0] != dict((pair, (fg, bg)) for pair, fg, bg in pairs)[tui.C_SELECTION][1]


@pytest.mark.parametrize("colours", [0, 8, 256])
def test_palette_survives_monochrome_and_limited_colour_terminals(folder, monkeypatch, colours):
    monkeypatch.setattr(curses, "COLORS", colours, raising=False)
    monkeypatch.setattr(curses, "can_change_color", lambda: False)
    monkeypatch.setattr(tui, "theme", lambda: "matrix")
    monkeypatch.setattr(tui, "_PALETTE_VERSION", [None])
    tui._deal_colours(colours)
    called = []
    def pair(number, fg, bg):
        assert -1 <= fg < colours and -1 <= bg < colours
        called.append(number)
    monkeypatch.setattr(curses, "init_pair", pair)
    tui._apply_theme_palette()
    assert bool(called) == bool(colours)


def test_active_palette_polls_theme_files_at_most_four_times_per_second(folder, monkeypatch):
    monkeypatch.setattr(tui, "_THEME_POLLING", [True])
    monkeypatch.setattr(tui, "theme", lambda: "classic")
    clock = [100.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    calls = []
    original = themes.load_md_themes
    def load(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(themes, "load_md_themes", load)
    tui._current_theme()
    initial = len(calls)
    for _ in range(100):
        tui._current_theme()
    assert len(calls) == initial
    clock[0] += .3
    tui._current_theme()
    assert len(calls) == initial + 1
