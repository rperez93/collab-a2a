"""A titled rule, and a row of air on either side, between the participants and
the roster panel's status row.

The row at the foot of the roster carries the session's figures — the batch
and the message count — and it sat directly under the last participant, in the
same dim colour as the state words beside the names, so it read as one more
line of the list. A rule above it, drawn the way the section headers are and
labelled `STATUS` the way they are labelled, is what says where the list ends
and the figures begin; a blank row above the rule and one below the status row
are what stop the block reading as glued to whichever neighbour it touches.

All of it costs rows, and the rules for paying them are what these tests hold.
They come out of the roster only while the roster keeps at least two rows of
participants — one whole person — after paying, and below that they are given
up in order: the bottom padding first, then the top padding, then the rule;
never a participant and never the status row. With the status row off there is
none of it. And the conversation pane does not move: everything lives inside
the roster panel's allocation.

The roster-only view pays for the top padding and the rule on the same terms
and nothing below the row: there the row sits on the pane's last line, where
the conversation's own bar sits in its pane, and a blank line under it would
separate it from a tmux border that separates it already.

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
WIDTHS = (24, 40, 80, 120)


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


def _is_status_rule(line: str) -> bool:
    """A rule drawn the way the section headers are: dashes across the row
    with the label two columns in, in the same case as `PARTICIPANTS`."""
    body = line.rstrip()
    if not body.startswith("-- STATUS -"):
        return False
    return set(body.replace(" STATUS ", "", 1)) == {"-"}


def _is_any_rule(line: str) -> bool:
    """Labelled or not: a run of the rule glyph is what no participant row has."""
    return "----" in line


def _is_roster_row(line: str) -> bool:
    """Both figures, because both are what the row is for: a row that carried
    the batch and had lost the count is the defect this panel was reported
    for. The count may be in its narrow form on a narrow pane."""
    return "6/10" in line and ("128 messages" in line or "128 msgs" in line)


def _blank(line: str) -> bool:
    return not line.strip()


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


def _foot(panel: int, roster_row: bool) -> dict[str, bool]:
    """What the panel's foot is made of, given `panel` rows under its header.

    Paid for in this order, each only while two participant rows survive it:
    the status row, the rule, the padding above the rule, the padding below
    the row. Given up in the reverse order.
    """
    bar = roster_row and panel - 1 >= 2
    rule = bar and panel - 2 >= 2
    pad_top = rule and panel - 3 >= 2
    pad_bottom = pad_top and panel - 4 >= 2
    return {"bar": bar, "rule": rule, "pad_top": pad_top, "pad_bottom": pad_bottom}


# --- the split view -----------------------------------------------------------

def _split_geometry(height: int) -> tuple[int, int]:
    """(chat_top, panel): where the conversation header is, and how many rows
    the roster panel has under its own header. The arithmetic the viewer has
    always used for the panel's size — nothing at the foot may change it."""
    body_height = height - 2 - 1                      # title rows, bottom bar
    roster_h = max(int(body_height * tui.roster_share()), tui.MIN_ROSTER_ROWS)
    roster_h = min(roster_h, max(body_height - 4, 2))
    return 2 + roster_h, roster_h - 1


@pytest.mark.parametrize("roster_row", [True, False])
def test_the_split_view_pays_for_the_foot_only_out_of_a_roster_that_can(
        tmp_path, cfg, roster_row):
    if not roster_row:
        config.save_watch_roster(enabled=False)
    viewer = _viewer(tmp_path, "both", people=40)
    seen: dict[str, set[bool]] = {k: set() for k in ("rule", "pad_top", "pad_bottom")}
    for height in HEIGHTS:
        if height not in SPLIT_HEIGHTS:
            win = Screen(height, 80)
            _draw(viewer, win)
            assert "CONVERSATION" in win.row(0), f"nothing drawn at {height}"
            assert not any(_is_status_rule(win.row(y)) for y in range(height)), height
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

            foot = _foot(panel, roster_row)
            for k in seen:
                if foot["bar"]:
                    seen[k].add(foot[k])
            # THE ROWS LEFT TO THE LIST: what they were before any of this
            # existed, less exactly what was drawn at the foot.
            spent = sum(foot.values())
            assert shown == panel - spent, \
                f"{where}: {panel} rows in the panel, {spent} at the foot, {shown} participants"

            # FROM THE BOTTOM UP: padding, the status row, the rule, padding.
            y = chat_top - 1
            if foot["pad_bottom"]:
                assert _blank(win.row(y)), f"{where}: no blank row under the status row"
                y -= 1
            assert _is_roster_row(win.row(y)) is foot["bar"], \
                f"{where}: status row {'missing' if foot['bar'] else 'drawn'}: {win.row(y)!r}"
            if foot["bar"]:
                y -= 1
            assert _is_status_rule(win.row(y)) is foot["rule"], \
                f"{where}: rule {'missing' if foot['rule'] else 'drawn'} above the row: {win.row(y)!r}"
            if foot["rule"]:
                y -= 1
            if foot["pad_top"]:
                assert _blank(win.row(y)), f"{where}: no blank row above the rule"
                y -= 1
            # And the last participant row is the one directly above all that.
            assert y == 3 + shown - 1, f"{where}: a gap of unknown rows at the foot"
            # No rule anywhere else in the panel — not painted over a
            # participant, not left where the row it belongs to is not.
            for yy in range(3, 3 + shown):
                assert not _is_any_rule(win.row(yy)), f"{where}: a rule at row {yy}"
    if roster_row:
        for k, sides in seen.items():
            assert sides == {True, False}, \
                f"the sweep did not reach both sides of the {k} threshold: {sides}"
    else:
        assert not any(seen.values())


