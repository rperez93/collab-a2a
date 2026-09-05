"""A coding agent's terminal that is not a coding agent — the other half of the
screenshot.

`collab watch --demo` puts the viewer on a conversation nobody is having. A
screenshot of collab wants the OTHER window in that picture too: the agent
whose session the viewer is showing, mid-task, with a message from the session
arriving in its transcript and its answer going back out through `collab send`.
That window is this module, and it is a picture. Nothing in it thinks, nothing
runs, and no key does anything but quit.

Three rules keep the picture honest:

* THE COLLAB LINES ARE THE VIEWER'S LINES. Every message this transcript quotes
  is looked up in `demo.events()` — the same script the viewer draws beside
  it — so the «message from jarvis» on the left is, verbatim, the line on the
  right. Two panels, one story.

* THE STATUS LINE IS THE PRODUCT'S. The bottom row is `statusline.render` fed
  a status of the simulated session, not a hand-drawn imitation of it: a
  screenshot shows what collab actually prints there, and a change to the
  renderer changes the picture.

* NOTHING IS READ THAT IS NOT ALREADY IN MEMORY, AND NOTHING IS WRITTEN. No
  hub, no `.collab`, no config of the reader's.

The agent on screen is deliberately nobody's: a made-up name, generic tool
names, no product's chrome. The point of the picture is collab in the corner
of an agent's window, not the agent.
"""

from __future__ import annotations

import curses
import datetime as _dt
import re
import time
from dataclasses import dataclass
from typing import Any

from .. import __version__, demo
from ..columns import clip as _clip, width as _w
from ..protocol import KIND_FILE, Envelope, local_clock
from ..statusline import render as statusline
from . import tui
from .tui import (C_ACCENT, C_DIM, C_GOOD, C_INFO, C_OFFLINE, C_ONLINE,
                  C_TEXT, C_TITLE, C_WARN, Row)

#: The agent on screen. A name that is nobody's product.
AGENT = "quill"
REPO = "collab-a2a"
BRANCH = "main"

#: Below this the transcript loses its clocks: at fifty columns a name, a time
#: and a message do not share a line and still leave room for the message.
NARROW_AT = 60


@dataclass
class Line:
    """One beat of the transcript.

    ``kind`` is one of: ``prompt`` (the person typed), ``agent`` (it answered),
    ``tool`` (it called something), ``result`` (what came back), ``inbound`` (a
    collab message arrived), ``outbound`` (one was sent). Inbound and outbound
    carry ``who`` and ``ts`` from the envelope they quote, so the row can name
    the sender and the minute the viewer shows.
    """

    kind: str
    text: str = ""
    who: str = ""
    ts: str = ""
    #: For an inbound file rather than a message: the verb before the name.
    mark: str = ""


def _said(events: list[Envelope], sender: str, opening: str) -> Envelope:
    """The envelope whose text begins with ``opening``. Looked up, not copied:
    a copy is a second script, and two scripts drift."""
    for e in events:
        if e.sender == sender and e.text.startswith(opening):
            return e
    raise LookupError(f"{sender} never says {opening!r} in the demo")


def _shared(events: list[Envelope], sender: str, name: str) -> Envelope:
    for e in events:
        if e.kind == KIND_FILE and e.sender == sender and e.body.get("name") == name:
            return e
    raise LookupError(f"{sender} never shares {name!r} in the demo")


