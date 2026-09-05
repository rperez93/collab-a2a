"""The roster panel's foot: four columns, its own colours, and room to grow.

The conversation's row gives things up from the right, which is right for it:
it carries a legend and a user's command, and losing those costs a reminder.
The roster's foot has no legend to spend. Every part of it is a figure the hub
counted, so a figure it dropped for width was the feature not working — and
what it did about that was clip, which on forty columns is a batch bar and a
message count sharing a line neither of them can be read on.

So it is a grid: four equal columns across the panel, each segment declaring
how many it takes. The batch takes all four and draws its bar across the whole
width, which is the one figure here that gets better with room; the count takes
one, because it is six characters; the rest take two.

The layout depends only on the spans, never on the text. That is the point of
declaring them — a foot that reflowed as a percentage went from 9% to 10% would
move every figure four times a minute — and it is what makes the row count
knowable before the panel's height is divided up.

Two things it does NOT do, both deliberate. It does not fill a pane too narrow
for four columns to hold a figure each, or too short for the rows: there it
falls back to the single fitted row this foot had before, which narrows
everything and drops nothing. And it does not spend the roster's rows without
paying the roster's price: every row is taken only while two rows of
participants still remain after it.
"""

from __future__ import annotations

import curses
import time

import pytest

from collab import config
from collab.client import statusbar as sb
from collab.client import tui
from test_roster_rule import Screen, _draw, _split_geometry, _viewer

NOW = time.time()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


def _pieces(*names):
    """Composed segments, named, with something to say in each."""
    return sb.compose_named(
        keys=("the whole legend at some length", "q: quit"),
        batch={"done": 4, "total": 9, "fetched_at": NOW},
        messages={"total": 128, "fetched_at": NOW},
        activity={"state": "working", "what": "the parser",
                  "since": NOW - 600, "updated_at": NOW - 600},
        segments=names, now=NOW)


def _grid(width, *names, **kw):
    return sb.grid(_pieces(*names), width, tui._w, tui._clip, **kw)


# --- the four columns ------------------------------------------------------------

def test_the_columns_are_equal_and_reach_the_last_one():
    """Computed from the whole width rather than by multiplying a rounded cell,
    or a pane that does not divide by four leaves a ragged edge."""
    edges = sb.column_edges(80)
    assert edges[0] == 0
    assert edges[-1] == 82, "the last column ends at the pane, less no gap"
    widths = [edges[i + 1] - edges[i] - sb.GRID_GAP for i in range(4)]
    assert max(widths) - min(widths) <= 1, widths


def test_the_batch_takes_a_whole_row_and_the_count_takes_one_column():
    placed = sb.place(("batch", "messages", "activity", "keys"), sb.DEFAULT_SPANS)
    assert placed == [("batch", 0, 0), ("messages", 1, 0),
                      ("activity", 1, 1), ("keys", 2, 0)]


def test_a_segment_that_does_not_fit_starts_the_next_row():
    """Never split across two: half a batch bar on one row and half on the next
    is not a figure."""
    placed = sb.place(("messages", "batch"), sb.DEFAULT_SPANS)
    assert placed == [("messages", 0, 0), ("batch", 1, 0)]


def test_the_bar_runs_the_width_of_the_panel():
    """Six glyphs were right when this shared a line with three other figures.
    On a row of its own it is the one figure that gets better with room."""
    wide = _grid(120, "batch")[0]
    narrow = _grid(40, "batch")[0]
    assert wide.text.count("█") + wide.text.count("░") > 40
    assert narrow.text.count("█") + narrow.text.count("░") < 40
    assert tui._w(wide.text) <= wide.width, "it over-ran its cell"


def test_the_bar_is_capped_however_wide_the_pane_is():
    """Past a point the eye reads a line rather than a proportion."""
    cell = _grid(400, "batch")[0]
    assert cell.text.count("█") + cell.text.count("░") <= sb.MAX_BAR_WIDTH


def test_a_cell_never_overruns_its_column():
    for width in (24, 40, 60, 80, 120, 200):
        for cell in _grid(width, "batch", "messages", "activity", "keys"):
            assert tui._w(cell.text) <= cell.width, (width, cell)
            assert cell.x + cell.width <= width, (width, cell)


