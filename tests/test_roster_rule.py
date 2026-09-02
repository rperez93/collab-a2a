"""A rule between the participants and the roster panel's status row.

The row at the foot of the roster carries the session's figures — the batch
and the message count — and it sat directly under the last participant, in the
same dim colour as the state words beside the names, so it read as one more
line of the list. A rule above it, drawn the way the section headers are, is
what says where the list ends and the figures begin.

The rule costs a row, and the rules for paying it are what these tests hold:
it comes out of the roster only while the roster keeps at least two rows of
participants after it, and below that it is the RULE that is dropped, never a
participant and never the status row. With the status row off there is no
rule either. And the conversation pane does not move: the rule lives inside
the roster panel's allocation.

The sweep draws on a cell grid rather than on a fake that concatenates writes
per row, because the failures it is looking for — a rule painted over a
participant, a status row painted over the rule, a write past the last column —
are exactly the ones a concatenating fake reads as success.
"""

from __future__ import annotations

import curses
import time

import pytest

from collab import config
from collab.client import tui
from collab.config import SessionProfile

# The viewer draws nothing under four rows or twenty-four columns, and under
# eight rows the split view gives the whole window to the conversation.
HEIGHTS = range(4, 61)
SPLIT_HEIGHTS = range(8, 61)
WIDTHS = (24, 40, 80, 110)


class Screen:
    """Cells, by column, the way a terminal holds them.

    A wide character takes its column and leaves an empty cell after it, so a
    row reads back as the columns it filled. Writes past the last column are
    recorded rather than clipped: on a real terminal one is what ends the
    viewer, so the test wants to see it, not survive it.
    """

    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.cells = [[" "] * width for _ in range(height)]
        self.overruns: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.height, self.width

    def _put(self, y: int, x: int, text: str, last: int) -> None:
        for ch in text:
            w = tui._w(ch)
            if x + w > last:
                self.overruns.append((y, x, text))
                return
            self.cells[y][x] = ch
            for k in range(1, w):
                self.cells[y][x + k] = ""
            x += w

    def addnstr(self, y, x, text, n, *attr):
        # addnstr may not touch the last cell of the window; that is the write
        # curses refuses, and the one every caller here bounds with width - 1.
        self._put(y, x, text[:n], self.width - 1)

    def hline(self, y, x, ch, n, *attr):
        glyph = chr(ch) if isinstance(ch, int) else ch
        # hline does not move the cursor, so it may reach the last column.
        self._put(y, x, glyph * n, self.width)

    def row(self, y: int) -> str:
        return "".join(self.cells[y])

    def __getattr__(self, _name):
        return lambda *a, **kw: None


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