def test_the_foot_is_pinned_to_the_panel_when_the_list_is_short(tmp_path, cfg):
    """One participant on a tall pane: the rule and its padding sit with the
    row they belong to, at the foot of the panel, not floating under the last
    name."""
    viewer = _viewer(tmp_path, "both", people=1)
    win = Screen(40, 120)
    _draw(viewer, win)
    assert "PARTICIPANTS" in win.row(2)
    chat_top, _ = _split_geometry(40)
    assert _blank(win.row(chat_top - 1)), "a blank row under the status row"
    assert _is_roster_row(win.row(chat_top - 2))
    assert _is_status_rule(win.row(chat_top - 3))
    assert " bob" in win.row(3)
    assert all(_blank(win.row(y)) for y in range(5, chat_top - 3)), \
        "the rows between the list and the rule are blank"


def test_the_status_rule_is_drawn_like_the_section_headers(tmp_path, cfg):
    """Same painter, same place for the label, same case."""
    viewer = _viewer(tmp_path, "both", people=3)
    win = Screen(30, 80)
    _draw(viewer, win)
    chat_top, _ = _split_geometry(30)
    header, rule = win.row(2), win.row(chat_top - 3)
    assert header.startswith("-- PARTICIPANTS")
    assert rule.startswith("-- STATUS -"), rule
    assert _is_status_rule(rule)


# --- the roster-only view -----------------------------------------------------

@pytest.mark.parametrize("roster_row,personal", [
    (True, True), (True, False), (False, True), (False, False),
])
def test_the_roster_only_view_rules_off_its_own_row_and_no_other(
        tmp_path, cfg, roster_row, personal):
    """That pane's one bottom row is the roster's when the session row is on,
    and the reader's when only the personal row is. The rule and the padding
    above it belong to the first and never to the second; nothing is spent
    below the row, which sits on the pane's last line."""
    if not roster_row:
        config.save_watch_roster(enabled=False)
    if not personal:
        config.save_watch_status(enabled=False)
    viewer = _viewer(tmp_path, "roster", people=40)
    saw = {"rule": set(), "pad": set()}
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
                assert _is_roster_row(bottom), f"{where}: the session's row is missing: {bottom!r}"
            elif personal:
                assert "$3.10" in bottom, f"{where}: the reader's row is missing"
            else:
                # The line went back to the list.
                assert not _is_roster_row(bottom) and "$3.10" not in bottom, \
                    f"{where}: a row nobody asked for"

            rule = roster_row and height - 3 >= 2
            pad = rule and height - 4 >= 2
            assert _is_status_rule(win.row(height - 2)) is rule, \
                f"{where}: rule {'missing' if rule else 'drawn'}: {win.row(height - 2)!r}"
            if pad:
                assert _blank(win.row(height - 3)), f"{where}: no blank row above the rule"
            if roster_row:
                saw["rule"].add(rule)
                saw["pad"].add(pad)

            rows = viewer._roster_rows
            content = viewer._roster_key[0]
            shown = _participants_shown(win, 1, rows, content)
            assert shown == viewer.roster.rows, \
                f"{where}: {viewer.roster.rows} rows reserved, {shown} drawn intact"
            assert shown == height - 1 - (1 if row else 0) - (1 if rule else 0) - (1 if pad else 0), \
                f"{where}: {shown} participant rows"
            for y in range(1, 1 + shown):
                assert not _is_any_rule(win.row(y)), f"{where}: a rule at row {y}"
    if roster_row:
        assert saw["rule"] == {True, False} and saw["pad"] == {True, False}, saw
    else:
        assert not saw["rule"] and not saw["pad"]


def test_the_chat_only_view_has_no_roster_and_so_no_rule(tmp_path, cfg):
    viewer = _viewer(tmp_path, "chat", people=3)
    for height in (4, 5, 12, 40):
        win = Screen(height, 80)
        _draw(viewer, win)
        assert "CONVERSATION" in win.row(0), f"nothing drawn at {height}"
        assert not any(_is_status_rule(win.row(y)) for y in range(1, height)), height