def script(now: _dt.datetime | None = None) -> list[Line]:
    """The transcript, oldest first.

    ``now`` is the same clock `demo.events` takes, so the minutes on the
    inbound lines are the minutes the viewer stamps on the same messages. A
    fixed ``now`` gives the same picture twice, which is what a screenshot
    wants; left alone it is the last couple of hours, like the viewer.
    """
    events = demo.events(now=now)
    host, you = demo.HOST, demo.YOU

    def heard(e: Envelope) -> Line:
        return Line("inbound", e.text, e.sender, e.ts)

    def sent(e: Envelope) -> Line:
        return Line("outbound", e.text, e.sender, e.ts)

    patch = _shared(events, host, "fold-click.patch")
    # The last hour of the viewer's script, from this side of it: the patch
    # arrives and is read, then the bottom bar is proposed and settled. Short
    # beats at the end on purpose — a window twenty-four rows high shows the
    # tail, and the tail is the screenshot.
    return [
        Line("prompt", f"{host} has posted the fold patch. read it, then take the "
                       "bottom bar with him — it says «End/G: newest» and draws "
                       "nothing"),
        Line("tool", "shell  collab recv --wait 600"),
        Line("inbound", str(patch.body.get("name")), patch.sender, patch.ts,
             mark="shared"),
        Line("tool", f"shell  collab file get {patch.body.get('id')}"),
        Line("result", f"{patch.body.get('name')} · "
                       f"{int(patch.body.get('size') or 0) / 1024:.0f} KB"),
        Line("tool", f"read   {patch.body.get('name')}"),
        Line("result", "112 lines"),
        Line("agent", "Two things wrong with it: the row index still resolves a "
                      "click above the pane, and with mouseinterval(0) a click is "
                      "press and release, so the fold toggles twice. Sending both."),
        Line("tool", "shell  collab send …"),
        sent(_said(events, you, "read it. two things")),
        Line("tool", "shell  collab recv --wait 600"),
        heard(_said(events, host, "⚠ good catch")),
        heard(_said(events, host, "added.")),
        Line("agent", "The patch is settled. On to the bar: the legend names a "
                      "key and shows nothing of where you are. Proposing a drawn "
                      "scrollbar."),
        Line("tool", "shell  git grep -n 'End/G' src/collab/client/tui.py"),
        Line("result", "tui.py:84: CHAT_KEYS_SHORT = \"End/G: newest · …\""),
        Line("tool", "shell  collab send …"),
        sent(_said(events, you, "next: the bottom bar")),
        Line("tool", "shell  collab recv --wait 600"),
        heard(_said(events, "mila", "a drawn scrollbar?")),
        Line("tool", "shell  collab send …"),
        sent(_said(events, you, "one line of it")),
        heard(_said(events, host, "and the jump button")),
        heard(_said(events, host, "TODO for whoever")),
        Line("agent", "Agreed on the constraint. The rail measures what is loaded, "
                      "so it ends dim where memory does instead of inventing a "
                      "percentage over rows that do not exist yet."),
        Line("tool", "shell  collab send …"),
        sent(_said(events, you, "agreed. a dim end")),
        Line("agent", "Design settled: a rail, a thumb, a percentage, a dim end "
                      "for what is not loaded, and the jump button only while "
                      "there is somewhere to jump. Starting on it."),
    ]


def status() -> dict[str, Any]:
    """What the daemon would have written about the simulated session.

    Everything the renderer reads, at the values that make the segment its
    ordinary self: live, current on both versions, one other person here and
    nothing unread. The heartbeat is added when the line is drawn, so a panel
    left open does not age into «reconnecting».
    """
    return {"state": "live", "version": __version__, "hub_version": __version__,
            "name": demo.YOU, "host": demo.HOST, "is_host": False,
            "others_connected": 1, "unread": 0, "unread_messages": 0,
            "session_id": demo.SESSION_ID}


#: `Panel` takes a ``status`` argument of the same name; this is the function.
_default_status = status


# --- the status line, from ANSI to curses -------------------------------------

#: The colours `statusline.COLORS` speaks, as the viewer's pairs. The walk over
#: the codes themselves lives in `client.statusbar`, which is where the other
#: caller is: the roster's foot paints per segment and has the same problem
#: with the same answer, and two walks over one escape grammar is one too many.
_SGR_PAIR = {"32": C_ONLINE, "33": C_WARN, "31": C_OFFLINE, "36": C_ACCENT}


