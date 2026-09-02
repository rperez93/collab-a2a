"""The way back to the live end, for a hand that is already on the mouse.

The bottom row already says it — «⏸ 2 new below — End (or G) jumps to the
newest» — and saying it is most of the job: somebody scrolled back needs to
know something arrived before they need a way down. What was missing is that
the sentence did nothing when clicked, which is the one thing a person with a
mouse will try.

So the notice IS the control. Not a bracket beside it: the row is shared with a
batch figure, a spend and whatever a status command prints, and three columns
spent restating what the sentence already says are three columns the command
does not get. There is no separate scrollbar down there to hang a button on —
that row belongs to the status bar, and the position is told down the side.
"""

from __future__ import annotations

import curses

import pytest

from collab import config
from collab.client import tui
from collab.client.tui import Row

from test_tui_scroll import FakeModel


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    """A config of our own: the status row is read from it on every frame, and
    these tests must not answer to whatever the person running them set."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


class _Pane:
    """Just enough curses window to capture what was written where."""

    def __init__(self, height=30, width=110):
        self.size = (height, width)
        self.rows: dict[int, str] = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, *a):
        self.rows[y] = self.rows.get(y, "") + text[:n]

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def _drawn(*, following: bool, behind: int = 2, width=110, height=30):
    """A viewer with the bottom row laid out, as `_draw` leaves it.

    `_hint` is called directly rather than through `_draw` for the reason the
    row's own arithmetic is tested without a terminal: going through curses to
    find out where a span landed only adds a way for the measurement to be the
    thing that is wrong. What `_draw` would have set is set here instead.
    """
    viewer, win = tui.Tui(FakeModel(newer=behind), view="both"), _Pane(height,
                                                                      width)
    viewer.chat.rows, viewer.chat.total, viewer.chat.offset = 10, 200, 20
    viewer.chat.follow = following
    viewer._chat_rows = [Row(f"line {i}", seq=i + 1) for i in range(200)]
    viewer._settings = config.watch_status_settings()
    viewer._bar = True
    viewer._hint(win, height, width)
    return viewer, win


def _click(monkeypatch, viewer, *, x, y, state=curses.BUTTON1_PRESSED):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, x, y, 0, state))
    return viewer.handle(curses.KEY_MOUSE)


# --- where it is -------------------------------------------------------------

def test_the_notice_is_on_the_row_and_says_which_keys_do_it():
    """The click is the addition; the sentence was already right. If this line
    stops naming End, somebody without a mouse has lost the way back."""
    _, win = _drawn(following=False)
    row = win.rows[29]
    assert "2 new below" in row
    assert "End" in row and "G" in row


def test_the_span_covers_the_notice_and_stops_where_it_ends():
    """Not the whole row. The rest of it is a batch figure and a reminder, and
    a click there must stay a no-op rather than being rounded to the nearest
    control."""
    viewer, win = _drawn(following=False)
    start, end = viewer._jump

    assert start == 0, "the notice goes first and `fit` never drops it"
    assert end == tui._w(win.rows[29][:end])
    assert end < tui._w(win.rows[29]), "there is row left over after it"


def test_following_the_live_end_there_is_nothing_to_click():
    """No notice, no span. A control that does nothing when pressed teaches
    people the row is decorative."""
    viewer, _ = _drawn(following=True)
    assert viewer._jump == (0, 0)


def test_scrolled_back_with_nothing_new_is_still_a_way_back():
    """«⏸ scrolled back» rather than a count, and it clicks just the same.

    Nothing arrived, but the reader is still not looking at the live end, and
    getting back there is the same journey whether or not anybody spoke while
    they were away.
    """
    viewer, win = _drawn(following=False, behind=0)
    start, end = viewer._jump

    assert "scrolled back" in win.rows[29]
    assert end > start


def test_the_rosters_own_row_claims_no_click():
    """The same method draws the roster panel's row, over a pane with no
    conversation in it. There is nowhere to jump to from there, so it must
    claim neither a span nor a row — otherwise a click at the foot of a
    roster-only viewer would scroll a conversation nobody is looking at."""
    viewer, _ = tui.Tui(FakeModel(newer=2), view="roster"), None
    win = _Pane(30, 110)
    viewer.chat.rows, viewer.chat.total, viewer.chat.offset = 10, 200, 20
    viewer.chat.follow = False
    viewer._chat_rows = [Row(f"line {i}", seq=i + 1) for i in range(200)]
    viewer._settings = config.watch_status_settings()
    viewer._roster_settings = config.watch_roster_settings()
    viewer._bar = True
    viewer._hint(win, 30, 110, notice=False, roster=True)

    assert viewer._jump == (0, 0)
    assert viewer._jump_y == -1, "no row is claimed, so no click resolves to it"


# --- what clicking it does ---------------------------------------------------

def test_clicking_the_notice_goes_to_the_live_end(monkeypatch):
    viewer, _ = _drawn(following=False)

    _click(monkeypatch, viewer, x=1, y=viewer._jump_y)
    assert viewer.model.jumps == ["tail"], "the live end, not the end of the window"
    assert viewer.chat.follow


def test_the_key_and_the_click_do_the_same_thing(monkeypatch):
    """One implementation, so they cannot drift apart."""
    by_key, _ = _drawn(following=False)
    by_key.handle(ord("G"))

    by_click, _ = _drawn(following=False)
    _click(monkeypatch, by_click, x=1, y=by_click._jump_y)

    assert by_key.model.jumps == by_click.model.jumps == ["tail"]
    assert by_key.chat.offset == by_click.chat.offset
    assert by_key.chat.follow == by_click.chat.follow


def test_a_click_past_the_notice_lands_on_nothing(monkeypatch):
    viewer, _ = _drawn(following=False)
    _, end = viewer._jump

    _click(monkeypatch, viewer, x=end + 1, y=viewer._jump_y)
    assert viewer.model.jumps == []


def test_a_click_on_another_row_is_not_a_click_on_it(monkeypatch):
    viewer, _ = _drawn(following=False)

    _click(monkeypatch, viewer, x=1, y=viewer._jump_y - 1)
    assert viewer.model.jumps == []


def test_a_double_click_still_jumps(monkeypatch):
    """The impatient case, and the one ncurses renames out from under you: two
    quick clicks arrive as one BUTTON1_DOUBLE_CLICKED, not as two presses."""
    state = getattr(curses, "BUTTON1_DOUBLE_CLICKED", None)
    if state is None:
        pytest.skip("this ncurses has no BUTTON1_DOUBLE_CLICKED")
    viewer, _ = _drawn(following=False)

    _click(monkeypatch, viewer, x=1, y=viewer._jump_y, state=state)
    assert viewer.model.jumps == ["tail"]


# --- and it cannot promise a column that is not there ------------------------

@pytest.mark.parametrize("width", [24, 30, 40, 60, 110])
def test_the_span_never_reaches_past_what_was_drawn(width):
    """`fit` puts the notice first and never drops it, but it WILL clip it when
    the row cannot hold it. A span taken from the unclipped sentence would then
    answer clicks that landed on empty terminal."""
    viewer, win = _drawn(following=False, width=width)

    _, end = viewer._jump
    assert end <= tui._w(win.rows.get(29, "")) <= width
