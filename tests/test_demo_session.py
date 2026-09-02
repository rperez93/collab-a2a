"""The simulated session: what it has to contain to be worth looking at.

A demo whose conversation is three lines of «hello» proves nothing — the things
that go wrong in this viewer go wrong on a message long enough to fold, on a
line the tone rules paint, on a name in Japanese, on a history longer than the
window. So these tests are about the SHAPES the script has to contain, not
about the prose in it: change the wording freely, lose one of these and the
demo stops being able to show the thing it exists to show.
"""

from __future__ import annotations

import pytest

from collab import demo, themes
from collab.client import tui
from collab.client.tui import conversation_rows, roster_rows
from collab.protocol import (KIND_CHAT, KIND_FILE, KIND_HELLO, KIND_PRESENCE,
                             KIND_TASK)


@pytest.fixture()
def built_in(folder):
    """Only the themes that ship, in a home with no user themes in it."""
    return sorted(themes.all_themes(folder=folder))


# --- it runs on nothing ------------------------------------------------------

def test_it_needs_no_session_and_no_files(tmp_path, monkeypatch):
    """The whole point: no hub, no daemon, no state directory."""
    missing = tmp_path / "nowhere"
    monkeypatch.setenv("COLLAB_HOME", str(missing))

    model = demo.model()
    model.load_initial(limit=5)

    assert model.events, "the viewer opened on something"
    assert not missing.exists(), "and wrote nothing to disk"


def test_the_viewer_believes_it_is_live(monkeypatch, tmp_path):
    """Otherwise the badge reads «offline» and the roster refuses to say who is
    here — the two things a demo of the roster most needs to show."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "nowhere"))
    model = demo.model()
    model.refresh_side()

    assert model.state() == "live"
    assert model.roster_is_current()


# --- what the script has to contain -----------------------------------------

def test_it_is_longer_than_the_window():
    """So scrolling back has somewhere to go and the paging code runs."""
    assert len(demo.events()) > tui.WINDOW


@pytest.mark.parametrize("kind", [KIND_CHAT, KIND_HELLO, KIND_PRESENCE,
                                  KIND_TASK, KIND_FILE])
def test_every_kind_the_renderer_special_cases_is_in_it(kind):
    """`_body_lines` has a branch per kind; a demo that never reaches one is a
    demo that cannot show it is broken."""
    assert any(e.kind == kind for e in demo.events())


@pytest.mark.parametrize("layout,fold", [("bubbles", 4), ("log", 1),
                                         ("log", 2), ("log", 8)])
def test_something_in_it_is_long_enough_to_fold(monkeypatch, layout, fold):
    """A message long enough that ANY theme which folds draws a button on it.

    THE FOLDING IS PINNED HERE, and it has to be. `conversation_rows` reads the
    live theme and the reader's own override, and both of those belong to
    whoever is running the tests: a themes folder with a `fold: 0` file, or a
    `collab fold off`, and nothing folds and the assertion cannot pass. A test
    that reports the tester's configuration is not reporting the demo.
    """
    resolved = dict(themes.DEFAULTS) | {"layout": layout, "fold": fold}
    monkeypatch.setattr(tui, "_current_theme", lambda: resolved)
    monkeypatch.setattr(tui, "fold_override", lambda: None)

    rows = conversation_rows(demo.events(), 80, demo.YOU)
    assert any(r.button for r in rows), "no «show more» to click"


def test_the_theme_that_ships_folds_the_long_messages_and_only_those(monkeypatch):
    """The shipped experience, said out loud rather than discovered.

    `classic` folds now, and the number it folds at was chosen against this
    script — see `themes.FOLD` for the figures. At eighty columns only the two
    file dumps fold. On the panes the viewer is actually opened in — `collab
    watch --tmux`, 35 % of the terminal, 27 to 41 columns — the log layout's
    body is a handful of columns and ordinary messages run to seven lines, so
    the share folded there is the number that decides: a fold of four took
    two-thirds of the messages at 40 columns, six took 40 %, eight takes 18 %.
    This pins both halves: there IS a «show more» on the demo, and it is on a
    minority of what was said, at the ordinary width and at the narrow ones.
    """
    resolved = dict(themes.DEFAULTS) | themes.BUILTIN["classic"]
    monkeypatch.setattr(tui, "_current_theme", lambda: resolved)
    monkeypatch.setattr(tui, "fold_override", lambda: None)

    said = [e for e in demo.events() if e.kind == KIND_CHAT]
    for width, at_most in ((80, 0.10), (27, 0.20), (41, 0.20)):
        rows = conversation_rows(demo.events(), width, demo.YOU)
        folded = {r.seq for r in rows if r.button}
        assert folded, f"nothing folds at {width}: the fold is off"
        share = len(folded) / len(said)
        assert share <= at_most, \
            f"{len(folded)} of {len(said)} messages fold at {width} — that hides the conversation"


def test_the_default_fold_is_the_built_in_theme_s(monkeypatch):
    """One number: a theme file that says nothing about folding, the shipped
    theme, and `collab theme --new`'s template all agree."""
    assert themes.DEFAULTS["fold"] == themes.BUILTIN["classic"]["fold"] == themes.FOLD == 8


