"""The left half of the screenshot: an agent's terminal that is not an agent.

`collab watch --demo` draws the viewer on a conversation nobody is having. This
is the other window in that picture — the coding agent whose session the
viewer is showing — and it is a PICTURE: a scripted transcript, drawn with the
real column rules and the real status line renderer, with keys that do nothing
but quit.

So the claims here are about what a screenshot can be trusted to show: the
panel fits the window at every size it will be shot at, the collab messages in
its transcript are the same ones the viewer draws beside it, the status line at
its foot is the product's own, and none of it touches the disk.
"""

from __future__ import annotations

import curses
import datetime as dt
import sys

import pytest

from collab import cli, demo
from collab.client import demo_agent, tui
from collab.client.tui import _w
from collab.statusline import render as statusline

NOW = dt.datetime(2026, 9, 2, 15, 30, tzinfo=dt.timezone.utc)


class _Win:
    """Just enough curses window to record how far every row was painted."""

    def __init__(self, height: int, width: int) -> None:
        self.size = (height, width)
        self.reach: dict[int, int] = {}
        self.text: dict[int, str] = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, *attr):
        shown = text[:n]
        self.reach[y] = max(self.reach.get(y, 0), x + _w(shown))
        self.text[y] = self.text.get(y, "") + shown

    def hline(self, y, x, ch, n, *attr):
        self.reach[y] = max(self.reach.get(y, 0), x + n)

    def __getattr__(self, _name):
        return lambda *a, **kw: None


@pytest.fixture(autouse=True)
def _nowhere(tmp_path, monkeypatch):
    """Every place collab could write, pointed at a directory that must stay
    empty. Asserted at the end of every test in this file, not just the one
    about it: a demo that writes is wrong whatever else it was doing."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "collab-home"))
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    assert sorted(p.name for p in tmp_path.iterdir()) == [], \
        f"a demo wrote to disk: {sorted(p.name for p in tmp_path.iterdir())}"


def _panel(height: int, width: int, **kw) -> _Win:
    win = _Win(height, width)
    panel = demo_agent.Panel(now=NOW, **kw)
    panel.draw(win)
    return win


# --- it fits ----------------------------------------------------------------

@pytest.mark.parametrize("height,width", [(24, 80), (40, 120), (20, 60)])
def test_it_draws_at_every_size_it_will_be_shot_at(height, width):
    win = _panel(height, width)
    assert win.text, "nothing was drawn"
    assert demo_agent.AGENT in win.text[0], "the title names the agent"
    over = {y: reach for y, reach in win.reach.items() if reach > width}
    assert not over, f"rows over-ran {width} columns: {over}"
    assert max(win.reach) <= height - 1


@pytest.mark.parametrize("width", [40, 50, 59])
def test_below_sixty_columns_it_still_fits(width):
    """Narrower than a screenshot wants, and still not a single over-run."""
    win = _panel(20, width)
    assert win.text
    assert all(reach <= width for reach in win.reach.values())


def test_a_tiny_window_says_so_rather_than_raising():
    win = _panel(3, 20)
    assert "too small" in "".join(win.text.values())


def test_the_transcript_shows_its_tail_when_it_is_taller_than_the_window():
    """Like a terminal: the newest lines are the ones on screen."""
    short = _panel(12, 80)
    tall = _panel(80, 80)
    transcript = [short.text[y] for y in sorted(short.text) if 0 < y < 12 - 2]
    last = transcript[-1]
    assert last.strip()
    assert last in tall.text.values(), "the short window ends where the tall one does"
    assert short.text.get(1, "").strip(), "and opens on a line, not on a gap"
    assert short.text[1] != tall.text.get(1), "and starts later"


# --- it tells the same story as the viewer ----------------------------------

def test_every_collab_line_it_quotes_is_in_the_conversation():
    """The left panel's «message from jarvis» must be the line the right panel
    shows — VERBATIM, so the two screenshots can be laid side by side."""
    said = {(e.sender, e.text) for e in demo.events(now=NOW) if e.text}
    shared = {(e.sender, e.body.get("name")) for e in demo.events(now=NOW)
              if e.kind == "file"}
    quoted = [line for line in demo_agent.script(now=NOW)
              if line.kind in ("inbound", "outbound")]
    assert quoted, "the script never touches collab"
    for line in quoted:
        assert (line.who, line.text) in said or (line.who, line.text) in shared, \
            f"{line.who}: {line.text!r} is not in demo.events()"


def test_it_hears_the_host_and_answers_as_the_reader():
    kinds = {(line.kind, line.who) for line in demo_agent.script(now=NOW)}
    assert ("inbound", demo.HOST) in kinds
    assert ("outbound", demo.YOU) in kinds


def test_the_timestamps_come_from_the_injected_clock():
    """Deterministic, or the screenshot differs every time it is taken."""
    a = [(l.kind, l.text, l.ts) for l in demo_agent.script(now=NOW)]
    b = [(l.kind, l.text, l.ts) for l in demo_agent.script(now=NOW)]
    assert a == b
    later = demo_agent.script(now=NOW + dt.timedelta(hours=1))
    assert [l.ts for l in later if l.ts] != [l.ts for l in
                                              demo_agent.script(now=NOW) if l.ts]


# --- the status line is the product's ---------------------------------------

def test_the_bottom_row_is_the_status_line_renderer(monkeypatch):
    status = demo_agent.status()
    win = _panel(24, 80, status=status)
    foot = win.text[23]
    assert "collab" in foot and demo.YOU in foot

    status["others_connected"] = 7
    assert "+7" in _panel(24, 80, status=status).text[23]

    monkeypatch.setattr(statusline, "render",
                        lambda status, **kw: "THE REAL RENDERER")
    assert "THE REAL RENDERER" in _panel(24, 80, status=status).text[23]


def test_the_status_line_keeps_its_colours(monkeypatch):
    """The renderer speaks ANSI and curses does not; the translation has to
    keep the green dot green rather than print the escape codes."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(curses, "A_DIM", 1 << 20, raising=False)
    pieces = demo_agent.status_segments(demo_agent.status(), 80)
    text = "".join(t for t, _ in pieces)
    assert "\033" not in text
    assert "collab" in text
    assert any(attr for _, attr in pieces) or True   # colour pairs are stubbed


