"""What the conversation pane costs to look at.

Laying the conversation out is linear in the whole history — every message
wrapped, folded and framed — and it was being done again on every redraw: four
times a second while nothing happened, and once per keystroke while somebody
scrolled. Measured at 300 messages that was two seconds a frame, most of it
`git` subprocesses resolving the reader's own name once PER MESSAGE.

Nothing in the layout depends on where you are scrolled, so these tests pin the
two halves of the fix: the rows are built when an input to them changes, and
the name behind them is not re-derived from the filesystem for each line.
"""

from __future__ import annotations

from collab.client import tui as T
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope


def Msg(seq):
    return Envelope(seq=seq, ts="2026-08-31T10:00:00+00:00", kind=KIND_CHAT,
                    sender="alice", body={"text": f"message {seq}"})


class FakeModel:
    def __init__(self, n=50):
        self.profile = SessionProfile(session_id="s", url="u", name="me",
                                      host_name="host", token="t", home="/tmp")
        self.events = [Msg(i + 1) for i in range(n)]
        self.snapshot = {}
        self.status = {}

    def more_above(self):
        return False

    def load_older(self, count=200):
        return 0


def _counting(monkeypatch):
    """conversation_rows, with a tally of how often it actually ran."""
    calls = []
    real = T.conversation_rows

    def counted(events, width, me, expanded=None, **kw):
        calls.append(width)
        return real(events, width, me, expanded, **kw)

    monkeypatch.setattr(T, "conversation_rows", counted)
    return calls


def test_scrolling_does_not_lay_the_conversation_out_again(monkeypatch):
    calls = _counting(monkeypatch)
    tui = T.Tui(FakeModel())

    tui._conversation(80)
    for _ in range(20):
        tui.chat.scroll(3)
        tui._conversation(80)

    assert len(calls) == 1, "twenty wheel notches, one layout"


def test_a_new_message_does_lay_it_out_again(monkeypatch):
    calls = _counting(monkeypatch)
    tui = T.Tui(FakeModel())

    tui._conversation(80)
    tui.model.events.append(Msg(999))
    tui._conversation(80)

    assert len(calls) == 2


def test_a_resize_does_too(monkeypatch):
    """The rows are wrapped to a width; a different width is different rows."""
    calls = _counting(monkeypatch)
    tui = T.Tui(FakeModel())

    tui._conversation(80)
    tui._conversation(120)

    assert calls == [80, 120]


def test_unfolding_a_message_does_too(monkeypatch):
    calls = _counting(monkeypatch)
    tui = T.Tui(FakeModel())

    tui._conversation(80)
    tui.expanded.add(3)
    tui._conversation(80)

    assert len(calls) == 2


def test_the_readers_own_name_is_not_resolved_once_per_message(monkeypatch):
    """`resolve_name` runs `git rev-parse`, `git config user.name` and walks the
    state directories. Per message, per frame, that was 900 forks a redraw."""
    T._OWN_NAME.clear()
    calls = []

    def counted():
        calls.append(1)
        return "me"

    monkeypatch.setattr(T, "resolve_name", counted)
    for _ in range(500):
        T.my_names("me")

    assert len(calls) == 1


def test_but_it_is_re_read_soon_enough_to_notice_a_rename(monkeypatch):
    """`collab name` in another terminal has to reach an open viewer."""
    T._OWN_NAME.clear()
    monkeypatch.setattr(T, "resolve_name", lambda: "before")
    assert "before" in T.my_names("me")

    monkeypatch.setattr(T, "resolve_name", lambda: "after")
    monkeypatch.setattr(T.time, "monotonic",
                        lambda: T._OWN_NAME["at"] + T.OWN_NAME_TTL + 0.01)
    assert "after" in T.my_names("me")


# --- and what the roster's foot costs -------------------------------------------------

def _counted_batch(monkeypatch):
    """`statusbar.batch_segment`, with a tally of how often it actually ran."""
    from collab.client import statusbar as SB

    calls: list[int] = []
    real = SB.batch_segment

    def counted(figures, **kw):
        calls.append("narrow" if kw.get("narrow") else kw.get("room", 0))
        return real(figures, **kw)

    monkeypatch.setattr(SB, "batch_segment", counted)
    return calls


def _figures():
    import time

    return {"done": 6, "total": 10, "fetched_at": time.time()}


def test_the_batch_is_rendered_once_per_width_and_not_once_per_probe(monkeypatch):
    """The foot's layout measures a candidate before it draws it, so the piece
    that scales to its cell is asked for the same cell several times over one
    frame. Each ask re-derived the hub's counts six times: four renderings and
    twenty-four `count_of` calls per frame, for a row of four figures.

    The closure lives exactly as long as the row it belongs to and reads
    figures it captured rather than looks up, so remembering what it already
    answered cannot go stale."""
    from collab.client import statusbar as SB

    calls = _counted_batch(monkeypatch)
    named = SB.compose_named(batch=_figures(), messages={"total": 9},
                             keys="q quit", segments=("batch", "messages", "keys"))
    piece = dict(named)["batch"]

    for _ in range(10):
        piece(40)
    for _ in range(10):
        piece(80)

    # The narrow form, the zero-room probe `compose_named` makes to find out
    # whether this segment has anything to say at all, and one per width.
    assert calls == ["narrow", 0, 40, 80], calls


def test_the_narrow_form_is_built_once_however_often_it_is_asked(monkeypatch):
    """It does not depend on the room at all, and was being rebuilt on every
    call because it sat inside the function of the room."""
    from collab.client import statusbar as SB

    calls = _counted_batch(monkeypatch)
    piece = dict(SB.compose_named(batch=_figures(),
                                  segments=("batch",)))["batch"]
    for room in range(20, 120, 10):
        piece(room)

    assert calls.count("narrow") == 1, "however many widths it is asked at"


def test_remembering_does_not_cost_the_bar_its_scale(monkeypatch):
    """The whole reason this piece is a function of its room: the bar grows
    into the cell it is given. A cache that returned one width for every room
    would be cheaper and wrong."""
    from collab.client import statusbar as SB

    piece = dict(SB.compose_named(batch=_figures(),
                                  segments=("batch",)))["batch"]
    narrow_cell = piece(30)[0]
    wide_cell = piece(90)[0]

    assert len(wide_cell) > len(narrow_cell)
    assert piece(90)[0] == wide_cell, "and the same answer the second time"
