"""Scrolling the roster.

The roster always *could* be scrolled — tab to it, then the arrow keys. But the
first thing anyone does is turn the wheel, and nothing was listening for it, so
the pane read as frozen. Measured in a real terminal: ncurses grants the mouse
mask and delivers KEY_MOUSE, and without `mousemask` those events never come.
"""

from __future__ import annotations

import curses

import pytest

from collab.client.tui import Pane, Tui, WHEEL_LINES
from collab.config import SessionProfile


class FakeModel:
    """Enough of a Model for the parts of Tui that do not draw."""

    def __init__(self, older=0, newer=0):
        self.profile = SessionProfile(session_id="s", url="u", name="me",
                                      host_name="host", token="t", home="/tmp")
        self.events = []
        self.snapshot = {}
        self.status = {}
        #: What the bottom row asks the model for. Empty rather than absent:
        #: a stand-in that raises AttributeError where the real one answers
        #: «nothing to report» makes every test that draws that row fail for a
        #: reason that has nothing to do with what it was checking.
        self.own_stats: dict = {}
        #: What is on disk either side of the loaded window, and what the
        #: viewer did about it.
        self.older = older
        self.newer = newer
        self.pulls = 0
        self.jumps: list[str] = []

    def more_above(self):
        return self.older > 0

    def pending(self):
        return self.newer

    def load_older(self, count=200):
        if not self.older:
            return 0
        self.pulls += 1
        took = min(count, self.older)
        self.older -= took
        return took

    def load_newer(self, count=200):
        if not self.newer:
            return 0
        self.pulls += 1
        took = min(count, self.newer)
        self.newer -= took
        return took

    def load_tail(self):
        self.jumps.append("tail")
        self.older, self.newer = self.older + self.newer, 0

    def load_start(self):
        self.jumps.append("start")
        self.newer, self.older = self.newer + self.older, 0


def _tui(view="both", chat_top=8):
    tui = Tui(FakeModel(), view=view)
    tui._chat_top = chat_top
    tui.roster.rows, tui.roster.total = 4, 40
    tui.chat.rows, tui.chat.total = 10, 200
    return tui


def _wheel(monkeypatch, tui, *, y, state):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 10, y, 0, state))
    return tui.handle(curses.KEY_MOUSE)


# --- which pane the wheel is over -------------------------------------------

def test_the_wheel_scrolls_the_pane_it_is_over(monkeypatch):
    tui = _tui()
    before = tui.roster.offset

    chat_before = tui.chat.offset
    _wheel(monkeypatch, tui, y=4, state=curses.BUTTON5_PRESSED)
    assert tui.roster.offset == before + WHEEL_LINES
    assert tui.chat.offset == chat_before, "chat untouched"


def test_over_the_conversation_it_scrolls_the_conversation(monkeypatch):
    tui = _tui()
    tui.chat.offset = 50
    tui.chat.follow = False
    roster_before = tui.roster.offset

    _wheel(monkeypatch, tui, y=20, state=curses.BUTTON5_PRESSED)
    assert tui.chat.offset == 53
    assert tui.roster.offset == roster_before


def test_wheel_up_goes_back(monkeypatch):
    tui = _tui()
    tui.roster.offset = 10
    _wheel(monkeypatch, tui, y=4, state=curses.BUTTON4_PRESSED)
    assert tui.roster.offset == 10 - WHEEL_LINES


def test_scrolling_a_pane_focuses_it(monkeypatch):
    """So the keys you reach for next go where you were just looking."""
    tui = _tui()
    assert tui.focus == "chat"
    _wheel(monkeypatch, tui, y=4, state=curses.BUTTON5_PRESSED)
    assert tui.focus == "roster"


def test_a_click_is_not_a_scroll(monkeypatch):
    tui = _tui()
    before = tui.roster.offset
    _wheel(monkeypatch, tui, y=4, state=curses.BUTTON1_PRESSED)
    assert tui.roster.offset == before


def test_a_mouse_event_that_cannot_be_read_is_survivable(monkeypatch):
    """getmouse raises if the queue moved on; that must not end the viewer."""
    tui = _tui()

    def boom():
        raise curses.error("no mouse event")

    monkeypatch.setattr(curses, "getmouse", boom)
    assert tui.handle(curses.KEY_MOUSE) is True


def test_in_a_single_pane_view_the_wheel_always_hits_that_pane(monkeypatch):
    tui = _tui(view="roster")
    _wheel(monkeypatch, tui, y=99, state=curses.BUTTON5_PRESSED)
    assert tui.roster.offset == WHEEL_LINES


# --- keys that reach the roster without taking focus ------------------------