@pytest.mark.parametrize("hour,minute", [(0, 1), (0, 30), (7, 0), (13, 45),
                                         (23, 59)])
def test_the_day_changes_partway_through_at_any_hour(hour, minute):
    """The day separator only draws on a boundary, so the script has to cross
    one — AT WHATEVER HOUR the demo is opened. At 00:01 UTC every one of
    today's beats is still on yesterday's date, and a backlog anchored to «a
    day ago» lands on that same date: no boundary, no separator, and nothing to
    tell you the feature is gone."""
    import datetime as dt

    now = dt.datetime(2026, 9, 2, hour, minute, tzinfo=dt.timezone.utc)
    days = {e.ts[:10] for e in demo.events(now=now)}
    assert len(days) >= 2, f"{sorted(days)} at {hour:02d}:{minute:02d}"


def test_the_tone_rules_have_something_to_paint():
    """Good, bad, warning and information lines: four colours that are only
    ever seen when a line happens to match, and never on request."""
    tones = {tui.line_pair(line)
             for e in demo.events()
             for line in (e.text or "").splitlines()}
    for tone in (tui.C_GOOD, tui.C_BAD, tui.C_WARNLINE, tui.C_INFO):
        assert tone in tones, f"nothing in the script paints {tone}"


def test_more_than_one_person_speaks():
    """The speaker colours and the «own side» of the bubble need someone to
    tell apart from you."""
    senders = {e.sender for e in demo.events() if e.sender}
    assert demo.YOU in senders
    assert len(senders) >= 3


def test_a_wide_alphabet_is_in_it():
    """CJK and emoji take two columns each, which is where the bubble maths
    breaks. Measured, not eyeballed: something in the script must be wider
    than its own character count."""
    assert any(tui._w(e.text) > len(e.text) for e in demo.events() if e.text)


# --- it survives every theme -------------------------------------------------

def test_it_renders_under_every_built_in_theme(monkeypatch, built_in):
    for name in built_in:
        monkeypatch.setattr(tui, "theme", lambda name=name: name)
        rows = conversation_rows(demo.events(), 80, demo.YOU)
        assert rows, f"the {name} theme rendered nothing"
        assert all(tui._w(r.text) <= 80 for r in rows), \
            f"the {name} theme drew wider than the pane"


@pytest.mark.parametrize("width", [24, 40, 56, 80, 200])
def test_it_renders_at_every_width(width):
    rows = conversation_rows(demo.events(), width, demo.YOU)
    assert all(tui._w(r.text) <= width for r in rows)


@pytest.mark.parametrize("width", [24, 30, 40, 56, 80, 200])
@pytest.mark.parametrize("layout", ["log", "bubbles"])
def test_it_renders_at_every_width_with_folding_on_too(monkeypatch, layout,
                                                       width):
    """The same claim, with the fold button drawn — which is where it failed.

    `classic` at 40 columns produced a 52-column row: a 31-column indent with a
    21-column «▸ show more (2 lines)» appended raw, while every header row
    beside it went through `_clip`. It never showed because the shipped
    `classic` then had `fold: 0` and this row was never drawn — so the guard
    above, which reads the live theme, only ever measured the unfolded case on
    a stock install.
    """
    resolved = dict(themes.DEFAULTS) | {"layout": layout, "fold": 2}
    monkeypatch.setattr(tui, "_current_theme", lambda: resolved)
    monkeypatch.setattr(tui, "fold_override", lambda: None)

    rows = conversation_rows(demo.events(), width, demo.YOU)
    assert any(r.button for r in rows), "nothing folded — the test proves nothing"
    assert all(tui._w(r.text) <= width for r in rows)


@pytest.mark.parametrize("width", [24, 30, 40])
def test_a_narrow_pane_keeps_the_whole_button_readable(monkeypatch, width):
    """The indent gives way before the label does. A button clipped to
    «▸ show m…» is still clickable and no longer says what it does."""
    resolved = dict(themes.DEFAULTS) | {"layout": "log", "fold": 2}
    monkeypatch.setattr(tui, "_current_theme", lambda: resolved)
    monkeypatch.setattr(tui, "fold_override", lambda: None)

    buttons = [r.text for r in conversation_rows(demo.events(), width, demo.YOU)
               if r.button]
    assert buttons
    assert all("lines)" in b or "line)" in b for b in buttons)


# --- the roster --------------------------------------------------------------

def test_the_roster_says_who_is_here(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "nowhere"))
    model = demo.model()
    model.refresh_side()

    text = "\n".join(r.text for r in roster_rows(model, 80))
    for person in model.participants():
        assert person["name"] in text
    assert "(you)" in text, "and which one is the reader"


# --- the real paging code runs on it ----------------------------------------

def test_reading_back_and_returning_to_the_live_end(monkeypatch, tmp_path):
    """Not a stand-in for the Model: the demo swaps the LOG underneath the real
    one, so windowing, trimming and paging are the shipped code."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "nowhere"))
    model = demo.model()
    model.load_initial(limit=5)

    assert model.more_above(), "there is history behind the opening screen"
    assert model.load_older() > 0

    model.load_start()
    assert not model.more_above()
    assert model.pending() > 0, "and the rest is still ahead"

    model.load_tail()
    assert model.pending() == 0