def test_a_segment_is_narrowed_before_it_is_clipped():
    """The rule the single row already used, applied inside a cell."""
    cell = next(c for c in _grid(48, "batch", "messages") if c.name == "messages")
    assert cell.text in ("128 messages", "128 msgs")
    tight = next(c for c in _grid(40, "messages", "keys") if c.name == "keys")
    assert tight.text.endswith("…") or tight.text == "q: quit"


# --- how many rows, and who pays for them -----------------------------------------

def test_the_rows_needed_are_known_before_anything_is_drawn():
    """The roster gives up these rows, so it has to know how many first."""
    assert sb.rows_needed(("batch", "messages", "activity", "keys")) == 3
    assert sb.rows_needed(("messages", "keys")) == 1
    assert sb.rows_needed(()) == 0


def test_the_row_limit_drops_from_the_right():
    """The list is an order of preference, so the last thing somebody put on it
    is the thing they can most afford to lose."""
    kept = [c.name for c in _grid(120, "batch", "messages", "activity", "keys",
                                  rows=2)]
    assert kept == ["batch", "messages", "activity"]


def test_the_limit_is_a_setting_and_is_bounded(cfg):
    assert config.watch_roster_settings()["rows"] == config.DEFAULT_ROSTER_ROWS
    config.setting("watch_status_roster_rows").write(6)
    assert config.watch_roster_settings()["rows"] == 6
    cfg.write_text('{"watch_status_roster_rows": 99}')
    config._CACHE.clear()
    assert config.watch_roster_settings()["rows"] == config.MAX_ROSTER_FOOT_ROWS
    cfg.write_text('{"watch_status_roster_rows": "lots"}')
    config._CACHE.clear()
    assert config.watch_roster_settings()["rows"] == config.DEFAULT_ROSTER_ROWS


# --- the spans are configurable ----------------------------------------------------

def test_a_span_is_written_beside_the_name(cfg):
    config.setting("watch_status_roster_segments").write(
        ["batch:4", "messages:1", "keys:2"])
    told = config.watch_roster_settings()
    assert told["segments"] == ("batch", "messages", "keys")
    assert told["spans"] == {"batch": 4, "messages": 1, "keys": 2}


def test_a_bare_name_keeps_its_default_span(cfg):
    """The whole feature is invisible to anybody who does not want it, and the
    list they already wrote goes on meaning what it meant."""
    config.setting("watch_status_roster_segments").write(["batch", "messages:2"])
    assert config.watch_roster_settings()["spans"] == {"messages": 2}


def test_it_is_printed_back_in_the_form_it_was_typed(cfg):
    """A listing that showed `batch` for a stored `batch:4` would answer «what
    did I set» with something setting it again would not produce."""
    config.setting("watch_status_roster_segments").write(["batch:4", "keys"])
    assert config.setting("watch_status_roster_segments").read() == ["batch:4", "keys"]


def test_a_span_outside_the_grid_costs_the_span_and_not_the_segment(cfg):
    """Somebody who typed `batch:9` wants the batch; dropping it would answer a
    question they did not ask."""
    with pytest.raises(ValueError) as bad:
        config.setting("watch_status_roster_segments").write(["batch:9"])
    assert "1 to 4" in str(bad.value)
    cfg.write_text('{"watch_status_roster_segments": ["batch:9", "messages"]}')
    config._CACHE.clear()
    told = config.watch_roster_settings()
    assert told["segments"] == ("batch", "messages"), "the segment survived"
    assert told["spans"] == {}, "and only its span was refused"


def test_the_span_reaches_the_layout(cfg):
    cells = _grid(120, "batch", "messages", spans={"batch": 2, "messages": 2})
    assert [(c.name, c.row) for c in cells] == [("batch", 0), ("messages", 0)]


# --- when the grid is not used at all -----------------------------------------------

def test_a_pane_too_narrow_for_four_columns_is_not_gridded():
    """At twenty-four columns a quarter-cell is four characters and `128 msgs`
    arrives as `128…`. That is worse than the one fitted row this foot had."""
    assert not sb.fits_the_grid(_pieces("batch", "messages"), 23, tui._w)
    assert sb.fits_the_grid(_pieces("batch", "messages"), 79, tui._w)


def test_the_fallback_keeps_every_figure(tmp_path, cfg):
    """Which is the whole reason for falling back: the grid drops from the
    right past its row limit, and a short pane never chose that limit."""
    viewer = _viewer(tmp_path, "both", people=40)
    win = Screen(24, 27)
    _draw(viewer, win)
    chat_top, _panel = _split_geometry(24)
    foot = " ".join(win.row(chat_top - 1 - n) for n in range(3))
    assert "6/10" in foot or "60%" in foot, foot
    assert "128 msgs" in foot or "128 messages" in foot, foot