def test_bracket_keys_scroll_the_roster_from_the_conversation():
    tui = _tui()
    assert tui.focus == "chat"

    tui.handle(ord("]"))
    assert tui.roster.offset == 1
    assert tui.focus == "chat", "you did not have to leave the conversation"

    tui.handle(ord("["))
    assert tui.roster.offset == 0


def test_tab_still_works():
    tui = _tui()
    tui.handle(ord("\t"))
    assert tui.focus == "roster"
    tui.handle(curses.KEY_DOWN)
    assert tui.roster.offset == 1


# --- the roster cannot be squeezed out of existence -------------------------

@pytest.mark.parametrize("height", [8, 10, 12, 20, 40])
def test_the_roster_keeps_at_least_one_visible_row(height):
    """At zero visible rows it renders nothing, and a pane you cannot see is a
    pane you cannot scroll."""
    from collab.client.tui import MIN_ROSTER_ROWS, roster_share

    body_height = height - 3
    roster_h = max(int(body_height * roster_share()), MIN_ROSTER_ROWS)
    roster_h = min(roster_h, max(body_height - 4, 2))
    assert roster_h - 1 >= 1, f"roster invisible at height {height}"


def test_a_pane_that_fits_everything_reports_nothing_to_scroll():
    pane = Pane(rows=10, total=4)
    pane.settle()
    assert pane.offset == 0


# --- getting back to the newest ---------------------------------------------

@pytest.mark.parametrize("key", [ord("G"), curses.KEY_END])
def test_end_and_G_both_jump_to_the_newest(key):
    """End is the key people press; G is the one vi users press.

    And «the newest» is what is being said now, not the end of the window: a
    reader fifty messages behind it is asking for the live end, which is the
    whole reason the key exists.
    """
    tui = _tui()
    tui.model.newer = 300
    tui.chat.offset, tui.chat.follow = 0, False

    tui.handle(key)
    assert tui.model.jumps == ["tail"]
    assert tui.chat.offset == max(tui.chat.total - tui.chat.rows, 0)
    assert tui.chat.follow, "and the pane follows what is said next"


@pytest.mark.parametrize("key", [ord("g"), curses.KEY_HOME])
def test_home_and_g_both_go_to_the_top(key):
    """And «the top» is the start of the conversation, not of the window."""
    tui = _tui()
    tui.model.older = 300
    tui.chat.offset = 100

    tui.handle(key)
    assert tui.model.jumps == ["start"]
    assert tui.chat.offset == 0
    assert not tui.chat.follow


def test_half_page_keys_move_half_a_pane():
    tui = _tui()
    tui.chat.offset, tui.chat.follow = 100, False

    tui.handle(4)                      # ctrl-D
    assert tui.chat.offset == 105
    tui.handle(21)                     # ctrl-U
    assert tui.chat.offset == 100


# --- history that is on disk but not on screen ------------------------------

def test_reaching_the_top_pulls_in_older_messages():
    """The viewer opens on the last hundred; the rest is not lost, just not
    loaded, and the top of the screen is where you ask for it."""
    tui = _tui()
    tui.model.older = 500
    tui.chat.offset, tui.chat.follow = 1, False

    tui.handle(curses.KEY_UP)          # lands on 0
    assert tui.model.pulls == 1, "more history was fetched"


def test_older_messages_are_not_fetched_while_you_are_reading_the_middle():
    tui = _tui()
    tui.model.older = 500
    tui.chat.offset, tui.chat.follow = 50, False

    tui.handle(curses.KEY_UP)
    assert tui.model.pulls == 0


def test_the_top_key_reaches_the_real_beginning():
    """`g` means the start of the conversation, not the start of the page."""
    tui = _tui()
    tui.model.older = 450

    tui.handle(ord("g"))
    assert tui.model.older == 0
    assert tui.chat.offset == 0


def test_a_conversation_that_fits_does_not_drag_the_log_in():
    """Sitting at the bottom of a short conversation is offset 0 as well.

    Reading that as «show me more history» pulled the whole log in a page per
    keystroke and dropped the pane off the live end while doing it.
    """
    tui = _tui()
    tui.model.older = 500
    tui.chat.total, tui.chat.rows = 4, 10      # it all fits
    tui.chat.settle()

    for _ in range(5):
        tui.handle(curses.KEY_DOWN)

    assert tui.model.pulls == 0
    assert tui.chat.follow, "and it is still following what is said next"


def test_nothing_older_means_nothing_to_fetch():
    tui = _tui()
    tui.chat.offset, tui.chat.follow = 0, False

    tui.handle(curses.KEY_UP)
    assert tui.model.pulls == 0