def status_segments(status: dict[str, Any], width: int) -> list[tuple[str, int]]:
    """The rendered status line, cut where its colour changes.

    The renderer paints with escape codes because it writes to a terminal;
    curses wants attributes. This walks the codes and hands back plain text
    with the pair each run should be painted in, so the green dot stays green
    and no escape reaches the screen as characters.
    """
    from .statusbar import sgr_runs

    line = statusline.render({"heartbeat": time.time(), **status}, width=width)
    return [(run, _attr(_SGR_PAIR.get(colour, 0), dim, bold))
            for run, colour, bold, dim in sgr_runs(line)]


def _attr(pair: int, dim: bool, bold: bool = False) -> int:
    attr = curses.color_pair(pair) if pair else 0
    return (attr | (curses.A_DIM if dim else 0)
            | (curses.A_BOLD if bold else 0))


# --- rows -----------------------------------------------------------------------

def rows(lines: list[Line], width: int) -> list[Row]:
    """The transcript laid out for ``width`` columns. Every row fits.

    Wrapped and clipped through the viewer's own `_wrap` and `columns.clip`,
    which is what makes a kanji in a quoted message two columns here as well
    as there.
    """
    narrow = width < NARROW_AT
    out: list[Row] = []

    def body(text: str, indent: int, pair: int = C_TEXT, tone: bool = False) -> None:
        pad = " " * indent
        for piece in tui._wrap(text, max(width - indent, 1)):
            colour = (tui.line_pair(piece) or pair) if tone else pair
            out.append(Row(_clip(pad + piece, width), colour))

    for line in lines:
        if line.kind == "prompt":
            pieces = tui._wrap(line.text, max(width - 2, 1))
            out.append(Row(_clip("› " + pieces[0], width), C_TEXT, curses.A_BOLD))
            for piece in pieces[1:]:
                out.append(Row(_clip("  " + piece, width), C_TEXT, curses.A_BOLD))
        elif line.kind == "agent":
            out.append(Row(_clip(f"◆ {AGENT}", width), C_ACCENT, curses.A_BOLD))
            body(line.text, 2, C_TEXT)
        elif line.kind == "tool":
            out.append(Row(_clip(f"  ⚙ {line.text}", width), C_INFO))
            continue
        elif line.kind == "result":
            out.append(Row(_clip(f"    └ {line.text}", width), C_DIM))
            continue
        elif line.kind in ("inbound", "outbound"):
            glyph = "✉" if line.kind == "inbound" else "↑"
            when = "" if narrow else f" · {local_clock(line.ts)}"
            verb = "collab send" if line.kind == "outbound" else "collab"
            head = f"{glyph} {verb} · {line.who}{when}"
            pair = tui._speaker_pair(line.who) if line.kind == "inbound" else C_GOOD
            out.append(Row(_clip(head, width), pair, curses.A_BOLD))
            if line.mark:
                out.append(Row(_clip(f"    {line.mark} {line.text}", width), C_DIM))
            else:
                body(line.text, 4, C_TEXT, tone=True)
        out.append(Row("", 0))
    return out