# --- the colours -----------------------------------------------------------------------

class _Attrs(Screen):
    """A screen that records the attribute each write was made with."""

    def __init__(self, height, width):
        super().__init__(height, width)
        self.written: list[tuple[int, str, int]] = []

    def addnstr(self, y, x, text, n, *attr):
        super().addnstr(y, x, text, n, *attr)
        if text.strip():
            self.written.append((y, text, attr[0] if attr else 0))


def _foot_writes(viewer, win, chat_top):
    return [(text, attr) for y, text, attr in win.written
            if chat_top - 4 <= y < chat_top]


def test_each_figure_carries_its_own_attribute(tmp_path, cfg, monkeypatch):
    """The row was uniformly dim, so a batch bar, a count and a legend read as
    one undifferentiated strip: the eye has nothing to catch on, and the figure
    two agents are steering by looks exactly like the words `q: quit`."""
    seen: dict[int, int] = {}
    monkeypatch.setattr(curses, "color_pair", lambda n: seen.setdefault(n, n * 100))
    viewer = _viewer(tmp_path, "both", people=8)
    win = _Attrs(30, 120)
    _draw(viewer, win)
    chat_top, _ = _split_geometry(30)
    writes = _foot_writes(viewer, win, chat_top)
    batch = next(a for t, a in writes if "4/9" in t or "6/10" in t)
    count = next(a for t, a in writes if "messages" in t or "msgs" in t)
    assert batch != count, "the batch and the count are drawn the same"
    assert batch & curses.color_pair(tui.C_ACCENT), "the batch is not accented"


def test_the_activity_is_bold_while_working_and_not_otherwise():
    cell = sb.Cell("activity", "working: the parser · 10m ago", 0, 0, 40)
    viewer = tui.Tui.__new__(tui.Tui)
    working = viewer._foot_attr(cell)
    resting = viewer._foot_attr(sb.Cell("activity", "idle · 2m ago", 0, 0, 40))
    assert working != resting


# --- escape codes in a segment ----------------------------------------------------------

def test_a_colour_code_becomes_an_attribute_rather_than_three_characters():
    runs = sb.sgr_runs("\033[32mgreen\033[0m plain")
    assert [r[0] for r in runs] == ["green", " plain"]
    assert runs[0][1] == "32" and runs[1][1] == ""


def test_bold_and_dim_are_honoured():
    runs = sb.sgr_runs("\033[1mbold\033[0m\033[2mdim\033[0m")
    assert runs[0][2] is True and runs[0][3] is False
    assert runs[1][3] is True


@pytest.mark.parametrize("hostile", [
    "\033[2Jcleared", "\033[10;10Hmoved", "\033]0;title\007renamed",
])
def test_anything_that_is_not_a_colour_is_dropped(hostile):
    """A cursor movement in a segment is not a colour, it is a command to the
    terminal — and this text can be whatever a user's command printed."""
    runs = sb.sgr_runs(hostile)
    joined = "".join(r[0] for r in runs)
    assert "\033" not in joined


def test_measuring_ignores_the_escapes():
    """Measured with the codes in it, a coloured segment reserves columns it
    does not use and the figure beside it is dropped for a width nobody used."""
    assert sb.strip_sgr("\033[32mabc\033[0m") == "abc"
    assert tui._w(sb.strip_sgr("\033[32mabc\033[0m")) == 3


# --- the padding above the rule -----------------------------------------------------------

def test_the_padding_needs_two_whole_participants_after_it(tmp_path, cfg):
    """It is the only thing at the foot that says nothing, so it is the first
    to justify itself: on a panel showing one person a blank row is a quarter
    of what the reader came for."""
    tall = _viewer(tmp_path, "both", people=40)
    win = Screen(40, 120)
    _draw(tall, win)
    from test_roster_rule import _foot

    chat_top, panel = _split_geometry(40)
    assert panel >= 8, "the fixture has to be a panel with room"
    foot = _foot(panel, True, 120)
    assert foot["pad_top"], "this panel is tall enough to pay for the padding"
    above = chat_top - 1 - foot["pad_bottom"] - foot["bar"] - foot["rule"]
    assert not win.row(above).strip(), "no blank row above the rule"