def _viewer(tmp_path, view: str, people: int) -> tui.Tui:
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True, exist_ok=True)
    profile = SessionProfile(session_id="s", url="u", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save()
    model = tui.Model(profile=profile)
    now = time.time()
    # One wide name, so the column measure is exercised on a participant row
    # and not only on the status row's glyphs.
    names = ["bob", "alice", "機能追加"] + [f"agent{i:02d}" for i in range(people)]
    model.snapshot = {"participants": [{"name": n, "connected": True}
                                       for n in names[:people]],
                      "fetched_at": now}
    model.status = {"batch": {"done": 6, "total": 10, "fetched_at": now},
                    "messages": {"total": 128, "fetched_at": now}}
    model.own_stats = {"cost_usd": 3.1}
    model._state = "live"
    return tui.Tui(model, view=view)


def _draw(viewer, win) -> None:
    try:
        viewer._draw(win)
    except curses.error:
        pass


def _is_rule(line: str) -> bool:
    return bool(line.strip()) and set(line.strip()) == {"-"}


def _is_roster_row(line: str) -> bool:
    # The batch is the last thing the row gives up for width, and it is not on
    # the reader's own row by default, so it is what tells the row apart.
    return "6/10" in line


def _columns(line: str, n: int) -> str:
    """The first `n` columns of a row, measured and not sliced.

    A wide name collapses to one character per glyph in the row string, so a
    character slice reaches the gutter glyph two columns past where the row
    was told to stop; and the viewer's own `_clip` marks a cut with an
    ellipsis, which is right on screen and wrong in a comparison.
    """
    out, cols = [], 0
    for ch in line:
        w = tui._w(ch)
        if cols + w > n:
            break
        out.append(ch)
        cols += w
    return "".join(out)


def _participants_shown(screen: Screen, top: int, rows: list, content: int) -> int:
    """How many rows from `top` down are, intact, the roster's rows in order."""
    shown = 0
    for i, row in enumerate(rows):
        y = top + i
        if y >= screen.height:
            break
        if _columns(screen.row(y), content).rstrip() != _columns(row.text[:content], content).rstrip():
            break
        shown += 1
    return shown


# --- the split view -----------------------------------------------------------

def _split_geometry(height: int) -> tuple[int, int]:
    """(chat_top, panel): where the conversation header is, and how many rows
    the roster panel has under its own header. The arithmetic the viewer has
    always used for the panel's size — the rule must not change it."""
    body_height = height - 2 - 1                      # title rows, bottom bar
    roster_h = max(int(body_height * tui.ROSTER_SHARE), tui.MIN_ROSTER_ROWS)
    roster_h = min(roster_h, max(body_height - 4, 2))
    return 2 + roster_h, roster_h - 1


@pytest.mark.parametrize("roster_row", [True, False])
def test_the_split_view_pays_for_the_rule_only_out_of_a_roster_that_can(
        tmp_path, cfg, roster_row):
    if not roster_row:
        config.save_watch_roster(enabled=False)
    viewer = _viewer(tmp_path, "both", people=40)
    saw_a_rule = saw_no_rule_for_want_of_room = False
    for height in HEIGHTS:
        if height not in SPLIT_HEIGHTS:
            win = Screen(height, 80)
            _draw(viewer, win)
            assert "CONVERSATION" in win.row(0), f"nothing drawn at {height}"
            assert not any(_is_rule(win.row(y)) for y in range(height)), height
            continue
        for width in WIDTHS:
            where = f"at {height}x{width}"
            win = Screen(height, width)
            _draw(viewer, win)
            assert "PARTICIPANTS" in win.row(2), f"nothing drawn {where}"
            assert not win.overruns, f"drawn past the last column {where}: {win.overruns[0]}"

            chat_top, panel = _split_geometry(height)
            assert "CONVERSATION" in win.row(chat_top), \
                f"the conversation pane moved {where}"
            # Nothing of the roster panel's leaks below its allocation.
            assert not _is_roster_row(win.row(chat_top)), where

            rows = viewer._roster_rows
            # The width the rows were BUILT to: the gutter is decided from the
            # previous frame, so asking for it after the draw is a frame late.
            content = viewer._roster_key[0]
            shown = _participants_shown(win, 3, rows, content)
            assert shown == viewer.roster.rows, \
                f"{where}: {viewer.roster.rows} rows reserved, {shown} drawn intact"

            # THE STATUS ROW, on the terms it always had: when it has something
            # to say and a whole participant still fits above it.
            bar = roster_row and panel - 1 >= 2
            assert _is_roster_row(win.row(chat_top - 1)) is bar, \
                f"{where}: status row {'missing' if bar else 'drawn'}"
            # THE RULE: only with the row, and only while two rows of
            # participants remain after paying for it.
            rule = bar and panel - 2 >= 2
            assert _is_rule(win.row(chat_top - 2)) is rule, \
                f"{where}: rule {'missing' if rule else 'drawn'} above the row"
            saw_a_rule |= rule
            saw_no_rule_for_want_of_room |= bar and not rule

            # NEVER A PARTICIPANT INSTEAD OF THE RULE. The rows left to the
            # list are what they were before the rule existed, less one only
            # where the rule was actually drawn.
            before = panel - (1 if bar else 0)
            assert shown == before - (1 if rule else 0), \
                f"{where}: {before} participant rows before the rule, {shown} after"
            # And no rule anywhere else in the panel — not painted over a
            # participant, not left where the row it belongs to is not.
            for y in range(3, chat_top - (2 if rule else 1)):
                assert not _is_rule(win.row(y)), f"{where}: a rule at row {y}"
    if roster_row:
        assert saw_a_rule and saw_no_rule_for_want_of_room, \
            "the sweep did not reach both sides of the threshold"
    else:
        assert not saw_a_rule


def test_the_rule_sits_directly_above_the_row_when_the_list_is_short(
        tmp_path, cfg):
    """One participant on a tall pane: the rule is pinned with the row it
    separates, at the foot of the panel, not floating under the last name."""
    viewer = _viewer(tmp_path, "both", people=1)
    win = Screen(40, 110)
    _draw(viewer, win)
    assert "PARTICIPANTS" in win.row(2)
    chat_top, _ = _split_geometry(40)
    assert _is_roster_row(win.row(chat_top - 1))
    assert _is_rule(win.row(chat_top - 2))
    assert " bob" in win.row(3)
    assert all(not win.row(y).strip() for y in range(5, chat_top - 2)), \
        "the rows between the list and the rule are blank"


# --- the roster-only view -----------------------------------------------------

@pytest.mark.parametrize("roster_row,personal", [
    (True, True), (True, False), (False, True), (False, False),
])
def test_the_roster_only_view_rules_off_its_own_row_and_no_other(
        tmp_path, cfg, roster_row, personal):
    """That pane's one bottom row is the roster's when the session row is on,
    and the reader's when only the personal row is. The rule belongs to the
    first and never to the second."""
    if not roster_row:
        config.save_watch_roster(enabled=False)
    if not personal:
        config.save_watch_status(enabled=False)
    viewer = _viewer(tmp_path, "roster", people=40)
    saw_a_rule = False
    for height in HEIGHTS:
        for width in WIDTHS:
            where = f"at {height}x{width}"
            win = Screen(height, width)
            _draw(viewer, win)
            assert "PARTICIPANTS" in win.row(0), f"nothing drawn {where}"
            assert not win.overruns, f"drawn past the last column {where}: {win.overruns[0]}"

            bottom = win.row(height - 1)
            row = roster_row or personal
            if roster_row:
                assert _is_roster_row(bottom), f"{where}: the session's row is missing"
            elif personal:
                assert "$3.10" in bottom, f"{where}: the reader's row is missing"
            else:
                # The line went back to the list.
                assert not _is_roster_row(bottom) and "$3.10" not in bottom, \
                    f"{where}: a row nobody asked for"

            rule = roster_row and height - 1 - 1 - 1 >= 2
            assert _is_rule(win.row(height - 2)) is rule, \
                f"{where}: rule {'missing' if rule else 'drawn'}"
            saw_a_rule |= rule

            rows = viewer._roster_rows
            # The width the rows were BUILT to: the gutter is decided from the
            # previous frame, so asking for it after the draw is a frame late.
            content = viewer._roster_key[0]
            shown = _participants_shown(win, 1, rows, content)
            assert shown == viewer.roster.rows, \
                f"{where}: {viewer.roster.rows} rows reserved, {shown} drawn intact"
            assert shown == height - 1 - (1 if row else 0) - (1 if rule else 0), \
                f"{where}: {shown} participant rows"
            for y in range(1, height - (2 if rule else 1)):
                assert not _is_rule(win.row(y)), f"{where}: a rule at row {y}"
    assert saw_a_rule is roster_row


def test_the_chat_only_view_has_no_roster_and_so_no_rule(tmp_path, cfg):
    viewer = _viewer(tmp_path, "chat", people=3)
    for height in (4, 5, 12, 40):
        win = Screen(height, 80)
        _draw(viewer, win)
        assert "CONVERSATION" in win.row(0), f"nothing drawn at {height}"
        assert not any(_is_rule(win.row(y)) for y in range(1, height)), height