class Panel:
    """The window. Draws the script; quits on q."""

    def __init__(self, now: _dt.datetime | None = None,
                 status: dict[str, Any] | None = None) -> None:
        self.now = now
        self.status = _default_status() if status is None else status
        self.lines = script(now)
        tui.record_colours(demo.snapshot()["participants"])

    def handle(self, key: int) -> bool:
        """False to quit. Everything else is a picture being pressed."""
        return key not in (ord("q"), ord("Q"), 3)

    def draw(self, win) -> None:
        """A curses failure here loses a frame, never the panel — the same
        rule the viewer draws under, for the same reason."""
        try:
            self._draw(win)
        except curses.error:
            pass

    def _draw(self, win) -> None:
        win.erase()
        height, width = win.getmaxyx()
        if height < 6 or width < 24:
            room = max(width - 1, 0)
            if room:
                win.addnstr(0, 0, "window too small"[:room], room)
            return

        # The title: the agent, and where it is working. The badge on the right
        # is the session the viewer beside it shows.
        head = f" {AGENT} · {REPO} ({BRANCH}) "
        badge = f" {demo.YOU} in {demo.SESSION_ID} "
        title_attr = curses.color_pair(C_TITLE) | curses.A_BOLD
        win.hline(0, 0, " ", width, title_attr)
        win.addnstr(0, 0, _clip(head, max(width - 1, 0)), max(width - 1, 0), title_attr)
        if not (width < NARROW_AT) and _w(head) + _w(badge) < width:
            win.addnstr(0, width - _w(badge) - 1, badge, _w(badge), title_attr)

        # The transcript, newest at the bottom, like a terminal.
        drawn = rows(self.lines, width)
        while drawn and not drawn[-1].text:
            drawn.pop()
        room = height - 3
        shown = drawn[-room:] if len(drawn) > room else drawn
        # Not opening on a blank: the cut can land on the gap between two beats,
        # and a screenshot's first row should be a line rather than the space
        # after one.
        while shown and not shown[0].text:
            shown = shown[1:]
        for i, row in enumerate(shown):
            if row.text:
                win.addnstr(1 + i, 0, row.text, width,
                            curses.color_pair(row.pair) | row.attr)

        # The prompt, waiting.
        win.addnstr(height - 2, 0, "› ", min(2, width), curses.color_pair(C_TEXT) | curses.A_BOLD)
        win.addnstr(height - 2, 2, "▌", 1, curses.color_pair(C_DIM))

        # And collab's own line, from its own renderer. Never into the last
        # cell of the last row: that write is the one that ends a curses
        # program.
        x = 0
        for text, attr in status_segments(self.status, width - 1):
            room = width - 1 - x
            if room <= 0:
                break
            piece = _clip(text, room)
            win.addnstr(height - 1, x, piece, len(piece), attr)
            x += _w(piece)


# --- running it -----------------------------------------------------------------

def _prepare(win) -> None:
    tui._init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    win.keypad(True)
    win.timeout(int(tui.POLL_SECONDS * 1000))


def run(*, now: _dt.datetime | None = None) -> int:
    """The agent panel alone, filling the window."""
    panel = Panel(now=now)

    def loop(win) -> int:
        _prepare(win)
        while True:
            panel.draw(win)
            key = win.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                tui._on_resize(win)
            elif not panel.handle(key):
                return 0

    return curses.wrapper(loop)


def split_windows(win) -> tuple[Any, Any]:
    """Two sub-windows side by side, with one column between them for a rule.

    Each half is handed to the drawer that already knows how to fill a
    window — the panel above, the viewer's `Tui.draw` — so this does no layout
    of its own. Sub-windows share the parent's storage, which is what lets one
    `getch` on the parent refresh both.
    """
    height, width = win.getmaxyx()
    left = width // 2
    right_x = left + 1
    return (win.derwin(height, left, 0, 0),
            win.derwin(height, max(width - right_x, 1), 0, right_x))


def run_together(*, now: _dt.datetime | None = None,
                 limit: int = tui.OPEN_WITH) -> int:
    """Both panels in one window: the agent on the left, the viewer on the right.

    For a terminal that is not inside tmux, where there is no second pane to
    put the viewer in. The viewer is the shipped `Tui` on the demo model, and
    it gets the keys; the panel only ever wants q. The mouse is left off — a
    click's coordinates belong to the whole window, and neither half is told
    where it sits in it.
    """
    panel = Panel(now=now)
    model = demo.model()
    model.load_initial(limit=limit)
    viewer = tui.Tui(model, view="both")

    def loop(win) -> int:
        _prepare(win)
        left, right = split_windows(win)
        while True:
            height, width = win.getmaxyx()
            win.erase()
            try:
                win.vline(0, width // 2, curses.ACS_VLINE, height,
                          curses.color_pair(C_DIM))
            except curses.error:
                pass
            panel.draw(left)
            viewer.draw(right)
            key = win.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                tui._on_resize(win)
                left, right = split_windows(win)
            elif not panel.handle(key) or not viewer.handle(key):
                return 0

    return curses.wrapper(loop)
