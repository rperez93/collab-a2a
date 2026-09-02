"""Clicking «show more».

The button row is the only clickable thing in the conversation, and clicking it
must fold or unfold that message. It stopped doing so when the wheel arrived:
`handle` grew a second mouse branch above the one that folded, so every click
was answered by the wheel handler and the fold code was never reached.

The button is not the chat theme's. ANY theme that folds draws one — the log
layout included — so these tests click the rows the real renderer produced,
under each layout, rather than rows written out by hand.
"""

from __future__ import annotations

import curses

import pytest

from collab.client import tui as tui_mod
from collab.client.tui import Row, conversation_rows
from collab.protocol import KIND_CHAT, Envelope

from test_tui_scroll import _tui


def _click(monkeypatch, tui, *, y, state=curses.BUTTON1_PRESSED):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 10, y, 0, state))
    return tui.handle(curses.KEY_MOUSE)


def _folded(tui, *, rows=3):
    """A conversation whose last row is the «show more» button of message 7."""
    tui._chat_rows = [Row("body", seq=7) for _ in range(rows - 1)]
    tui._chat_rows.append(Row("▸ show more (4 lines)", seq=7, button=True))
    tui.chat.rows, tui.chat.total = rows, rows
    tui.chat.offset = 0
    tui.chat.follow = False
    return tui


# --- the click reaches the fold ---------------------------------------------

def test_clicking_show_more_unfolds_that_message(monkeypatch):
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=8 + 2)          # the button row
    assert tui.expanded == {7}

    _click(monkeypatch, tui, y=8 + 2)          # and it folds back
    assert tui.expanded == set()


def test_clicking_a_body_row_does_nothing(monkeypatch):
    """Reading is not selecting: only the button answers the mouse."""
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=8)
    assert tui.expanded == set()


def test_a_click_over_the_roster_never_folds_a_message(monkeypatch):
    """The row index is counted down from the top of the conversation, so a
    click above it would still land on a row if nobody said otherwise."""
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=2)
    assert tui.expanded == set()


def test_a_click_below_the_last_row_folds_nothing(monkeypatch):
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=8 + 40)
    assert tui.expanded == set()


def test_the_release_of_a_click_does_not_undo_it(monkeypatch):
    """With mouseinterval(0) a click arrives as press then release; folding on
    both would toggle twice and leave the message exactly as it was."""
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=8 + 2, state=curses.BUTTON1_PRESSED)
    _click(monkeypatch, tui, y=8 + 2,
           state=getattr(curses, "BUTTON1_RELEASED", 0))
    assert tui.expanded == {7}


@pytest.mark.parametrize("what", ["BUTTON1_CLICKED", "BUTTON1_DOUBLE_CLICKED",
                                  "BUTTON1_TRIPLE_CLICKED"])
def test_clicking_twice_quickly_is_still_clicking(monkeypatch, what):
    """ncurses does not report two quick clicks as two clicks: it coalesces
    them into ONE event under a different name. Measured on a real terminal —
    the second click of a pair arrived as BUTTON1_DOUBLE_CLICKED and the button
    did nothing, which is what it looks like to somebody clicking impatiently
    at a control that is not responding."""
    state = getattr(curses, what, None)
    if state is None:
        pytest.skip(f"this ncurses has no {what}")
    tui = _folded(_tui(chat_top=8))

    _click(monkeypatch, tui, y=8 + 2, state=state)
    assert tui.expanded == {7}


def test_in_a_single_pane_chat_view_the_button_still_clicks(monkeypatch):
    """The tmux layout shows the conversation alone, one row below the title."""
    tui = _folded(_tui(view="chat", chat_top=1))

    _click(monkeypatch, tui, y=1 + 2)
    assert tui.expanded == {7}


# --- and it reaches it under EVERY theme that folds --------------------------

def _long_message():
    return Envelope(kind=KIND_CHAT, sender="edith", seq=7,
                    text="\n".join(f"line {i}" for i in range(12)),
                    ts="2026-09-01T10:00:00+00:00")


@pytest.mark.parametrize("layout", ["bubbles", "log"])
def test_the_button_folds_under_any_layout(monkeypatch, folder, layout):
    """The fold is a setting, not a look: a log-layout theme that asks for it
    draws the same button, and clicking it has to work the same."""
    (folder / "mine.md").write_text(f"---\nlayout: {layout}\nfold: 3\n---\n",
                                    encoding="utf-8")
    monkeypatch.setattr(tui_mod, "theme", lambda: "mine")

    rows = conversation_rows([_long_message()], 80, "someone-else")
    buttons = [i for i, r in enumerate(rows) if r.button]
    assert buttons, f"the {layout} layout drew no «show more»"

    tui = _tui(chat_top=8)
    tui._chat_rows = rows
    tui.chat.rows, tui.chat.total = len(rows), len(rows)
    tui.chat.offset, tui.chat.follow = 0, False

    _click(monkeypatch, tui, y=8 + buttons[0])
    assert tui.expanded == {7}


def test_a_theme_that_does_not_fold_has_nothing_to_click(monkeypatch, folder):
    (folder / "flat.md").write_text("---\nlayout: log\nfold: 0\n---\n",
                                    encoding="utf-8")
    monkeypatch.setattr(tui_mod, "theme", lambda: "flat")
    # THE READER'S OWN OVERRIDE IS THEIRS, not the test's. `effective_fold`
    # puts `collab fold N` over the theme, and it reads the real global config,
    # so on a machine whose owner had run `collab fold 4` this drew a button
    # over a theme that said `fold: 0` and failed for the tester's settings.
    monkeypatch.setattr(tui_mod, "fold_override", lambda: None)

    rows = conversation_rows([_long_message()], 80, "someone-else")
    assert not any(r.button for r in rows)
    assert sum(1 for r in rows if "line 11" in r.text) == 1, "and all of it shows"
