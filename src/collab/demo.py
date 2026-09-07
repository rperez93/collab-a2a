"""A session that is not happening, so the viewer can be looked at without one.

Themes, folding, the scrollbar, the tone a line asks for, how a bubble behaves
at twenty-four columns — every one of those is judged BY EYE, and judging them
used to mean finding a second agent, opening a real session, and hoping
somebody said something long enough to fold. So the thing being looked at was
never the thing being tested: it was whatever the other person happened to
type.

Here the conversation is fixed. `collab watch --demo` opens the ordinary viewer
on it — the same panes, the same keys, the same renderer — with the log swapped
underneath for one that lives in memory. Nothing is fetched, nothing is
written, and no session is joined.

WHAT IS SIMULATED IS THE LOG, NOT THE MODEL. `DemoInbox` answers the same six
questions `Inbox` does, and the real `Model` does its own windowing, trimming
and paging over it — so what a reader exercises in the demo is the shipped
code, not a hollow copy of it that can quietly drift out of step with it.

The script below is deliberately built out of the shapes that break things: a
message long enough to fold, lines the tone rules paint, a name in Japanese, an
attachment, a task, somebody joining, a change of day, and a history longer
than the window so there is something to scroll back into.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any

from .config import SessionProfile
from .protocol import (KIND_CHAT, KIND_FILE, KIND_HELLO, KIND_PRESENCE,
                       KIND_TASK, Envelope)

#: Who the reader is. The bubbles on the right, the «(you)» in the roster.
YOU = "edith"
HOST = "jarvis"
SESSION_ID = "demo"
TITLE = "demo · a conversation with nobody on the other end"

#: `%Y-%m-%dT%H:%M:%SZ`, which is what the wire carries and what `_day`
#: converts to the reader's own timezone before taking a date off it.
_WIRE = "%Y-%m-%dT%H:%M:%SZ"


def _now(now: _dt.datetime | None = None) -> _dt.datetime:
    return now or _dt.datetime.now(_dt.timezone.utc)


def _ts(minutes_ago: float, now: _dt.datetime | None = None) -> str:
    return (_now(now) - _dt.timedelta(minutes=minutes_ago)).strftime(_WIRE)


def _backlog_day(now: _dt.datetime | None = None) -> _dt.datetime:
    """The day before the OLDEST of today's beats, not the day before now.

    The difference is two minutes a day and it matters: just after midnight
    every one of today's beats is still on yesterday's date, so a backlog
    anchored to «now minus a day» lands on that same date and the day
    separator — which only draws on a boundary — quietly stops existing.
    Anchored to the oldest beat there is a full day between the two, so the
    boundary is there at any hour.
    """
    oldest = max(m for m, *_ in _TODAY)
    return _now(now) - _dt.timedelta(minutes=oldest, days=1)


#: The backlog: yesterday afternoon, and the reason there is anything to scroll
#: back INTO. Short lines on purpose — this is the history you skim past on the
#: way to the top, and a wall of folded paragraphs would make that a chore.
_BACKLOG = [
    (HOST, "picking up the tunnel work where we left it"),
    (YOU, "the quick tunnels drop the SSE feed, I measured it twice"),
    (HOST, "measured how?"),
    (YOU, "two sessions, one behind the tunnel, one local. same client build"),
    ("mila", "and the local one delivered everything?"),
    (YOU, "every event. the tunnel connected 200 and then said nothing"),
    (HOST, "so it is the tunnel swallowing the stream, not our reader"),
    ("mila", "that matches what I saw on friday"),
    (HOST, "⚠ WARN: do not write that up as a client bug then"),
    (YOU, "already corrected the note"),
    ("mila", "何が問題なのかやっと分かった 🎉"),
    (HOST, "so ngrok stays the only real route for now"),
    (YOU, "for remote, yes. same machine does not need any of it"),
    ("mila", "ok. I will leave the authtoken to you"),
    (HOST, "fine. tomorrow then"),
]

#: And today: the conversation the demo actually opens on.
_TODAY: list[tuple[float, str, str, Any]] = [
    (128, HOST, KIND_HELLO, {"repo": "collab-a2a", "branch": "main",
                             "focus": "the watch pane"}),
    (126, HOST, KIND_CHAT, "morning. the viewer is what I want to look at today"),
    (124, YOU, KIND_CHAT, "same. the fold button has been dead for a while"),
    (121, HOST, KIND_CHAT,
     "here is what I found, and it is dumber than it looked.\n"
     "\n"
     "`handle` had TWO branches for a mouse event. the first one answered the "
     "wheel and returned, so the second one — the one that folded a message — "
     "was never reached at all.\n"
     "\n"
     "worse, it could not have worked even if it were reached: getmouse hands "
     "out an event once, so whichever branch ran first ate it and the other "
     "got nothing.\n"
     "\n"
     "the fix is one handler, not two."),
    (119, YOU, KIND_CHAT, "✓ that explains why it never fired even once"),
    (118, YOU, KIND_CHAT,
     "for the record, this is what I ran to confirm it:\n"
     "  git log -S'return self.handle_mouse()' --oneline -- src/collab/client/tui.py\n"
     "  python -m pytest tests/test_tui_scroll.py -q"),
    (116, HOST, KIND_CHAT, "and it lands on 86e3ab9, the commit that added the wheel"),
    (114, "mila", KIND_HELLO, {"repo": "collab-a2a", "branch": "themes",
                               "focus": "reading the theme files"}),
    (113, "mila", KIND_CHAT, "morning 🌅"),
    (112, "mila", KIND_CHAT,
     "while you two are in there — the info blue is unreadable on my terminal.\n"
     "it resolves to whatever the palette put in slot 4, which here is navy."),
    (110, YOU, KIND_CHAT, "✗ FAILED on my machine too, same reason"),
    (108, HOST, KIND_TASK, {"action": "claim", "id": "t_4", "state": "working",
                            "title": "one mouse handler, not two",
                            "owner": HOST}),
    (96, HOST, KIND_CHAT, "test written first. it fails for the right reason"),
    (94, YOU, KIND_CHAT,
     "post the diff when you have it, I want to read the guard"),
    (80, HOST, KIND_FILE, {"name": "fold-click.patch", "size": 4212,
                           "id": "f_7c1a"}),
    (78, YOU, KIND_CHAT,
     "read it. two things:\n"
     "\n"
     "1. the row index counts DOWN from the top of the conversation, so a "
     "click above it still lands on a row by arithmetic. guard the pane, not "
     "just the bounds.\n"
     "\n"
     "2. with mouseinterval(0) a click arrives as press AND release. fold on "
     "both and it toggles twice, which looks exactly like a button that does "
     "nothing.\n"
     "\n"
     "the second one is the ugly one — it would have shipped looking fixed."),
    (74, HOST, KIND_CHAT, "⚠ good catch. the release case is not in my tests"),
    (70, HOST, KIND_CHAT, "added. ✓ 9 passed"),
    (66, "mila", KIND_CHAT, "テーマの方も見ておくよ 🍵"),
    (52, HOST, KIND_TASK, {"action": "done", "id": "t_4", "state": "done",
                           "title": "one mouse handler, not two",
                           "owner": HOST}),
    (40, YOU, KIND_CHAT,
     "next: the bottom bar. it says «End/G: newest» and draws nothing.\n"
     "you cannot see where you are in a conversation, only be told."),
    (34, "mila", KIND_CHAT, "a drawn scrollbar? in a terminal?", ),
    (32, YOU, KIND_CHAT, "one line of it. a rail, a thumb, a percentage"),
    (30, HOST, KIND_CHAT, "and the jump button only when there is somewhere to jump"),
    (22, "mila", KIND_PRESENCE, {"event": "mila is away"}),
    (12, HOST, KIND_CHAT,
     "TODO for whoever picks this up:\n"
     "  - the track measures what is LOADED, not what is on disk\n"
     "  - so say so, rather than inventing a percentage over messages that "
     "are not in rows yet"),
    (6, YOU, KIND_CHAT, "agreed. a dim end on the rail, no fake arithmetic"),
    (2, HOST, KIND_CHAT, "ok. that is the whole demo, scroll back through it"),
]


def events(now: _dt.datetime | None = None) -> list[Envelope]:
    """The conversation, freshly built, oldest first.

    Fresh on every call because the viewer OWNS the list it is given — it
    splices pages into it and trims it from either end — so handing out one
    shared list would let one reader's scrolling rewrite another's history.

    ``now`` is for tests that need a particular hour; leave it alone and the
    conversation is always the last couple of hours.
    """
    out: list[Envelope] = []
    day = _backlog_day(now)
    seq = 0
    for i, (sender, text) in enumerate(_BACKLOG * 3):
        seq += 1
        # Spread over the afternoon; the exact minute does not matter, only
        # that it reads as a conversation and stays on the one date.
        stamp = day.replace(hour=14, minute=0, second=0) + _dt.timedelta(minutes=i * 7)
        out.append(Envelope(kind=KIND_CHAT, sender=sender, text=text, seq=seq,
                            ts=stamp.strftime(_WIRE)))
    for minutes, sender, kind, payload in _TODAY:
        seq += 1
        body = payload if isinstance(payload, dict) else {}
        text = payload if isinstance(payload, str) else ""
        out.append(Envelope(kind=kind, sender=sender, text=text, body=body,
                            seq=seq, ts=_ts(minutes, now)))
    return out


def profile() -> SessionProfile:
    """A profile that is never saved and never points anywhere real."""
    return SessionProfile(session_id=SESSION_ID, url="http://demo.invalid/",
                          name=YOU, host_name=HOST, token="demo",
                          participant_id="p_edith", home="")


def snapshot() -> dict[str, Any]:
    """The roster, as the hub would have described it.

    With the shapes the pane has branches for: somebody at work, somebody idle,
    somebody who has gone home, a host, a second person on this machine, and
    the figures an agent shares about itself.
    """
    now = time.time()
    return {
        "title": TITLE,
        "fetched_at": now,
        "participants": [
            {"name": HOST, "id": "p_jarvis", "is_host": True,
             "connected": True, "color": "#4888db",
             "repo": "collab-a2a", "branch": "main", "machine": "workshop",
             "last_seen": now,
             "activity": {"state": "working", "what": "the bottom bar",
                          "files": ["src/collab/client/tui.py"],
                          "since": now - 900, "updated_at": now},
             # A QUOTA IN THE PICTURE. The roster draws allowance windows and
             # the demo had never carried one, so the screenshot showed
             # everything about an agent except the figure the roster exists
             # for — the one you read before handing somebody more work.
             "stats": {"model": "claude-opus-5", "tokens_in": 184000,
                       "context_pct": 41, "cost_usd": 2.35,
                       "quotas": {"five_hour": {"used_pct": 62.0},
                                  "seven_day": {"used_pct": 38.0}}}},
            {"name": YOU, "id": "p_edith", "connected": True,
             "color": "#00cccc", "repo": "collab-a2a", "branch": "main",
             "machine": "workshop", "last_seen": now,
             "activity": {"state": "idle", "what": "reading the diff",
                          "since": now - 240, "updated_at": now},
             "stats": {"model": "claude-opus-5", "tokens_in": 96000,
                       "context_pct": 22,
                       "quotas": {"five_hour": {"used_pct": 21.0}}}},
            {"name": "mila", "id": "p_mila", "connected": False,
             "color": "#c678dd", "repo": "collab-a2a", "branch": "themes",
             "machine": "laptop", "last_seen": now - 1320,
             "focus": "reading the theme files",
             "stats": {"model": "claude-sonnet-5", "tokens_in": 31000}},
        ],
    }


class DemoInbox:
    """The log, in memory. The six questions `Inbox` answers, and no more.

    Deliberately the same shape as the real one rather than something simpler:
    the Model asks these six and nothing else, so matching them exactly is what
    lets the shipped paging code run untouched over a conversation that does
    not exist.
    """

    def __init__(self, script: list[Envelope] | None = None) -> None:
        self.events = script if script is not None else events()

    def _at(self, seq: int, events: list[Envelope] | None = None) -> int:
        """Where a seq sits in the log. Past the end when it is not there.

        Asked against the SAME list the caller is about to page over, not
        always the whole log: with kinds excluded the two have different
        lengths, and an index into one used to slice the other walks off by
        however many hidden events happened to be above it.
        """
        events = self.events if events is None else events
        for i, e in enumerate(events):
            if int(e.seq or 0) == seq:
                return i
        return len(events)

    def _shown(self, exclude: tuple[str, ...]) -> list[Envelope]:
        """The log as the caller is going to draw it.

        EXCLUDED BEFORE ANYTHING IS COUNTED OR SLICED, which is the same rule
        `Inbox._without` states for the real reader and states the reason for:
        dropping the kinds afterwards gives a page shorter than the limit asked
        for and a count of events that will never reach the screen — a pane
        saying «3 new below» and then showing nothing when you press End.

        The demo has to keep that rule rather than merely accept the argument.
        Its whole purpose is to run the shipped Model over a log in memory, and
        a stand-in that answers the same questions differently is a stand-in
        that hides the bug it exists to expose.
        """
        if not exclude:
            return self.events
        return [e for e in self.events if e.kind not in exclude]

    def all_events(self, limit: int = 100, *,
                   exclude: tuple[str, ...] = ()) -> list[Envelope]:
        events = self._shown(exclude)
        return list(events[-limit:]) if limit else list(events)

    def first(self, limit: int = 50, *,
              exclude: tuple[str, ...] = ()) -> list[Envelope]:
        return list(self._shown(exclude)[:limit])

    def before(self, seq: int, limit: int = 200, *,
               exclude: tuple[str, ...] = ()) -> list[Envelope]:
        events = self._shown(exclude)
        i = self._at(seq, events)
        return list(events[max(i - limit, 0):i])

    def after(self, seq: int, limit: int = 200, *,
              exclude: tuple[str, ...] = ()) -> list[Envelope]:
        events = self._shown(exclude)
        i = self._at(seq, events)
        return list(events[i + 1:i + 1 + limit])

    def count_after(self, seq: int, *, exclude: tuple[str, ...] = ()) -> int:
        events = self._shown(exclude)
        return max(len(events) - self._at(seq, events) - 1, 0)

    def has_before(self, seq: int, *, exclude: tuple[str, ...] = ()) -> bool:
        events = self._shown(exclude)
        return self._at(seq, events) > 0

    def close(self) -> None:
        """Nothing to close. Here because the viewer closes what it opens."""


def activity() -> dict[str, Any]:
    """What the reader's own agent last said it was doing, four minutes ago.

    The same statement the roster shows for `edith`, so the two surfaces that
    carry it — the roster's foot and the agent's status line — agree with the
    line above them.
    """
    now = time.time()
    return {"state": "idle", "what": "reading the diff",
            "since": now - 240, "updated_at": now - 240}


def status() -> dict[str, Any]:
    """What the daemon would have written about the simulated session, for
    the viewer: live, with the figures the roster's foot is drawn from.

    A batch part way through and a count of what has been said, both stamped
    now so they read as fresh rather than as memories, and the reader's own
    activity. Without them the foot is empty and the demo shows a viewer with
    a feature missing rather than one with nothing to say.
    """
    now = time.time()
    return {
        "state": "live",
        "batch": {"id": "B_demo", "name": "the bottom bar", "state": "open",
                  "total": 10, "done": 6, "withdrawn": 0, "outstanding": 4,
                  "percent": 60, "complete": False,
                  "counted_at": now, "fetched_at": now},
        "messages": {"total": 128, "fetched_at": now},
        "activity": activity(),
    }


def model():
    """The real Model, reading a log that is not on disk.

    Imported here rather than at the top of the file: `client.tui` pulls in
    curses, and this module is also read by tests and by the CLI's help, which
    have no terminal to speak of.
    """
    from .client.tui import Model

    class DemoModel(Model):
        """The real one, with the three methods that touch the disk stubbed.

        Everything else — the window, the trimming, `more_above`, `pending`,
        the reach back and forward — is the shipped code running over
        `DemoInbox`.
        """

        def _sync_seen(self) -> None:
            self._seen = 0

        def refresh_side(self) -> None:
            """A live session, so the badge is green and the roster is an
            observation rather than a memory. Both of those have branches the
            demo exists to show."""
            self.snapshot = snapshot()
            self.status = status()
            self._state = "live"

        def poll_events(self, follow: bool = True) -> int:
            """Nothing new ever arrives: the conversation is finished. A demo
            that grew under the reader could not be scrolled through."""
            return 0

    m = DemoModel(profile=profile())
    m._inbox = DemoInbox()
    m.refresh_side()
    return m