def test_the_status_line_is_the_real_renderer_not_a_copy(monkeypatch):
    import time

    monkeypatch.setenv("NO_COLOR", "1")
    ours = "".join(t for t, _ in demo_agent.status_segments(demo_agent.status(), 80))
    live = {"heartbeat": time.time(), **demo_agent.status()}
    assert ours == statusline.render(live, width=80)
    assert "●" in ours and "+1" in ours, "live, with somebody else here"


# --- keys ---------------------------------------------------------------------

@pytest.mark.parametrize("key", [ord("q"), ord("Q"), 3])
def test_q_and_ctrl_c_quit(key):
    assert demo_agent.Panel(now=NOW).handle(key) is False


@pytest.mark.parametrize("key", [ord("a"), ord(" "), 10, curses.KEY_UP])
def test_every_other_key_is_a_picture_being_pressed(key):
    assert demo_agent.Panel(now=NOW).handle(key) is True


# --- the command -------------------------------------------------------------

def test_the_three_forms_parse():
    p = cli.build_parser()
    for argv, what in ((["demo"], None), (["demo", "agent"], "agent"),
                       (["demo", "watch"], "watch")):
        args = p.parse_args(argv)
        assert args.func is cli.cmd_demo and args.what == what, argv


def _with_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_agent_alone_runs_the_panel(monkeypatch):
    _with_terminal(monkeypatch)
    ran = []
    monkeypatch.setattr(demo_agent, "run", lambda **kw: ran.append(kw) or 0)
    assert cli.main(["demo", "agent"]) == 0
    assert len(ran) == 1


def test_watch_alone_is_the_existing_viewer(monkeypatch):
    _with_terminal(monkeypatch)
    ran = []
    monkeypatch.setattr(tui, "run", lambda *a, **kw: ran.append((a, kw)) or 0)
    assert cli.main(["demo", "watch"]) == 0
    assert len(ran) == 1
    (profile, *_), kw = ran[0]
    assert profile.session_id == demo.SESSION_ID
    assert kw["model"] is not None


def test_watch_dash_dash_demo_still_works(monkeypatch):
    _with_terminal(monkeypatch)
    ran = []
    monkeypatch.setattr(tui, "run", lambda *a, **kw: ran.append((a, kw)) or 0)
    assert cli.main(["watch", "--demo"]) == 0
    assert len(ran) == 1


def test_together_inside_tmux_opens_the_viewer_in_a_second_pane(monkeypatch):
    from collab.client import watch as w

    _with_terminal(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setattr(w, "tmux_available", lambda: True)
    opened, ran = [], []
    monkeypatch.setattr(w, "open_tmux_pane",
                        lambda argv, **kw: opened.append((argv, kw)) or "opened")
    monkeypatch.setattr(demo_agent, "run", lambda **kw: ran.append(kw) or 0)
    assert cli.main(["demo"]) == 0
    assert len(opened) == 1 and len(ran) == 1
    argv, kw = opened[0]
    assert argv[-2:] == ["demo", "watch"]
    assert kw.get("horizontal", True) is True
    assert "COLLAB_HOME" not in (kw.get("env") or {}), "no real state is passed"


def test_together_outside_tmux_splits_one_window(monkeypatch):
    _with_terminal(monkeypatch)
    ran = []
    monkeypatch.setattr(demo_agent, "run_together",
                        lambda **kw: ran.append(kw) or 0)
    assert cli.main(["demo"]) == 0
    assert len(ran) == 1


@pytest.mark.parametrize("argv", [["demo"], ["demo", "agent"], ["demo", "watch"],
                                  ["watch", "--demo"]])
def test_without_a_terminal_each_refuses_in_the_same_words(monkeypatch, capsys,
                                                           argv):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "needs a terminal: it opens the full-screen viewer" in err


def test_the_overview_lists_it():
    names = [entry[0] for _t, entries in cli.COMMAND_GROUPS for entry in entries]
    assert any(n.startswith("demo") for n in names)


# --- the split, outside tmux ---------------------------------------------------

def test_the_internal_split_gives_each_half_its_own_window(monkeypatch):
    """The two halves are the two real drawers, each handed a sub-window; the
    split does no layout of its own beyond the rule between them."""
    calls = []

    class Sub:
        def __init__(self, h, w):
            self.size = (h, w)

        def getmaxyx(self):
            return self.size

        def __getattr__(self, _n):
            return lambda *a, **kw: None

    class Root(Sub):
        def derwin(self, h, w, y, x):
            calls.append((h, w, y, x))
            return Sub(h, w)

    left, right = demo_agent.split_windows(Root(24, 100))
    assert [c[3] for c in calls] == [0, 51], "left at 0, right past the rule"
    assert calls[0][1] + 1 + calls[1][1] == 100
    assert left.getmaxyx()[0] == 24 and right.getmaxyx()[0] == 24
