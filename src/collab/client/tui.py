"""A terminal UI for watching a collab session.

Two panes: the roster on top, the conversation below. Each scrolls on its own,
because the two answer different questions — *who is here and what are they
burning* versus *what was just said* — and you often want to hold one still
while reading the other.

Everything is read from files the daemon maintains, so the viewer never touches
the network and keeps working through a reconnect.
"""

from __future__ import annotations

import curses
import random
import subprocess

from ..columns import clip as _columns_clip, width as _columns_width

from ..config import (HEADER_SEPARATOR, default_color, fold_override,
                      parse_color, resolve_name,
                      hex_to_rgb, rgb_to_256, theme)
import datetime as _dt
import re
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import SessionProfile
from ..protocol import (
    Envelope,
    local_clock,
    local_datetime,
    local_today,
    KIND_ACTIVITY,
    KIND_CHAT,
    KIND_FILE,
    KIND_HELLO,
    KIND_PRESENCE,
    KIND_TASK,
    task_line,
)
try:
    from ..protocol import file_outcome
except ImportError:
    # UNTIL THE ROOM-FILE BRANCH LANDS. `protocol.file_outcome` is the one
    # wording for what an ack did to the host's copy — «deleted from the
    # host», or how many are still to collect — shared with `watch` so the
    # pane and the transcript cannot disagree. A protocol from before it only
    # ever deleted. Remove this fallback with the merge.
    def file_outcome(body: dict) -> str:
        return "deleted from the host"
from .. import activity, peers
from .. import themes
from ..config import watch_roster_settings, watch_status_settings
from ..stats import read_stats
from . import statusbar
from .statusbar import money_text
from .daemon import DaemonPaths, effective_state, is_running, read_status
from .inbox import Inbox

#: How much of the window the roster gets. The conversation is the thing you
#: read continuously, so it keeps the majority.
ROSTER_SHARE = 0.30
MIN_ROSTER_ROWS = 3
#: Lines per wheel notch. Three is what terminals and pagers settled on.
WHEEL_LINES = 3
POLL_SECONDS = 0.25

#: The key legend, per view, in two lengths. It is a segment of the bottom row
#: like any other and is the first thing given up when the pane is narrow: it
#: says the same six things every session, so it is the piece a reader has
#: already read.
#:
#: The short form exists because giving it up entirely was too blunt. The full
#: legend is ninety columns, so on anything under about a hundred and thirty —
#: which is most panes, with a batch figure and a quota beside it — the keys
#: vanished altogether, and the legend is how anybody learns the viewer. The
#: short form keeps the two that are not guessable: how to get back to the live
#: end, and how to leave.
CHAT_KEYS = ("wheel/tab: pane · ↑↓ pgup/pgdn: scroll · [ ]: roster · "
             "End/G: newest · Home/g: top · q: quit")
CHAT_KEYS_SHORT = "End/G: newest · tab: pane · q: quit"
ROSTER_KEYS = "wheel · ↑↓ pgup/pgdn: scroll · Home/End: top/end · q: quit"
ROSTER_KEYS_SHORT = "Home/End: top/end · q: quit"

#: KINDS THAT ARE STATE, NOT CONVERSATION.
#:
#: `collab working` and `collab idle` publish KIND_ACTIVITY to the hub and it
#: reaches everybody, exactly like a message — but it is not one. It is this
#: agent's state, and the answer to «what is bob doing» is whatever the LAST
#: one said, not the list of every one he has ever sent.
#:
#: The conversation pane had no case for it, so it fell through to the branch
#: that prints `env.text or str(env.body)`. That landed two ways, and the
#: quieter one was the worse: the hub copies `what` into the envelope's `text`,
#: so `collab working "the token refresh"` drew a perfectly ORDINARY BUBBLE
#: reading «the token refresh» over bob's name — indistinguishable from bob
#: having said it. Only `collab idle` with no note, where `text` is empty, fell
#: all the way through to `str(env.body)` and drew a raw Python dict. A dict on
#: screen is obviously broken and somebody fixes it; a plausible sentence
#: attributed to a colleague is the one that survives.
#:
#: An agent that says what it is working on, which is the thing this project
#: asks agents to do constantly, was spamming the transcript every human reads.
#:
#: Nothing is lost by dropping them here: the roster pane above shows each
#: participant's current activity, live, which is the right shape for a state —
#: one line per person, replaced, rather than one line per change, accumulated.
#: `collab listen` and `collab watch --no-follow` still render them, with the
#: `◉` mark and `activity.describe`, because an agent's event stream genuinely
#: wants each transition.
NOT_CONVERSATION = (KIND_ACTIVITY,)

#: HOW MANY MESSAGES THE PANE HOLDS AT ONCE, and how many it opens with.
#:
#: Laying the conversation out is linear in what is loaded — every message
#: wrapped, folded and framed — so what is loaded is what the pane costs. A
#: bounded window makes that cost a constant instead of something that grows
#: with the session: the reader moves the window, the window does not grow
#: under the reader. Nothing is lost by it; the log on disk keeps everything
#: and the window slides over it, a page at a time, at either end.
WINDOW = 50
OPEN_WITH = 5
PAGE = 25

# Colour pair ids.
C_TITLE = 1
C_DIM = 2
C_ONLINE = 3
C_OFFLINE = 4
C_ACCENT = 5
C_WARN = 6
#: The tone of a body line. Colour goes PER LINE and not per message: one of
#: ours carries a ✓ and a ✗ in the same paragraph, so painting the whole thing
#: red hides what worked and green hides the failure.
C_GOOD = 7
C_BAD = 8
C_WARNLINE = 9
C_INFO = 20      # blue · data, commands, figures
C_BUTTON = 21
#: AND THAT BLUE AS A HEX, not as `curses.COLOR_BLUE`. The terminal's blue is
#: whatever its palette says it is, and on the usual dark ground that is navy:
#: the commands and figures an information line is made of came out darker than
#: the frame around them. This one is the same hue and readable on both grounds.
#: Terminals that cannot redefine a colour get the nearest of the 256, and the
#: eight-colour ones fall back to COLOR_BLUE, which is what they had anyway.
INFO_HEX = "#4888db"
C_TEXT = 22      # white · the body of a message that says nothing special

#: How many lines of a message show before it folds, and how much of the width
#: the bubble takes. The gap left on the opposite side is what makes it readable
#: at a glance who sent it.
#: What a theme that says nothing about folding gets. READ FROM themes.py
#: rather than repeated here: two copies of the same number drift apart, and
#: that already cost a `fold off` broken in one layout and fine in the other.
FOLD_LINES = themes.DEFAULTS["fold"]
BUBBLE_SHARE = 0.90

#: AND A HARD CAP. No messaging app widens the bubble when you maximise the
#: window: it keeps a comfortable width and uses the rest as air. Without this,
#: at 200 columns you got 145-character lines — twice what the eye can follow
#: without losing its place.
# At 86 columns —this machine's pane— 85 %% is 73, so the cap does NOT bite:
# the bubble comes out at the width asked for. It only starts above 129 columns,
# which is where a line begins to be longer than the eye can follow without
# losing the row. A cap that trims the width you were just asked for is not a
# protection, it is a requirement change through the back door.
#: THE CAP, AS A SHARE OF THE WHOLE SCREEN — not of the pane.
#:
#: The difference matters and is not theoretical: the conversation pane is
#: usually a third of the window, so a cap computed on the pane narrows when you
#: shrink the pane, which is exactly when there is least room. Against the
#: screen the cap is the same however you look at it: a bigger window gives more
#: room, dragging the border does not change the reading width.
#:
#: The share itself is the theme's `bubble_max_share` — READ FROM themes.py,
#: like the fold, and for the same reason: a copy of the number here looked
#: live and did nothing.

#: And a floor, because 40 % of a small window comes to nothing. Below this,
#: BUBBLE_SHARE over the pane decides and this cap does not apply.
BUBBLE_MAX_MIN = 40


def _screen_width(panel: int) -> int:
    """The width of the WINDOW where it can be known; otherwise, of the pane.

    Inside tmux a pane does not know how wide the window is: `curses.COLS` is
    the pane's. tmux is asked, and the answer is kept — it is a subprocess, and
    calling it on every redraw would cost a process per keystroke. It is
    refreshed when the pane changes size, which is when the window may have
    changed too.
    """
    if _SCREEN.get("panel") == panel and _SCREEN.get("width"):
        return int(_SCREEN["width"])
    width = panel
    if os.environ.get("TMUX"):
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "#{window_width}"],
                               capture_output=True, text=True, timeout=1.0)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                width = max(panel, int(r.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            width = panel
    _SCREEN.update(panel=panel, width=width)
    return width


_SCREEN: dict[str, int] = {}

#: Below this the two-sided chat format is abandoned. 56 columns is where a
#: bubble at 90 %% stops having room for its frame, its indent and a line anyone
#: can read.
NARROW_AT = 56

#: Speaker pairs start well clear of the fixed ones: with a palette of twelve
#: and a base of 10 they overlapped C_INFO(20), C_BUTTON(21) and C_TEXT(22), and
#: the twelfth speaker would have repainted the white of the body.
C_SPEAKER_BASE = 30
#: TWELVE OF THE 256, PICKED FOR CONTRAST AND FOR HUE SEPARATION.
#:
#: The very dark ones are avoided (lost on black), the very light ones (they
#: get confused with the white of the body) and the greys. The twelve are far
#: enough apart in hue not to be mistaken for one another at a glance: sky,
#: turquoise, green, lime, olive, gold, orange, salmon, pink, violet, lavender
#: and mint.
#:
#: AN HONEST WARNING: they are calibrated for a DARK BACKGROUND, which is the
#: normal case for a terminal. On white the middle ones lose contrast. There is
#: no reliable way to ask a terminal what colour its background is, so an
#: assumption is picked and declared rather than pretending it serves both.
SPEAKER_COLORS_256 = (39, 43, 48, 118, 148, 178, 208, 203, 205, 141, 111, 85)

#: If the terminal only has eight, these are the only ones that stand out.
#: White is left out —it is the body colour— and so is black.
SPEAKER_COLORS_8 = (curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_GREEN,
                    curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_RED)

#: The palette in use and the order it is dealt in. The order is shuffled ONCE
#: per process, so two people never share a colour while any are free — which is
#: what used to fail: the colour came from `hash(name) % 6`, and two different
#: names could land in the same slot with ten colours unused.
SPEAKER_COLORS = SPEAKER_COLORS_8
_ORDER: list[int] = list(range(len(SPEAKER_COLORS_8)))
_SLOTS: dict[str, int] = {}


def _deal_colours(total: int) -> None:
    """Pick a palette for what the terminal supports and shuffle the deal."""
    global SPEAKER_COLORS, _ORDER
    SPEAKER_COLORS = (SPEAKER_COLORS_256 if total >= 256 else SPEAKER_COLORS_8)
    _ORDER = list(range(len(SPEAKER_COLORS)))
    random.shuffle(_ORDER)
    _SLOTS.clear()


KIND_MARK = {
    KIND_CHAT: " ",
    KIND_HELLO: "→",
    KIND_PRESENCE: "·",
    KIND_TASK: "◆",
    KIND_FILE: "▣",
}


#: Colours people have CHOSEN, read from the roster. They win over the
#: automatic deal: whoever asks for a colour asks so they are recognised, and
#: having a lottery change it at startup would be the exact opposite.
_CHOSEN: dict[str, int] = {}
#: Names the roster says belong to somebody else. Refilled on every refresh
#: from the participant ids, and read by my_names(): a name I am not holding is
#: not one of mine, however much my local config would like it to be.
_OTHERS: set[str] = set()

#: And the pairs we had to create for those colours, since they do not come
#: from the palette: `curses.init_pair` has to be called once per new colour.
_PAIRS_BY_COLOUR: dict[int, int] = {}
_NEXT_PAIR = [C_SPEAKER_BASE + 40]


def record_colours(personas) -> None:
    """Note the chosen colours the roster carries.

    Called on every refresh: if somebody changes theirs with `collab color`, the
    change lands on the next redraw without restarting anything.
    """
    for p in personas or ():
        name = str(p.get("name") or "")
        # The roster carries it flat; `meta` is the older shape and is kept
        # because a 1.4 hub still sends that and a viewer should not lose
        # someone's colour over a version difference.
        raw = p.get("color")
        if raw in (None, ""):
            raw = (p.get("meta") or {}).get("color")
        if not name or raw in (None, ""):
            _CHOSEN.pop(name, None)
            continue
        text = str(raw).strip()
        if text.isdigit():
            _CHOSEN[name] = int(text)
        elif hex_to_rgb(text) is not None:
            _CHOSEN[name] = text.lower()
        else:
            # An unreadable colour is ignored and falls back to the deal: any
            # colour beats a viewer that dies on somebody else's metadata.
            _CHOSEN.pop(name, None)


#: Indices we have redefined to serve an exact hex. Taken from the top —255
#: downwards— so as not to tread on the 216 of the cube, which is what
#: everything else in the terminal uses.
_NEXT_SLOT = [255]


def _colour_index(value) -> int:
    """From what the person asked for to the colour index to use.

    A `#00CCCC` is served EXACTLY if the terminal allows redefining colours; if
    not, it falls to the nearest of the 256. Both look fine: the difference
    between exact and approximate is a couple of points of hue, and dying
    because the terminal is limited would be far worse than getting close.
    """
    if isinstance(value, int):
        return value

    # IT GOES THROUGH parse_color, which understands name, index, hex, rgb()
    # A theme value is either a hex colour or a variable; parse_color
    # is what turns the first into something curses can use.
    parsed = parse_color(str(value))
    if isinstance(parsed, int):
        return parsed
    rgb = hex_to_rgb(parsed) if parsed else None
    if rgb is None:
        # -1 IS THE TERMINAL'S DEFAULT COLOUR, and the only honest thing to
        # return here. It used to return C_TEXT, which is a PAIR ID used as a
        # COLOUR INDEX: two different numbering schemes, the same number, and a
        # a colour that came out dark green (colour 22 = #005f00)
        # instead of the white that was meant.
        return -1
    nearest = rgb_to_256(*rgb)
    try:
        can_redefine = curses.can_change_color() and curses.COLORS >= 256
    except (curses.error, ValueError):
        can_redefine = False
    if not can_redefine:
        return nearest
    # Keyed by the NORMALISED hex and not by the raw string: «#00cccc» and
    # «00cccc» are the same colour and took two of the ~24 slots there are.
    if parsed in _HEX_SLOTS:
        return _HEX_SLOTS[parsed]
    slot = _NEXT_SLOT[0]
    if slot <= 231:                       # we have used up the free ones
        return nearest
    _NEXT_SLOT[0] -= 1
    try:
        # curses speaks in thousandths, not in 0-255.
        curses.init_color(slot, *(round(c * 1000 / 255) for c in rgb))
    except (curses.error, ValueError):
        return nearest
    _HEX_SLOTS[parsed] = slot
    return slot


_HEX_SLOTS: dict[str, int] = {}


def _pair_for(value) -> int:
    """The curses pair for a colour that is not in the palette."""
    color = _colour_index(value)
    if color not in _PAIRS_BY_COLOUR:
        pair = _NEXT_PAIR[0]
        _NEXT_PAIR[0] += 1
        try:
            curses.init_pair(pair, color, -1)
        except (curses.error, ValueError):
            # ValueError, not only curses.error: with the terminal not yet
            # initialised, or the index out of range, curses raises ValueError
            # and the narrower catch did not see it. One odd colour took the
            # WHOLE viewer down instead of falling back to white.
            return C_TEXT
        _PAIRS_BY_COLOUR[color] = pair
    return _PAIRS_BY_COLOUR[color]


def _speaker_pair(name: str) -> int:
    """A person's colour: the next free one, not the one their name says.

    It used to be `sum(name.encode()) % len(palette)`, i.e. a hash: two
    different names could land on the same colour with ten others unused. Here
    it is dealt in arrival order over a shuffled permutation, so IT DOES NOT
    REPEAT while any are free. If they run out it starts again — with twelve
    colours you need a thirteenth participant to see it.

    Stable within the process: the same name always gives the same colour while
    the viewer is open. It changes between runs, because the order is shuffled
    at startup.
    """
    chosen = _CHOSEN.get(name)
    if chosen is not None:
        return _pair_for(chosen)
    return _dealt_slot(name)


def _dealt_slot(name: str) -> int:
    """The DEALT colour, ignoring whatever the person chose.

    Kept apart from _speaker_pair because themes need to be able to ask for
    both: `$DEFAULT_COLOR` honours the choice —it is the global setting— and
    `$SPEAKER` is the plain deal, for a theme that wants the frame colour to
    differ from the one the person picked for their text.
    """
    if name not in _SLOTS:
        _SLOTS[name] = C_SPEAKER_BASE + _ORDER[len(_SLOTS) % len(_ORDER)]
    return _SLOTS[name]


#: Theme variables, resolved WHEN PAINTING and not when loading. That is why
#: `$DEFAULT_COLOR` follows whatever colour each person picks instead of
#: freezing the one set the day the theme was written.
_VARS = {
    "$DEFAULT_COLOR": _speaker_pair,
    "$SPEAKER": _dealt_slot,
    "$TEXT": lambda _s: C_TEXT,
    "$GOOD": lambda _s: C_GOOD,
    "$BAD": lambda _s: C_BAD,
    "$WARN": lambda _s: C_WARN,
    "$INFO": lambda _s: C_INFO,
    "$DIM": lambda _s: C_DIM,
}


def _theme_colour(value, speaker: str) -> int:
    """A theme value to a curses pair.

    Two forms: a variable (`$DEFAULT_COLOR`) or a hex colour (`#00cccc`). A
    variable that does not exist falls back to
    the text colour rather than killing the viewer: getting a theme wrong earns
    you a warning from `collab theme --check`, not the loss of your chat.
    """
    if not isinstance(value, str) or not value:
        return C_TEXT
    if value.startswith("$"):
        fn = _VARS.get(value.upper())
        return fn(speaker) if fn else C_TEXT
    return _pair_for(value)


_THEME_CACHE: dict = {}


def _current_theme() -> dict:
    """The resolved theme, re-read when the name OR the files change.

    The files are stat-ed on every redraw —a stat, not a read— so that editing
    a theme shows up when you save. Without that, anyone tuning theirs would
    have to close the viewer to see each change, and testing blind is what makes
    nobody write themes.
    """
    chosen = theme()
    # THE STAMP COMES FROM THE FILES, not from the folder. Saving an existing
    # file does not change the folder's mtime, so a folder-level stamp meant
    # editing your theme did nothing until you created another one — with nano,
    # `cat >` or any editor that writes in place. That is precisely what this
    # function's own docstring promised would work.
    #
    # load_md_themes already stamps per file, so the cheap thing to do is ask it
    # and let its own cache decide whether to touch the disk.
    try:
        stamp = tuple(sorted(
            (p.name, p.stat().st_mtime, p.stat().st_size)
            for p in themes.user_themes_dir().iterdir()
            if p.suffix.lower() in (".md", ".markdown")))
    except OSError:
        stamp = ()
    if _THEME_CACHE.get("key") != (chosen, stamp):
        # The version is what tells the row cache the rendering rules moved.
        # Comparing the theme dicts themselves would work and costs more than
        # the redraw it is trying to avoid.
        _THEME_CACHE.update(key=(chosen, stamp), theme=themes.resolve(chosen),
                            version=_THEME_CACHE.get("version", 0) + 1)
    return _THEME_CACHE["theme"]


def effective_fold() -> int:
    """The folding actually in force: the reader's word over the theme's.

    A theme decides how a conversation LOOKS and folding is part of that, so
    every theme names a number. But the person reading is the one who knows
    whether they want four lines or the whole message, and saying so should not
    mean editing a theme file somebody else wrote and shared.

    Compared against None and not for truth: an override of 0 means «never
    fold», and `if override:` reads that as «nothing set» and hands the
    decision straight back to the theme it was meant to overrule.
    """
    mine = fold_override()
    return int(_current_theme()["fold"]) if mine is None else mine


def _colour_stamp() -> tuple:
    """The chosen colours, as something a cache key can compare.

    By value and not by count: somebody running `collab color` does not change
    how many people have one, and a rebuild keyed on the count would leave
    their new colour off the screen until something else moved.
    """
    return tuple(sorted(_CHOSEN.items()))


def _theme_version() -> int:
    """Bumped whenever the resolved theme changes. Cheap enough per frame."""
    _current_theme()
    return int(_THEME_CACHE.get("version", 0))


def _fmt_pct(value: Any, label: str) -> str:
    try:
        return f"{label} {float(value):.0f}%"
    except (TypeError, ValueError):
        return ""


def quota_text(stats: dict[str, Any]) -> str:
    """Every allowance window this agent has, busiest first.

    Agents do not agree on which windows they have, so whatever they report is
    shown rather than a fixed two.
    """
    from ..stats import quota_summary

    return quota_summary(stats, with_resets=True)


def stat_line(person: dict[str, Any]) -> str:
    """One line of whatever this agent chose to share about itself."""
    stats = person.get("stats") or {}
    bits: list[str] = []
    repo = person.get("repo")
    if repo:
        branch = person.get("branch")
        bits.append(f"{repo}/{branch}" if branch else repo)
    if person.get("machine"):
        bits.append(str(person["machine"]))
    if stats.get("model"):
        bits.append(str(stats["model"]))
    if (quota := quota_text(stats)):
        bits.append(quota)
    if (money := money_text(stats.get("cost_usd"))):
        bits.append(money)
    if stats.get("tokens_in") is not None:
        try:
            bits.append(f"{int(stats['tokens_in']) / 1000:.0f}k in")
        except (TypeError, ValueError):
            pass
    if (ctx := _fmt_pct(stats.get("context_pct"), "ctx")):
        bits.append(ctx)
    # A STAMP ALONE IS NOT FIGURES. The hub no longer stamps a report that
    # sanitised to nothing, but a hub from before that did, and a dict of one
    # key drawn as «31m ago — old» says «this agent's data is stale» where the
    # truth is «this agent never told us anything».
    figures = {k: v for k, v in stats.items() if k != "reported_at"}
    if figures:
        # THE ROSTER IS NARROW, so a fresh figure carries no date here — the
        # full `collab stats` output dates every row. But a figure that is old,
        # or whose age nobody can say, is marked, because drawn plainly it is
        # indistinguishable from a current one and this is the row somebody
        # reads to decide who takes the next task.
        from ..stats import is_stale, reported_age
        if is_stale(stats):
            bits.append(reported_age(stats))
    return " · ".join(bits)


@dataclass
class Pane:
    """A scrollable region. Sticks to the bottom until you scroll away."""

    offset: int = 0
    follow: bool = True
    rows: int = 0
    total: int = 0

    def clamp(self) -> None:
        limit = max(self.total - self.rows, 0)
        self.offset = max(0, min(self.offset, limit))

    def scroll(self, delta: int) -> None:
        self.offset += delta
        self.follow = False
        self.clamp()
        if self.offset >= max(self.total - self.rows, 0):
            self.follow = True

    def to_end(self) -> None:
        self.follow = True
        self.offset = max(self.total - self.rows, 0)

    def top_seq(self, rows: "list[Row]") -> int:
        """Which message is at the top of the view right now.

        Rows are not a stable way to remember a place: the same message is five
        rows in `bubbles` and one in `log`, so an offset means something
        different after a theme change. A seq means the same thing in both.
        """
        for row in rows[self.offset:self.offset + max(self.rows, 1)]:
            if row.seq:
                return row.seq
        return 0

    def hold(self, seq: int, rows: "list[Row]") -> None:
        """Put that message back where the eye expects it.

        Only when not following. At the bottom of a conversation the right
        place is still the bottom — settle() handles that, and pinning a seq
        instead would leave you a screen short of the newest message.
        """
        if self.follow or not seq:
            return
        for i, row in enumerate(rows):
            if row.seq == seq:
                self.offset = i
                self.clamp()
                return

    def to_start(self) -> None:
        self.follow = False
        self.offset = 0

    def settle(self) -> None:
        if self.follow:
            self.offset = max(self.total - self.rows, 0)
        self.clamp()


@dataclass
class Model:
    """Everything on screen, refreshed from the daemon's files."""

    profile: SessionProfile
    events: list[Envelope] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    #: OUR OWN usage figures, for the bottom row. Read beside the snapshot and
    #: not on the draw path: the row is composed four times a second and the
    #: figures move once every few minutes at best, so reading the file per
    #: frame would be a hundred reads to notice one change.
    own_stats: dict[str, Any] = field(default_factory=dict)
    _seen: int = 0
    #: Whether anything older than what is loaded exists; None = not asked yet.
    _older: bool | None = None
    _inbox: Any = None
    #: The daemon's state as judged, not as it last wrote it down. Refreshed
    #: once a second with the snapshot; see state().
    _state: str = "offline"

    @property
    def paths(self) -> DaemonPaths:
        return DaemonPaths(self.profile.dir)

    @property
    def inbox(self) -> Inbox:
        """One connection for the life of the viewer, not one per question."""
        if self._inbox is None:
            self._inbox = Inbox(self.profile.dir)
        return self._inbox

    def title(self) -> str:
        return (self.snapshot.get("title")
                or self.profile.session_id)

    def participants(self) -> list[dict[str, Any]]:
        """The roster, with everyone's chosen colour recorded from it.

        Here and not at startup: somebody changing their colour with `collab
        color` mid-session shows up on the next redraw.
        """
        people = list(self.snapshot.get("participants") or [])

        # WHOSE NAMES ARE NOT MINE — worked out first, because my_names() reads
        # it. my_names() adds the global name so history signed before the hub
        # suffixed me keeps my colour; but the global name differs from the
        # session name precisely BECAUSE somebody else got there first, so
        # seeding my colour under it painted my colour onto them and aligned
        # their messages to my side of the screen.
        #
        # The roster settles it: an entry with a different participant id is a
        # different person, whatever it is called.
        my_id = getattr(self.profile, "participant_id", "") or ""
        _OTHERS.clear()
        _OTHERS.update(p["name"] for p in people
                       if p.get("name") and p.get("id")
                       and p.get("id") != my_id)

        # MY OWN NAMES ARE CLEARED BEFORE THE ROSTER SPEAKS, so whatever
        # survives is what the roster put there THIS TIME. Without it,
        # `published` also counted what a previous frame seeded from my local
        # config, so clearing my colour never reached the screen: the stale
        # entry made it look published, and a published colour is not
        # overwritten.
        for n in my_names(self.profile.name):
            _CHOSEN.pop(n, None)

        record_colours(people)

        # WHAT THE ROSTER MANAGED TO SET, asked of the same place that set it.
        # Working it out separately —«does this person carry a colour?»— while
        # record_colours measured something else —«is that colour legible?»—
        # left a gap where a colour the hub published but this terminal cannot
        # read suppressed the local one too, and you ended up with none.
        published = set(_CHOSEN)

        # THE ROSTER FIRST, MINE ONLY WHERE IT SAYS NOTHING. The other way
        # round does not work: the roster carries `color = ""` for everyone who
        # has not published yet, and record_colours reads that as «has no
        # colour», erasing what was just seeded. Saying nothing is not saying
        # no. So what is published still wins — it is what everyone else sees —
        # but until it has travelled my local config is not lost on the way.
        #
        # `mine` is passed even when None: record_colours reads that as «no
        # longer has a chosen colour» and drops it. Called only when there is
        # one, CLEARING a colour would not show until a restart.
        mine = default_color()
        record_colours([{"name": n, "color": mine}
                        for n in my_names(self.profile.name)
                        if n not in published])
        return people

    def load_initial(self, limit: int = OPEN_WITH) -> None:
        """What the viewer opens on: the last ``limit`` messages.

        A few, deliberately. It used to be the last 500 whatever you asked for
        —`--limit` reached the plain renderer and not this one, so asking for
        more got you less— and every one of them was laid out again on every
        redraw. Opening on what is on screen and reaching back for the rest is
        the difference between a pane that appears and one that arrives.
        """
        self.events = self.inbox.all_events(limit=min(limit or WINDOW, WINDOW),
                                            exclude=NOT_CONVERSATION)
        self._older = None
        self._sync_seen()
        self.refresh_side()

    def _sync_seen(self) -> None:
        """Start following the log from its end, not from where we last were.

        The window is filled from the database; the byte offset is what the
        tail of the JSONL is read from. Left behind, everything between would
        arrive a second time.
        """
        path = self.paths.root / "inbox.jsonl"
        try:
            self._seen = path.stat().st_size
        except OSError:
            self._seen = 0

    # -- the window ----------------------------------------------------------
    #
    # The viewer holds a WINDOW over the conversation, not the conversation. The
    # log on disk is complete and stays complete; what is in memory —and so what
    # is laid out, wrapped and framed— is WINDOW messages, whichever WINDOW you
    # are looking at. Scrolling off either end slides it, and the pane says
    # which way there is more.

    def _trim(self, *, keep: str) -> None:
        """Hold the window to size, dropping from the end you are leaving."""
        over = len(self.events) - WINDOW
        if over <= 0:
            return
        if keep == "start":
            del self.events[-over:]
        else:
            del self.events[:over]
            self._older = True

    def load_older(self, count: int = PAGE) -> int:
        """Slide the window back.

        Opening on the whole of a long session costs a wait nobody asked for,
        and opening on the last few used to be the end of the matter: above the
        first message there was nothing, and no way to tell a conversation that
        starts there from one that was merely cut.
        """
        if not self.events or not self.more_above():
            return 0
        first = int(getattr(self.events[0], "seq", 0) or 0)
        older = self.inbox.before(first, limit=count, exclude=NOT_CONVERSATION)
        self._older = None
        if not older:
            return 0
        self.events[:0] = older
        self._trim(keep="start")
        return len(older)

    def load_newer(self, count: int = PAGE) -> int:
        """Slide the window forward, towards what is being said now."""
        if not self.events or not self.pending():
            return 0
        last = int(getattr(self.events[-1], "seq", 0) or 0)
        newer = self.inbox.after(last, limit=count, exclude=NOT_CONVERSATION)
        if not newer:
            return 0
        self.events.extend(newer)
        self._trim(keep="end")
        return len(newer)

    def load_tail(self) -> None:
        """Back to the live end, however far away it is."""
        self.events = self.inbox.all_events(limit=WINDOW, exclude=NOT_CONVERSATION)
        self._older = None
        self._sync_seen()

    def load_start(self) -> None:
        """To the beginning, in one read rather than a page at a time."""
        self.events = self.inbox.first(limit=WINDOW, exclude=NOT_CONVERSATION)
        self._older = False

    def pending(self) -> int:
        """Messages that have arrived below the window and are not in it.

        Read from the log rather than counted as they land: while you are
        scrolled back the window is held still —yanking it about under a reader
        is not «live», it is unusable— so the count has to come from the place
        that does keep everything.
        """
        if not self.events:
            return 0
        last = int(getattr(self.events[-1], "seq", 0) or 0)
        return self.inbox.count_after(last, exclude=NOT_CONVERSATION) if last else 0

    def more_above(self) -> bool:
        """Whether anything is left further back.

        Remembered between frames: it is read on every redraw to label the
        pane, and the answer only moves when the loaded history does.
        """
        if self._older is None:
            first = int(getattr(self.events[0], "seq", 0) or 0) if self.events else 0
            self._older = bool(first) and self.inbox.has_before(
                first, exclude=NOT_CONVERSATION)
        return self._older

    def roster_is_current(self) -> bool:
        """Can we still check who is here?

        Only while our own feed is live. `snapshot.json` is a cache of what the
        hub last told us, refreshed only by a fetch that worked — so the moment
        we stop being connected it stops being an observation and becomes a
        memory, however recent.
        """
        return self.state() == "live"

    def snapshot_age(self) -> str:
        """How old the roster is, in words. Empty when it does not say."""
        try:
            fetched = float(self.snapshot.get("fetched_at") or 0)
        except (TypeError, ValueError):
            return ""
        if not fetched:
            # Written by a collab that did not stamp it. The file's own mtime
            # is the next best thing and is never wrong by much: it is rewritten
            # on every successful fetch.
            try:
                fetched = self.paths.snapshot.stat().st_mtime
            except OSError:
                return ""
        return ago(fetched)

    def state(self) -> str:
        """What the daemon is doing, judged rather than quoted.

        The badge used to print `status.json`'s own word for itself, so a pane
        left open after its listener died sat there saying `live` in green,
        beside a roster and an unread count that had stopped moving hours
        before. Worked out once a second in refresh_side rather than per frame:
        it costs a pid read and a `kill(pid, 0)`, which is nothing, but nothing
        four times a second is still four times more often than the answer can
        change usefully.
        """
        return self._state

    def refresh_side(self) -> None:
        try:
            self.snapshot = json.loads(self.paths.snapshot.read_text())
        except (OSError, ValueError):
            pass
        self.status = read_status(self.profile) or self.status
        self._state = effective_state(
            self.status, running=is_running(self.profile) is not None)
        # Not `or self.own_stats`, unlike the status above: an empty answer
        # here means the file was never written or belongs to another agent,
        # and holding the last figures would leave a quota on the row that
        # nothing is refreshing any more.
        self.own_stats = read_stats(self.profile)

    def poll_events(self, follow: bool = True) -> int:
        """Read whatever has been appended since we last looked.

        ONLY WHILE THE READER IS AT THE BOTTOM. Scrolled back, the window is
        held exactly where it was put: appending would slide it —fifty messages
        is fifty messages— and take the paragraph being read off the screen. The
        arrivals are not lost, they are on disk and counted, and the pane says
        how many are waiting below.
        """
        if not follow:
            return 0
        if self.pending():
            # Following again after reading back through the history: the
            # window is somewhere in the middle, and the tail of the log is not
            # what comes after it. Reload the end rather than splicing the
            # newest messages onto a page from an hour ago.
            self.load_tail()
            return 0
        path = self.paths.root / "inbox.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if size < self._seen:
            # The log was replaced under us — a session reset, or a state
            # directory rebuilt. Reading from the old offset would splice the
            # middle of one conversation onto the start of another.
            self.load_tail()
            return 0
        if size == self._seen:
            return 0
        added = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(self._seen)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        env = Envelope.from_dict(json.loads(line))
                    except ValueError:
                        continue
                    # The tail is read from the log FILE rather than through
                    # the queries above, so it needs the same rule applied by
                    # hand: without it a `collab working` landing while you
                    # watch put a raw state dict on screen, even though every
                    # other way into this list filters them out.
                    if env.kind in NOT_CONVERSATION:
                        continue
                    self.events.append(env)
                    added += 1
                self._seen = fh.tell()
        except OSError:
            return 0
        if added:
            self._trim(keep="end")
        return added


#: Marks that say how something went. Red BEATS green when both match:
#: «not ok» and «WRONG, no failure but EXIT=0» carry both, and erring towards
#: verde es el error caro. «CORRECTO» estuvo en la lista de verdes y se quito:
#: turns up in ordinary prose constantly, and a colour that shows up in prose
#: significar nada.
_BAD = re.compile(r"✗|\bMAL\b|\bFALL[AOE]|\bERROR|\bROTO?\b|\bFALS[AO]\b|"
                  r"\bFAILED?\b|\bBROKEN\b|EXIT=[1-9]|\bNO EXISTE\b", re.I)
_GOOD = re.compile(r"✓|\bok\b|\bCONFIRMADO\b|\bAGUANTA\b|\bPASA\b|\bPASSED?\b|"
                   r"EXIT=0|\bIDENTIC[AO]\b", re.I)
_WARN = re.compile(r"⚠|\bAVISO\b|\bOJO\b|\bPENDIENTE\b|\bWARN\w*\b|"
                   r"\bNO MEDIDO\b|\bINCOMPLETA\b|\bTODO\b", re.I)
# Tested with match(), so it is anchored to the start of the line. The branch
# for commands had no leading \s*, so «  git rev-list ...» — with the indent a
# command is ALWAYS written with — did not match. The pattern looked right and
# failed on the one shape it would actually meet.
_DATA = re.compile(r"^\s*([`|+#-]|\w+\s*=|(git|python3?|node|bash|npm|collab|tmux)\b)", re.I)


#: WHAT COUNTS AS «the left button went down here».
#:
#: Not RELEASED, which would fire a second time for the same click. And the
#: double and triple variants ARE included, because ncurses does not report two
#: quick clicks as two clicks: it coalesces them into ONE event under a
#: different name, and a mask that lists only PRESSED and CLICKED silently
#: ignores it. Measured on a real terminal — a click, then a second one a
#: moment later, arrived as `0x8` and nothing happened. Which is the failure
#: this whole file is about: a button that works until somebody clicks it
#: twice, and then looks broken to exactly the person who is trying hardest.
_CLICK = (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
          | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
          | getattr(curses, "BUTTON1_TRIPLE_CLICKED", 0))

#: The strokes of the scrollbar, drawn DOWN a column: a rail, the part of it
#: you are looking at, and an end that says the conversation does not stop
#: where the rail does.
#:
#: `scroll_track` is written for either axis because it once served two — there
#: was a horizontal one along the bottom row as well. That row belongs to the
#: status bar, which says more with it, so the position is told down the side
#: and nowhere else. The `glyphs` argument stays: the arithmetic has no opinion
#: about which way it is drawn, and a caller that wants the other axis needs no
#: second copy of it.
V_RAIL, V_THUMB, V_UNLOADED = "┆", "█", "┊"

#: WHY TMUX'S OWN SCROLLBAR IS NOT USED FOR THIS.
#:
#: tmux grew `pane-scrollbars` in 3.6, and it is the wrong tool here three
#: times over. It measures TMUX'S scrollback; the viewer is a curses app, so it
#: runs on the alternate screen, where tmux adds nothing to history — the thumb
#: is computed from `screen_size_y + screen_hsize`, and with no history that is
#: a bar permanently full, saying «nothing to scroll» over a conversation you
#: are halfway up. Upstream treats showing it there as a bug and is removing
#: it. And even working perfectly it could not answer the question: this
#: conversation is a window of messages held in memory over a log on disk, and
#: tmux has never seen either. Only this process knows where the reader is.

def scroll_track(width: int, offset: int, rows: int, total: int, *,
                 more_above: bool = False, more_below: bool = False,
                 glyphs: tuple[str, str, str] = (V_RAIL, V_THUMB,
                                                 V_UNLOADED)) -> str:
    """The scrollbar, as `width` columns of text.

    It measures WHAT IS LOADED, which is not the whole conversation: the viewer
    holds a window over the log and pages it in as you travel. Claiming a
    percentage of the whole would mean counting messages that are not in rows
    yet — mixing units, and a thumb that jumps every time a page arrives. So
    the ends say it instead: an `┄` where there is history the window has not
    reached, and the reader can see that 0 % means «the top of what is here».
    """
    rail, thumb_glyph, unloaded = glyphs
    width = max(width, 1)
    # AN OFFSET OUTSIDE THE CONVERSATION IS BROUGHT BACK INTO IT HERE. Every
    # caller runs `Pane.clamp` first, so this cannot be reached today — but
    # «cannot be reached» is a fact about the callers, checked by reading them,
    # and it stops being true the first time somebody adds a caller. Without
    # it a negative offset walks `start` off the front of the list and raises
    # IndexError, and a large one reports a percentage above 100.
    offset = min(max(offset, 0), max(total - rows, 0))
    if total <= 0 or rows <= 0 or total <= rows:
        # Everything LOADED fits on the screen, so the thumb is the whole rail.
        # Which is not the same as «this is the whole conversation» — the marks
        # below still apply, and skipping them here painted a full rail over a
        # window with five hundred messages behind it.
        track = [thumb_glyph] * width
    else:
        thumb = min(max(round(width * rows / total), 1), width)
        travel = width - thumb
        at = offset / max(total - rows, 1)
        start = min(round(travel * at), travel)
        track = [rail] * width
        for i in range(start, start + thumb):
            track[i] = thumb_glyph
    # The marks last, so an end that carries one is never painted over by the
    # thumb sitting on top of it — at the top of a window with more behind it,
    # which is the exact case they exist for.
    if more_above:
        track[0] = unloaded
    if more_below:
        track[-1] = unloaded
    return "".join(track)


@dataclass
class Gutter:
    """A vertical scrollbar down the right edge of a pane.

    Kept as a record rather than drawn and forgotten for the same reason the
    bar is: a click has to be resolved against the column and rows that were
    actually painted, and a handler that works them out for itself a moment
    later is a handler that eventually disagrees with the screen.
    """

    x: int
    top: int
    rows: int
    pane: "Pane"

    def holds(self, x: int, y: int) -> bool:
        return x == self.x and self.top <= y < self.top + self.rows

    def fraction(self, y: int) -> float:
        return (y - self.top) / max(self.rows - 1, 1)


def line_pair(line: str) -> int:
    """Which colour this line asks for. 0 = the speaker's, untouched."""
    if not line.strip():
        return 0
    if _BAD.search(line):
        return C_BAD
    if _GOOD.search(line):
        return C_GOOD
    if _WARN.search(line):
        return C_WARNLINE
    if _DATA.match(line):
        return C_INFO
    return 0


#: What text takes on screen, which is not the same as `len()`. The measure
#: and the cut live in `collab.columns` now, because the host agent's status
#: line needs the same two and had grown its own count of CHARACTERS instead —
#: see that module for what a kanji did to it. The names stay: every row in
#: this file and a good many tests measure through them.
_w = _columns_width
_clip = _columns_clip


def _pad(text: str, width: int) -> str:
    """Pad to `width` COLUMNS. `f"{x:<n}"` counts characters, which left the
    right-hand border out of line on anything written in kanji."""
    return text + " " * max(0, width - _w(text))


def _wrap(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    out: list[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        line = ""
        for word in paragraph.split():
            # A WORD WIDER THAN THE BOX IS BROKEN. It used to be emitted
            # whole and ran off the right edge taking the border with it: with
            # a URL or a hash — an everyday thing between agents — the bubble
            # was left open. Better to cut a URL than to break the frame.
            while _w(word) > width:
                if line:
                    out.append(line)
                    line = ""
                piece = ""
                for c in word:
                    if _w(piece + c) > width:
                        break
                    piece += c
                out.append(piece)
                word = word[len(piece):]
            if _w(line) + _w(word) + 1 > width:
                if line:
                    out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return out or [""]


@dataclass
class Row:
    """A rendered line, with the colour pair and attributes it needs."""

    text: str
    pair: int = 0
    attr: int = 0
    #: Which event produced it, so a click knows which one to fold. And whether
    #: this row IS the button: without it a click would have to guess from text.
    seq: int = 0
    button: bool = False
    #: The colour of the FRAME, which is not the colour of the content. On a
    #: body line `pair` says how it went —red on failure— and the `│` live
    #: inside that same string. Without keeping the sender's colour separately
    #: the border cannot have it: the whole row is painted in the tone and the
    #: sides get tinted with it.
    edge: int = 0
    #: How many leading characters go in `edge` instead of `pair`. A Row carries
    #: one colour, and the roster needs two on the same line: the dot and the
    #: name in the person's colour, the state and the focus in the neutral one.
    #: Splitting the row in two would have split a line that reads as one thing.
    head: int = 0


#: The bubble's strokes. Drawn with box characters because the frame is what
#: turns a log line into a message: without it the eye cannot tell where one
#: ends and the next begins when both run to five lines.
_TL, _TR, _BL, _BR, _H, _V = "╭", "╮", "╰", "╯", "─", "│"

#: Events that are NOT conversation. In a messaging app «X joined the group» is
#: not a bubble: it is a centred, quiet line, because it is not something
#: somebody said to you.
_SYSTEM = (KIND_HELLO, KIND_PRESENCE)


def _body_lines(env: Envelope, width: int) -> list[str]:
    """An event's text, already wrapped to the inside width of the bubble."""
    if env.kind == KIND_CHAT:
        where = f"→{env.to}" if env.to else ""
        head = f"{where}  " if where else ""
        return _wrap(f"{head}{env.text}", width)
    if env.kind == KIND_HELLO:
        b = env.body
        loc = ", ".join(x for x in (b.get("repo"), b.get("branch")) if x)
        detail = "joined" + (f" from {loc}" if loc else "")
        if b.get("focus"):
            detail += f" — {b['focus']}"
        return _wrap(detail, width)
    if env.kind == KIND_PRESENCE:
        return _wrap(str(env.body.get("event", "")), width)
    if env.kind == KIND_TASK:
        return _wrap(task_line(env.body), width)
    if env.kind == KIND_FILE:
        b = env.body
        if b.get("action") == "received":
            # The protocol's words for what the ack did, the same ones `watch`
            # prints: a room file is not gone when one person has it.
            return _wrap(f"collected {b.get('name')} ({file_outcome(b)})", width)
        size = int(b.get("size") or 0)
        return _wrap(f"shared {b.get('name')} ({size / 1024:.0f} KB) · "
                     f"collab file get {b.get('id')}", width)
    return _wrap(env.text or str(env.body), width)


#: SPELLED OUT RATHER THAN ASKED OF strftime. `%b` is locale-dependent, so the
#: date beside a message came out in whatever language the machine happened to
#: be set to — and a transcript two people read together cannot have half its
#: dates in one language. This is also the only place the month is named, so
#: `_day_label` and `_stamp` cannot disagree about how to spell it.
# ONE TABLE. The stats row spells months from `protocol.MONTHS`; a second copy
# here drifted from it once already in wording, and two tables that can differ
# are two calendars on one screen.
from ..protocol import MONTHS


def _local_date(ts: str) -> _dt.date | None:
    """The calendar day the stamp falls on FOR THE PERSON READING IT.

    The wire is UTC; the reader is not. Taking the date off the raw string
    while taking the clock off the converted datetime is how a message sent at
    21:30 last night came out headed «today» — the two halves came from two
    different days. Both now come off `local_datetime`.
    """
    parsed = local_datetime(ts)
    return parsed.date() if parsed else None


def _day(ts: str) -> str:
    """The grouping key for the day separators, in the reader's timezone."""
    d = _local_date(ts)
    if d is None:
        return ts[:10] if len(ts) >= 10 else ""
    return d.isoformat()


def _day_label(ts: str) -> str:
    """«today», «yesterday» or the date. A messaging app does not make you
    work it out."""
    d = _local_date(ts)
    if d is None:
        return _day(ts)
    delta = (local_today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _stamp(ts: str) -> str:
    """«15:22» when it is today, «30 aug 15:22» when it is not.

    The date only appears when it is needed. Always showing it means six extra
    characters on every message to tell you something you already knew.
    """
    when = local_datetime(ts)
    if when is None:
        return local_clock(ts)
    clock = when.strftime("%H:%M")
    if when.date() == local_today():
        return clock
    return f"{when.day} {MONTHS[when.month - 1]} {clock}"


def _fold_lines_to(lines: list[str], fold: int, open_now: bool) -> tuple[list[str], int]:
    """Which lines show and how many are left. ONCE, for both themes.

    It was written twice —in event_rows and in classic_rows— differing by one
    guard: with fold = 0, classic did not fold (correct) and chat computed
    hidden = len(lines), i.e. replaced THE ENTIRE MESSAGE with a button. A
    one-line message came out as «▸ show more (1 line)» and nothing else. It is
    the same shape that already bit us with two independent _wrap functions: two
    copies of the same sum drift apart, and the one that breaks is the one
    nobody tests.
    """
    if fold <= 0:
        return lines, 0
    hidden = max(0, len(lines) - fold)
    return (lines if open_now or not hidden else lines[:fold]), hidden


def event_rows(env: Envelope, width: int, me: str,
               expanded: set[int] | None = None,
               fold: int = FOLD_LINES,
               with_name: bool = True,
               theme: dict | None = None) -> list[Row]:
    """One event as a bubble, sided by who sent it.

    MINE ON THE RIGHT AND EVERYONE ELSE'S ON THE LEFT, with a gap on the
    opposite side: that gap is what makes whose message it is readable without
    reading the name. The text inside stays left-aligned — a paragraph justified
    right is unreadable past two lines.
    """
    expanded = expanded if expanded is not None else set()
    T = theme or _current_theme()
    seq = int(getattr(env, "seq", 0) or 0)
    speaker = env.sender or "?"
    estrecho = int(T["narrow_at"])
    # When narrow nobody goes right: there is no room for the gap that makes
    # the distinction readable, and without it all you get is text out of place.
    mine = (_is_mine(speaker, me) and width >= estrecho
            and T["own_side"] == "right")
    clock = local_clock(env.ts)
    pair = _theme_colour(T["header"], speaker)
    border = _theme_colour(T["frame"], speaker)
    body_pair = _theme_colour(T["text"], speaker)
    tl, tr, bl, br, h, v = (list(str(T["chars"])) + list("╭╮╰╯─│"))[:6]

    # --- system events: one centred line, no bubble -------------------------
    if env.kind in _SYSTEM:
        text = f"— {speaker} {' '.join(_body_lines(env, max(width - 20, 20)))} —"
        # With «…», not cut dead: at 60 columns it ate the end of the focus of
        # the session without a word, and that is where people say what they
        # are working on.
        text = _clip(text, width)
        pad = " " * max(0, (width - len(text)) // 2)
        return [Row(pad + text, C_DIM, curses.A_DIM, seq, False)]

    # --- bubble ------------------------------------------------------------
    # The floor of 28 must NOT override the pane: between 24 and 37 columns
    # —widths `draw()` does render— the bubble came out at 28 and overflowed. A
    # minimum that ignores the maximum stops being a minimum and becomes a bug.
    # NARROW MODE: below NARROW_AT the pane has no room for two-sided
    # colours on two sides. The right-hand indent eats the width exactly
    # when there is least to spare, and a message broken into twelve-character
    # scraps does not read. So it takes ALL the width and everything goes left:
    # who is speaking is still known from the name and the frame colour.
    if width < estrecho:
        bubble = width
    else:
        # SIX IS THE REAL FLOOR, whatever the theme asks for: two strokes,
        # two spaces and one column of text. `bubble_share: 0.05` with
        # `bubble_min: 4` —both at the exact minimum of their documented
        # range— left inner = 0, and a _wrap of zero width returns the text
        # unwrapped: a body two columns wide inside a box of four, on every
        # pane between 56 and 99 columns. Neither setting breaks alone.
        floor = min(max(int(T["bubble_min"]), 6), width)
        cap = max(BUBBLE_MAX_MIN,
                   int(_screen_width(width) * float(T["bubble_max_share"])))
        bubble = min(width, max(floor, min(cap,
                                            int(width * float(T["bubble_share"])))))
    inner = max(1, bubble - 4)              # two borders and a space each side
    lines = _body_lines(env, inner)

    open_now = seq in expanded
    shown, hidden = _fold_lines_to(lines, fold, open_now)
    if hidden:
        plural = "line" if hidden == 1 else "lines"
        shown = shown + [("▾ show less" if open_now
                          else f"▸ show more ({hidden} {plural})")]

    # THE HEADER HAS TO FIT, and the header is the stamp plus the separator
    # plus the name. This asked for `len(clock) + 6` — the SHORT clock, five
    # characters — while what gets printed is _stamp(), which is twelve for a
    # message that is not from today. On an eighty-column terminal the box came
    # out thirteen wide and «28 ago 13:38 · alice» was clipped to «28 ago 13:38…»:
    # who spoke was unreadable, with seventy-nine columns free.
    cabecera_min = _w(_stamp(env.ts)) + _w(HEADER_SEPARATOR) + _w(speaker) + 6
    box_width = min(bubble, max([_w(x) for x in shown] + [cabecera_min]) + 4)
    left = " " * max(0, width - box_width) if mine else ""
    rows: list[Row] = []

    # THE HEADER: STAMP FIRST, NAME AFTER, on one line.
    #
    # The time used to live on the bottom edge of the bubble. It looks good
    # there and makes you drop your eyes to the end of a forty-line message to
    # find out when it was said.
    #
    # And on a GROUPED message — from whoever just spoke — only the stamp
    # shows: the name is already above, the time is not, and without it a group
    # of five messages read as one stretched out over an hour.
    #
    # Clipped against the BUBBLE and against the PANE: an 80-character name
    # came out whole over a box of 41 and left the block stepped.
    stamp = _stamp(env.ts)
    cabecera = f"{stamp} {HEADER_SEPARATOR} {speaker}" if with_name else stamp
    cabecera = _clip(cabecera, max(1, min(box_width - 2, width - 2)))
    line = (_pad("", box_width - _w(cabecera)) + cabecera if mine
             else "  " + cabecera)
    rows.append(Row(left + line, pair, curses.A_BOLD, seq, False))

    # THE FRAME IN WHITE, not in the speaker's colour: the border is structure,
    # and tinting it competed with the colours that do mean something — the
    # red of an error, the green of a ✓. Who is speaking is already clear from
    # the side and from the name.
    rows.append(Row(left + tl + h * (box_width - 2) + tr, border, 0, seq,
                    False, border))
    for i, line in enumerate(shown):
        is_button = hidden and i == len(shown) - 1
        # CLIP HERE AND NOT EARLIER. The button line does not go through
        # _wrap — it is added afterwards — so in a narrow bubble it ran off the
        # right: «▸ show more (1 line)» measures 19 and the box measured 10. And
        # even when _wrap is right, this cut is the last word: NO line may be
        # wider than the inside of its own box.
        body = f"{v} {_pad(_clip(line, box_width - 4), box_width - 4)} {v}"
        # WHITE by default, and colour only when the line says something. The
        # fallback used to be the speaker's colour, so the WHOLE body came out
        # tinted and the green of a ✓ stood out against nothing.
        if is_button:
            tone = C_BUTTON
        elif T["tones"]:
            tone = line_pair(line) or body_pair
        else:
            tone = body_pair
        rows.append(Row(left + body, tone,
                        curses.A_BOLD if is_button else 0, seq, bool(is_button),
                        border))
    # The floor, now without the time: it moved up to the header row.
    # No A_DIM: the roof was painted at full intensity and the floor dimmed, so
    # the same box had two tones of border and looked half drawn. A frame only
    # reads as a frame when its four sides are the same stroke.
    rows.append(Row(left + bl + h * (box_width - 2) + br, border,
                    0, seq, False, border))
    rows.append(Row("", 0, 0, seq, False))
    return rows


def classic_rows(env: Envelope, width: int, me: str,
                 expanded: set[int] | None = None,
                 fold: int = 0,
                 theme: dict | None = None) -> list[Row]:
    """The original look: time, name and running text, no bubbles.

    It is `main`'s, with two things the new theme brought that there was no
    sense keeping from it:

    · YOUR OWN COLOUR GOES ON THE TEXT. There is no frame here to put it on, and
      a global setting that only works in one theme is not global. Whoever picks
      a colour picks it to be recognised, not to be recognised in one view.
    · folding, at the theme's number. The shipped `classic` did not fold, on
      the theory that people who choose the log view want it all in front of
      them; a forty-line file dump between the reader and the three lines
      after it is what that theory looked like, and it folds now like every
      other theme — see themes.FOLD. `collab fold off` is still there for
      whoever really does want it all.
    """
    expanded = expanded if expanded is not None else set()
    T = theme or _current_theme()
    seq = int(getattr(env, "seq", 0) or 0)
    clock = _stamp(env.ts)
    mark = KIND_MARK.get(env.kind, " ")
    speaker = env.sender or "?"
    label = f"{speaker}{' (you)' if _is_mine(speaker, me) else ''}"
    head = f"{clock} {HEADER_SEPARATOR} {label:>14.14} {mark} "
    # EVERYTHING HERE IN COLUMNS.
    #
    # `{label:>14.14}` pads to fourteen CHARACTERS, which is up to twenty-eight
    # columns for a name in Japanese. The continuation indent was built from
    # len(head), so it landed three to eight columns short of where the first
    # line's text starts, and body_width was computed too generous by the same
    # amount — so line one was built wider than the pane and the terminal threw
    # away the overflow.
    head_width = _w(head)
    indent = " " * head_width

    # AND THE HEADER CAN BE WIDER THAN THE PANE. It is twenty-five columns for
    # any ASCII name, and draw() renders panes from twenty-four: between 24 and
    # 26 the body had nowhere to go and the message was INVISIBLE — header on
    # screen, text nowhere, no warning. The floor of 20 below could never be
    # reached because it was a floor on a number that was already negative.
    #
    # Under that width the header is dropped to its first line and the body
    # runs full width underneath: unusual-looking, but readable, which is the
    # only thing that matters at twenty-four columns.
    cramped = head_width + 8 > width
    if cramped:
        indent = ""
    body_width = max(width - (0 if cramped else head_width) - 1, 8)

    lines = _body_lines(env, body_width)
    open_now = seq in expanded
    shown, hidden = _fold_lines_to(lines, fold, open_now)

    pair = _theme_colour(T["header"], speaker)
    text = _theme_colour(T["text"], speaker)
    dim = curses.A_DIM if env.kind in (KIND_PRESENCE, KIND_HELLO) else 0

    def _tone(line: str) -> int:
        return (line_pair(line) or text) if T["tones"] else text

    first_line = shown[0] if shown else ""
    if cramped:
        # The header on its own line, then the text with nothing in front of it.
        rows = [Row(_clip(head, width), pair, dim, seq, False, pair)]
        if first_line:
            rows.append(Row(_clip(first_line, width), _tone(first_line), dim, seq,
                            False, pair))
    else:
        rows = [Row(head + first_line, _tone(first_line), dim, seq, False, pair)]
    for extra in shown[1:]:
        rows.append(Row(_clip(indent + extra, width), _tone(extra), dim, seq,
                        False, pair))
    if hidden:
        plural = "line" if hidden == 1 else "lines"
        label = ("▾ show less" if open_now
                    else f"▸ show more ({hidden} {plural})")
        # THE INDENT GIVES WAY, NOT THE LABEL. This row is a control, and the
        # indent under it is decoration that lines it up with the text above;
        # `_clip` alone would spend the narrow pane on the alignment and cut
        # the button down to «▸ show m…», which is neither readable nor
        # obviously a button.
        #
        # It goes wrong only where the header column is wide and the pane is
        # not: `classic` at 40 columns put a 31-column indent in front of a
        # 21-column label and returned a row of 52. Nobody met it because the
        # shipped `classic` then had `fold: 0` and never drew this row at all —
        # until `collab fold` made every theme able to, and then `classic`
        # itself started folding.
        room = max(width - _w(label), 0)
        rows.append(Row(_clip(indent[:room] + label, width), C_BUTTON,
                        curses.A_BOLD, seq, True, pair))
    return rows


#: The global name, and when it was last worked out. IT IS NOT FREE: resolving
#: it runs `git rev-parse` and `git config user.name`, reads the identity file
#: and walks the state directories. my_names() is asked once PER MESSAGE, and
#: the conversation is rebuilt on every redraw — measured at 300 messages that
#: was 900 forks a frame and 2 s of it, which is exactly what made the pane feel
#: like treacle. The answer changes when somebody runs `collab name`, so it is
#: re-read on a timer rather than kept for the life of the process.
_OWN_NAME: dict[str, Any] = {}
OWN_NAME_TTL = 2.0


def _own_name() -> str | None:
    now = time.monotonic()
    if _OWN_NAME and now - _OWN_NAME["at"] < OWN_NAME_TTL:
        return _OWN_NAME["name"]
    try:
        own = resolve_name()
    except Exception:                                    # noqa: BLE001
        own = None
    _OWN_NAME.update(name=own, at=now)
    return own


def my_names(session: str) -> list[str]:
    """Every name I can appear signing under.

    THIS session's is not enough. If the hub suffixes you over a name clash —or
    you join under another because yours is taken— your older messages are still
    signed with the usual one. The global name is the one that does not change
    because the hub has a problem, so both count.

    It exists as a list and not as a comparison because TWO things depend on it:
    which side a message aligns to, and what colour it comes out. They were
    written separately, one was fixed and the other stayed broken — your global
    colour did not show on your own messages and it looked as though the setting
    did nothing.
    """
    out = [session] if session else []
    own = _own_name()
    # The session name is always mine — the hub handed it to me. The global one
    # is mine only while nobody else is using it: it differs from the session
    # name exactly when the hub had to suffix me, and it had to suffix me
    # because somebody else was already there.
    if own and own not in out and own not in _OTHERS:
        out.append(own)
    return out


def _is_mine(name: str, me: str) -> bool:
    """Is this message mine?"""
    return bool(name) and name in my_names(me)


def conversation_rows(events, width: int, me: str,

                      expanded: set[int] | None = None,
                      fold: int = FOLD_LINES) -> list[Row]:
    """The whole conversation, grouped the way a messaging app would group it.

    TWO THINGS THAT MAKE THE DIFFERENCE and cannot be decided from one message
    alone, which is why this exists apart from `event_rows`:

    · the name repeats ONLY when the speaker changes. Repeating it on every
      message is what makes a conversation look like a log.
    · a separator when the day changes, with «today» and «yesterday» spelled
      out, so nobody has to subtract dates.
    """
    # THE THEME IS READ HERE, on every refresh, not at start-up: a
    # `collab theme classic` lands on the next redraw without restarting the
    # viewer. Read once, changing theme would mean closing it, and whoever
    # tried would think the command does nothing.
    T = _current_theme()
    fold = effective_fold()
    # BY THE LAYOUT IT DECLARES, not by what it is called. With `if theme ==
    # "classic"` any user theme extending classic came out in bubbles: it
    # inherited everything except the one thing that was actually checked.
    if T["layout"] == "log":
        rows: list[Row] = []
        for env in events:
            rows.extend(classic_rows(env, width, me, expanded, fold, T))
        return rows

    rows: list[Row] = []
    last_speaker = None
    last_day = None
    for env in events:
        day = _day(env.ts)
        if T["day_separators"] and day and day != last_day:
            label = f" {_day_label(env.ts)} "
            side = max(0, (width - len(label)) // 2)
            rows.append(Row(_H * side + label + _H * side, C_DIM,
                            curses.A_DIM, 0, False))
            last_day, last_speaker = day, None
        if env.kind in _SYSTEM:
            rows.extend(event_rows(env, width, me, expanded, fold, theme=T))
            last_speaker = None
            continue
        show_name = (env.sender != last_speaker) if T["group_by_author"] else True
        rows.extend(event_rows(env, width, me, expanded, fold,
                               with_name=show_name, theme=T))
        last_speaker = env.sender
    return rows


def ago(seen: Any) -> str:
    """How long since we last heard from someone, in words."""
    try:
        gap = time.time() - float(seen)
    except (TypeError, ValueError):
        return ""
    if gap < 90:
        return "just now"
    minutes = int(gap // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 60 * 24:
        return f"{minutes // 60}h ago"
    return f"{minutes // (60 * 24)}d ago"


def roster_rows(model: Model, width: int) -> list[Row]:
    """One participant per two lines: who and how, then what they are using.

    WHO IS ONLINE IS SOMETHING WE LEARN FROM THE HUB, so when we cannot reach
    the hub we do not know it. `snapshot.json` is only rewritten by a fetch that
    SUCCEEDS — deliberately, so a two-second blip does not empty the pane — and
    the consequence was that a session which died left the last good roster on
    screen for ever, everyone still marked online, while the badge above it said
    reconnecting. The pane was not failing to update; it was showing figures it
    could no longer check, which is worse, because it looks like an answer.
    """
    rows: list[Row] = []
    me = model.profile.name
    known = model.roster_is_current()
    stale_for = model.snapshot_age()
    for person in model.participants():
        online = person.get("connected") and known
        name = person.get("name", "?")
        doing = person.get("activity") or {}
        # FILLED MEANS AT WORK, HOLLOW MEANS FREE — in the person's own colour
        # either way, because the dot's job in this pane is to say who, and its
        # shape is what says what. A second colour on it would compete with
        # that, and the state word beside it already carries online/offline.
        #
        # It is deliberately not animated: the roster is rebuilt only when
        # something in it changes, and a spinner would mean rebuilding every
        # frame — which is what a redraw used to cost before it was fixed.
        glyph = "●" if (online and activity.is_working(doing)) else "○"

        tags = []
        if person.get("is_host"):
            tags.append("host")
        if name == me:
            tags.append("you")
        elif peers.same_machine(person):
            tags.append("same machine")
        suffix = f" ({', '.join(tags)})" if tags else ""

        # Say the state rather than only colouring a dot, and for someone who
        # has gone, say when — leaving a minute ago and leaving yesterday mean
        # very different things.
        state = "online" if online else "offline"
        if not known:
            # Not «offline», which would be a claim of its own. We are the ones
            # who are disconnected; what they are doing is not ours to report.
            state = f"unknown · as of {stale_for}" if stale_for else "unknown"
        elif not online and (seen := ago(person.get("last_seen"))):
            state = f"offline · last seen {seen}"

        # THE PERSON'S COLOUR ON THE DOT AND THE NAME, the neutral one on the
        # state. The colour is what tells people apart at a glance, and the list
        # of people is the one place that is entirely about telling them apart —
        # it identified them everywhere except there.
        #
        # The state keeps its own green/grey: whether someone is here is not a
        # matter of who they are, and a dot that carried both meanings would
        # tell you neither.
        who = f" {glyph} {name}{suffix}"
        head = who
        # COLUMNS, not characters. A name in Japanese costs two columns per
        # glyph, so `28 - len(head)` left the state word starting at column 28,
        # 31 or 32 depending on whose name it was — a column that is meant to
        # line up and did not. Measured: 28 / 32 / 31 / 28 for four names.
        pad = max(28 - _w(head), 1)
        head += " " * pad + state
        # WHAT THEY ARE DOING NOW BEATS WHAT THEY SAID ON ARRIVAL. The focus is
        # a sentence from the join, hours old by lunchtime; the activity is the
        # answer to the question anybody actually has.
        if online and (doing_line := activity.describe(doing, width=48)):
            head += f"  {doing_line}"
        elif (focus := person.get("focus") or ""):
            head += f"  {focus}"
        colour = _speaker_pair(name)
        # And clipped by columns too: `head[:width]` cut by characters, so a row
        # with a wide name was built wider than the pane and the terminal
        # dropped whatever hung over — the state word, usually.
        rows.append(Row(_clip(head, width), C_ONLINE if online else C_OFFLINE,
                        curses.A_BOLD if online else curses.A_DIM,
                        edge=colour, head=min(len(who), width)))

        # The description line too, dimmed: it belongs to that person, and at a
        # glance the two lines read as one block rather than as two entries.
        detail = stat_line(person) or "nothing shared yet"
        line = f"     {detail}"[:width]
        rows.append(Row(line, C_DIM, curses.A_DIM,
                        edge=colour, head=len(line)))
    if not rows:
        rows.append(Row("  (waiting for the roster…)", C_DIM, curses.A_DIM))
    return rows


class Tui:
    """The viewer.

    ``view`` selects what this window shows: both panes, or just one of them.
    A single-pane view is what makes the tmux layout possible — two windows,
    each showing one half, with tmux doing the splitting so the user can resize
    and move them with the keys they already know.
    """

    def __init__(self, model: Model, view: str = "both") -> None:
        self.model = model
        self.view = view if view in ("both", "chat", "roster") else "both"
        # The roster reads from the top — following its tail would hide whoever
        # joined first, including yourself. Only the conversation tails.
        self.roster = Pane(follow=False)
        self.chat = Pane()
        self.focus = "roster" if self.view == "roster" else "chat"
        #: Messages unfolded by hand. Empty = everything folded.
        self.expanded: set[int] = set()
        #: Where the conversation starts on screen and which rows are showing.
        #: Filled by draw() and read by the click handler; it also lets the
        #: wheel tell the panes apart.
        self._chat_top = 0
        self._chat_rows: list[Row] = []
        #: Where the way back to the live end was drawn on the bottom row, as
        #: a half-open column span, and the row it was drawn on. Recorded by
        #: the draw rather than worked out again by the click handler: a
        #: handler that re-derives a control's position by searching the line
        #: for a bracket is a handler that breaks the day somebody's status
        #: command prints one.
        self._jump: tuple[int, int] = (0, 0)
        self._jump_y = -1
        #: The vertical scrollbars painted this frame, if any. Empty is the
        #: ordinary case: a pane whose content fits does not get one, and does
        #: not lose the column either.
        self._gutters: list[Gutter] = []
        #: The theme the rows on screen were built with, so a change can be
        #: noticed and the reader's place kept across it.
        self._theme_drawn: str = ""
        #: WHAT THE CACHED ROWS WERE BUILT FROM. Laying out the conversation is
        #: linear in the whole history —every message wrapped, folded and framed
        #: again— and it was being done on every redraw, four times a second and
        #: once per keystroke. Nothing in it depends on where you are scrolled,
        #: so it is done when an input actually changes and not before: that is
        #: the difference between a wheel notch costing a second and costing
        #: nothing.
        self._rows_key: tuple = ()
        self._chat_width = 0
        self._roster_key: tuple = ()
        self._roster_rows: list[Row] = []
        #: The user's own command for the bottom row. It holds the last line
        #: that command printed and runs it on a timer in a thread of its own;
        #: the draw never does more than read the string. See statusbar.
        self._command = statusbar.CommandSegment()
        #: The state directory's name for the title bar, and when it was last
        #: asked for. Asking costs a directory listing and a pid check per
        #: claim, which is nothing once and something four times a second.
        self._where_label = ""
        self._where_at = 0.0
        #: Whether the bottom row exists at all, decided once per draw and read
        #: by the geometry: with the row off, the line it reserves belongs to
        #: the panes, and a viewer that hid the row but kept the gap would just
        #: have traded a useful line for a blank one.
        self._bar = True
        self._settings: dict[str, Any] = watch_status_settings()
        #: The roster panel's own bottom row. Its own settings rather than more
        #: segments on the one above: that row is the reader's and this one is
        #: the session's, and what may go on them differs because of it.
        self._roster_settings: dict[str, Any] = watch_roster_settings()

    # -- cached layout -------------------------------------------------------

    def _conversation(self, width: int) -> list[Row]:
        """The conversation as rows, rebuilt only when something moved."""
        events = self.model.events
        key = (width, len(events), int(getattr(events[-1], "seq", 0) or 0) if events else 0,
               frozenset(self.expanded), _theme_version(),
               self.model.profile.name, _colour_stamp())
        if key != self._rows_key:
            self._rows_key = key
            self._chat_width = width
            self._chat_rows = conversation_rows(events, width,
                                                self.model.profile.name,
                                                self.expanded)
        return self._chat_rows

    def reach_back(self) -> int:
        """At the top of what is loaded, pull the page before it in.

        And put the message you were reading back where it was: the new rows
        arrive ABOVE it, so leaving the offset alone would jump the pane a page
        further back and read as an overshoot rather than as more history.
        """
        if self.chat.offset > 0 or not self.model.more_above():
            return 0
        seq = self.chat.top_seq(self._chat_rows)
        added = self.model.load_older()
        if not added or not self._chat_width:
            return added
        rows = self._conversation(self._chat_width)
        self.chat.follow = False
        self.chat.total = len(rows)
        self.chat.hold(seq, rows)
        return added

    def reach_forward(self) -> int:
        """At the bottom of the window, pull the page after it in.

        The mirror of reach_back, and the reason scrolling down out of the
        history does not stop dead one screen short of what is being said now.
        """
        rows = self._chat_rows
        at_bottom = self.chat.offset >= max(len(rows) - self.chat.rows, 0)
        if not at_bottom or not self.model.pending():
            return 0
        seq = self.chat.top_seq(rows)
        added = self.model.load_newer()
        if not added or not self._chat_width:
            return added
        rows = self._conversation(self._chat_width)
        self.chat.total = len(rows)
        if not self.chat.follow:
            self.chat.hold(seq, rows)
        return added

    def _roster(self, width: int) -> list[Row]:
        people = self.model.participants()
        # The whole roster as text, plus a coarse clock: the rows carry «4m
        # ago», which goes stale while the snapshot itself says the same thing.
        key = (width, _theme_version(), _colour_stamp(), repr(people),
               int(time.monotonic() // 5))
        if key != self._roster_key:
            self._roster_key = key
            self._roster_rows = roster_rows(self.model, width)
        return self._roster_rows

    # -- drawing ------------------------------------------------------------

    def _hline(self, win, y: int, width: int, label: str) -> None:
        win.attron(curses.color_pair(C_DIM))
        win.hline(y, 0, curses.ACS_HLINE, width)
        win.attroff(curses.color_pair(C_DIM))
        if label:
            focused = (label.lower().startswith("participants") and self.focus == "roster") \
                or (label.lower().startswith("conversation") and self.focus == "chat")
            attr = curses.color_pair(C_ACCENT) | (curses.A_BOLD if focused else 0)
            text = f" {label} "
            win.addnstr(y, 2, text, max(width - 4, 0), attr)


    def _paint_row(self, win, y: int, row: "Row", width: int) -> None:
        """Paint one row, then repaint the parts that carry a second colour.

        The body takes the colour of what the line SAYS —red on a failure, green
        on a pass— and the `│` live inside that same string. They are repainted
        with `row.edge`, the colour of WHO SENT IT: so the frame identifies the
        sender at a glance and the inside is free to say how it went. Two
        different pieces of information in two different places.

        `row.head` does the same for the roster, where the dot and the name take
        the person's colour and the state keeps its own.
        """
        try:
            win.addnstr(y, 0, row.text or " ", width, curses.color_pair(row.pair)
                        | row.attr)
        except curses.error:
            return
        if row.head and row.edge:
            try:
                win.addnstr(y, 0, row.text[:row.head],
                            min(row.head, width),
                            curses.color_pair(row.edge) | row.attr)
            except curses.error:
                pass
        if not row.text or row.text.lstrip()[:1] != _V:
            return
        marco = curses.color_pair(row.edge or C_TEXT)
        for col in (row.text.find(_V), row.text.rfind(_V)):
            if 0 <= col < width:
                try:
                    win.addnstr(y, col, _V, 1, marco)
                except curses.error:
                    pass

    def draw(self, win) -> None:
        """Paint a frame. A curses failure here loses A FRAME, not the viewer.

        This is what «the watch pane dies» was: `addnwstr() returned ERR` from
        one write to the last cell of a pane somebody had just dragged narrow,
        straight out through curses.wrapper, and `collab watch` fell back to the
        plain scrolling transcript — the full-screen view gone for good over a
        single character it could not place. Every write in here is bounded, and
        the ones that are not are the ones that get it wrong; a dropped frame is
        repainted a quarter of a second later and nobody sees it.
        """
        try:
            self._draw(win)
        except curses.error:
            try:
                win.refresh()
            except curses.error:
                pass

    def _draw(self, win) -> None:
        win.erase()
        # Forgotten with the frame they belong to: a gutter left behind from a
        # pane that no longer has one is a click target over live text.
        self._gutters = []
        # ASKED ONCE PER FRAME, from the config, so a change made in another
        # terminal reaches a pane that is already open — the same live reload
        # `theme` has. `load_config` re-reads only when the file's mtime or
        # size moves, so this costs a stat and nothing else.
        self._settings = watch_status_settings()
        self._bar = bool(self._settings["enabled"])
        self._roster_settings = watch_roster_settings()
        if self._bar:
            self._command.poll(self._settings["command"],
                               self._settings["interval"])
        height, width = win.getmaxyx()
        if height < 4 or width < 24:
            # Never into the last cell of the last row: on a one-column pane
            # that is the write that ends the viewer.
            room = max(width - 1, 0)
            if room:
                win.addnstr(0, 0, "window too small"[:room], room)
            win.refresh()
            return

        if self.view != "both":
            self._draw_single(win, height, width)
            win.refresh()
            return

        if height < 8:
            # Not enough room for two panes; show the conversation rather than
            # squeezing both into something unreadable.
            self.view, restore = "chat", True
            self._draw_single(win, height, width)
            self.view = "both" if restore else self.view
            win.refresh()
            return

        # --- title bar -----------------------------------------------------
        m = self.model
        state = m.state()
        state_pair = {"live": C_ONLINE, "reconnecting": C_WARN}.get(state, C_OFFLINE)
        title = m.title()
        left = f" {title} "
        # WHO WE ARE IS `is_host`, NOT WHETHER OUR NAME IS THE HOST'S. Two
        # agents on one machine resolve the same default display name, so the
        # name comparison this used to be called the guest «(host)» and two
        # viewers in two terminals read the same. One rule for this bar and
        # the status line, in statusbar.who; the directory label is what tells
        # two same-named agents apart when they share a checkout.
        who = statusbar.who(m.profile.name, m.profile.host_name,
                            is_host=m.profile.is_host, where=self._where())
        right = f" {who} "
        version = m.status.get("version") or ""

        win.attron(curses.color_pair(C_TITLE) | curses.A_BOLD)
        win.hline(0, 0, " ", width)
        win.addnstr(0, 0, left, max(width - 1, 0))
        win.attroff(curses.color_pair(C_TITLE) | curses.A_BOLD)
        tail = f"{right}"
        if note := statusbar.daemon_note(m.status):
            # The file we draw from is written by a daemon on OTHER code. Every
            # field it never heard of is simply missing, and the pane drew the
            # fewer segments in silence — an upgrade with a session open left
            # its host with no message count and no explanation for a whole
            # afternoon. Said here, where the version already was.
            tail += f" {note} "
        elif version:
            tail += f" v{version} "
        if note := statusbar.hub_note(m.status):
            # And the host's hub, whose snapshot is what every pane in the
            # session draws from. Only the host can replace it, and the
            # wording says so — a guest reading this has nothing to restart.
            tail += f" {note} "
        # Placed by COLUMNS: the tail holds a `→` or a `—`, and a name may be
        # anything; `len()` put a kanji name one cell past the pane's edge.
        win.addnstr(0, max(width - _w(tail) - 1, 0), _clip(tail, max(width - 1, 0)),
                    max(width - 1, 0), curses.color_pair(C_TITLE))
        badge = f" {state} "
        win.addnstr(1, 0, badge, max(width - 1, 0),
                    curses.color_pair(state_pair) | curses.A_BOLD)
        people = m.participants()
        # THE COUNT IS A CLAIM TOO. The rows below it stopped saying «online»
        # once we lost the feed, and this line went on saying «2/3 online» one
        # column from a badge reading «offline» — the same staleness, in the
        # place the eye lands first.
        if m.roster_is_current():
            online = sum(1 for p in people if p.get("connected"))
            summary = f"{online}/{len(people)} online"
        else:
            summary = f"{len(people)} here, none confirmed"
        win.addnstr(1, len(badge) + 1, summary, max(width - len(badge) - 2, 0),
                    curses.color_pair(C_DIM))

        # --- geometry ------------------------------------------------------
        body_top = 2
        # The last row belongs to the bottom bar only while there IS one.
        # Reserved unconditionally, turning the bar off bought a blank line
        # instead of a line of conversation.
        foot = 1 if self._bar else 0
        body_height = height - body_top - foot
        roster_h = max(int(body_height * ROSTER_SHARE), MIN_ROSTER_ROWS)
        # Leave the conversation room to exist, but never squeeze the roster
        # out entirely: at MIN_ROSTER_ROWS-1 visible rows it renders nothing at
        # all, and a pane you cannot see is a pane you cannot scroll.
        roster_h = min(roster_h, max(body_height - 4, 2))

        # THE ROW IS TAKEN ONLY WHEN IT IS USED, and only where a whole
        # participant still fits after it. Reserved unconditionally, a session
        # with nothing to say — no batch, no count fetched yet, a daemon from
        # before the count existed — would have cost the roster a blank line;
        # and a roster is two rows per person, so taking one from a three-row
        # pane leaves half a participant, which is worse than no figures at all.
        # (A hub that has counted zero HAS something to say — «0 messages» —
        # and takes the row; see statusbar.messages_segment.)
        session = self._roster_bar() if self._roster_settings["enabled"] else []
        session_h = 1 if session and roster_h - 2 >= 2 else 0
        # A RULE ABOVE THAT ROW, drawn the way the section headers are and
        # labelled the way they are — `STATUS`, beside `PARTICIPANTS (3)` and
        # `CONVERSATION` — so the figures read as a section of the panel and
        # not as one more line of the list: they sat directly under the last
        # participant, in the same dim colour as the state words beside the
        # names. The rule costs a row too, and it is paid for after the row —
        # taken only while two rows of participants, one whole person, still
        # remain after it, and below that the rule is what goes, never a
        # participant and never the row. Out of the roster's allocation, so
        # `chat_top` does not move.
        rule_h = 1 if session_h and roster_h - 3 >= 2 else 0
        # AND A ROW OF AIR ON EITHER SIDE, on the same terms and paid for last:
        # one above the rule, so the last participant and the section header
        # do not touch, and one under the status row, so the figures do not
        # sit on the conversation's header. Each is taken only while a whole
        # person still fits after it, and they are the first to go when the
        # pane shrinks — the one below first, the one above next, then the
        # rule — because they are the only things at the foot that say
        # nothing. Also out of the roster's allocation.
        pad_top = 1 if rule_h and roster_h - 4 >= 2 else 0
        pad_bottom = 1 if pad_top and roster_h - 5 >= 2 else 0
        self.roster.rows = roster_h - 1 - session_h - rule_h - pad_top - pad_bottom
        # AND THE HEIGHT IS SETTLED BEFORE THE WIDTH IS ASKED FOR, because
        # `_gutter_width` reads `rows` to decide whether there is anything to
        # scroll. The gutter then costs the content a column, so the rows are
        # BUILT to the width that is left rather than trimmed to it afterwards:
        # trimming would cut the last character off a line that was made to fit.
        roster_gutter = self._gutter_width(self.roster)
        rows = self._roster(width - 1 - roster_gutter * 2)
        self.roster.total = len(rows)
        self.roster.settle()
        hidden = max(len(rows) - self.roster.rows - self.roster.offset, 0)
        label = self._roster_label(people)
        # Rows are not people — two lines each — so a range of row numbers
        # beside a head count reads as a contradiction. Say which way there is
        # more instead, which is the only thing the number was for.
        if hidden or self.roster.offset:
            more = ("▴" if self.roster.offset else "") + ("▾" if hidden else "")
            label += f" · scroll {more} (tab, or [ ])"
        self._hline(win, body_top, width, label)
        for i in range(self.roster.rows):
            idx = self.roster.offset + i
            if idx >= len(rows):
                break
            # Through _paint_row like the conversation, and not with a bare
            # addnstr: the roster rows carry a head in the person's own colour,
            # and painting them here by hand meant that colour was computed and
            # then thrown away — the split view showed none of it while the
            # roster-only view did.
            self._paint_row(win, body_top + 1 + i, rows[idx],
                            width - 1 - roster_gutter * 2)
        if roster_gutter:
            self._paint_gutter(win, width - 2, body_top + 1, self.roster.rows,
                               self.roster)

        chat_top = body_top + roster_h
        # From the bottom of the panel up: the padding under the row (left
        # blank), the row, the rule, the padding above it (left blank).
        row_y = chat_top - 1 - pad_bottom
        if rule_h:
            self._hline(win, row_y - 1, width, "STATUS")
        if session_h:
            # THE LAST ROW OF THE ROSTER PANEL, mirroring the conversation's
            # own row at the last row of the window — one bar per panel, each
            # at the foot of what it describes. Pinned rather than part of
            # `rows`: the roster scrolls and this does not, and figures about
            # the whole session that scrolled away the moment somebody looked
            # down the participant list would be figures you could only read by
            # not using the pane.
            #
            # AND NOTHING ON IT IS GIVEN UP FOR WIDTH. There is no legend here,
            # so every part is a figure, and a figure this row lost is the
            # feature not working: in a `collab watch --tmux` pane the count
            # went and the batch kept its glyphs. Narrow forms first, then a
            # clip that shows — see `statusbar.fit`.
            self._paint_bar(win, row_y, width, session, keep=len(session))
        self._chat_top = chat_top
        self._hline(win, chat_top, width, self._chat_label())

        # WHERE WE WERE, IN MESSAGES, BEFORE THE ROWS ARE REBUILT. Taken from
        # the rows that are on screen right now, because after the rebuild the
        # offset may point at a different message entirely.
        was_at = self.chat.top_seq(self._chat_rows) if self._chat_rows else 0
        theme_before = self._theme_drawn

        # ONE FOOT ROW, AND IT IS THE STATUS BAR'S. This branch used to draw a
        # scrollbar of its own down there and reserved the row unconditionally;
        # the status row got there first and says more, so the conversation's
        # height is its arithmetic and the position is told down the side
        # instead. `rows` is set before the gutter is measured because
        # `_gutter_width` reads it — what comes from the previous frame is
        # `total`, set below.
        self.chat.rows = height - chat_top - 1 - foot
        chat_gutter = self._gutter_width(self.chat)
        chat_rows = self._conversation(width - 1 - chat_gutter * 2)
        self._chat_top = chat_top + 1
        self.chat.total = len(chat_rows)
        self.chat.settle()

        # A theme change moves every row. Put the message you were reading back
        # under your eyes instead of leaving the offset pointing at whatever
        # now happens to live at that row number.
        self._theme_drawn = theme()
        if theme_before and self._theme_drawn != theme_before:
            self.chat.hold(was_at, chat_rows)
        for i in range(self.chat.rows):
            idx = self.chat.offset + i
            if idx >= len(chat_rows):
                break
            self._paint_row(win, chat_top + 1 + i, chat_rows[idx],
                            width - 1 - chat_gutter * 2)
        if chat_gutter:
            self._paint_gutter(win, width - 2, chat_top + 1, self.chat.rows,
                               self.chat, more_above=m.more_above(),
                               more_below=bool(m.pending()))

        # --- help ----------------------------------------------------------
        self._hint(win, height, width)
        win.refresh()

    def _gutter_width(self, pane: "Pane") -> int:
        """One column, or none — by what the theme asks for.

        Decided from the row count the LAST frame arrived at. Deciding it from
        this frame's would mean laying the conversation out twice at two
        widths, once to ask the question and once to answer it — and the layout
        is cached per width, so the two would evict each other on every frame
        and the cache would stop existing.

        The one-frame lag cannot oscillate: taking a column away only ever
        makes text wrap into MORE rows, never fewer, so a pane that needs the
        bar still needs it once it has it.

        A PANE WITH NO HEIGHT GETS NOTHING, WHATEVER THE SETTING SAYS. `always`
        is a preference about a pane you can see; before the first frame there
        is none to draw beside, and honouring it there would put a bar on every
        pane in the window for one frame.
        """
        if pane.rows <= 0:
            return 0
        mode = _current_theme()["scrollbar_side"]
        if mode == "off":
            return 0
        if mode == "always":
            return 1
        return 1 if pane.total > pane.rows else 0

    def _paint_gutter(self, win, x: int, top: int, rows: int, pane: "Pane", *,
                      more_above: bool = False,
                      more_below: bool = False) -> None:
        """Paint the vertical scrollbar and remember where it went."""
        if rows <= 0:
            return
        cells = scroll_track(rows, pane.offset, pane.rows, pane.total,
                             more_above=more_above, more_below=more_below,
                             glyphs=(V_RAIL, V_THUMB, V_UNLOADED))
        for i, cell in enumerate(cells):
            tone = C_DIM if cell == V_RAIL else C_ACCENT
            attr = curses.A_DIM if cell == V_RAIL else curses.A_BOLD
            try:
                win.addnstr(top + i, x, cell, 1, curses.color_pair(tone) | attr)
            except curses.error:
                pass
        self._gutters.append(Gutter(x=x, top=top, rows=rows, pane=pane))

    def _where(self) -> str:
        """The state directory's name, when two agents share this checkout.

        Re-asked every few seconds rather than once: the second agent may join
        after the pane was opened, and the label exists for the moment there
        are two. See statusbar.state_dir_label for why it is otherwise empty.
        """
        now = time.time()
        if now - self._where_at > 5.0:
            self._where_label = statusbar.state_dir_label(self.model.profile.home)
            self._where_at = now
        return self._where_label

    def _hint(self, win, height: int, width: int,
              keys: tuple[str, str] = (CHAT_KEYS, CHAT_KEYS_SHORT),
              notice: bool = True, roster: bool = False) -> None:
        """The bottom row: what you are missing, then whatever else fits.

        Scrolled back, the count is the point — «G to resume following» does not
        say whether anything has been said since you left, which is the only
        reason to go back down. The key is named twice, End first: it is the one
        people try, and the one that needs no explaining. That notice goes in
        first and `statusbar.fit` never drops it; everything after it is a
        segment and every segment is expendable.

        The line used to be cut with `line[:width - 1]`. That was right while
        the row held nothing but ASCII key names and stopped being right the
        moment a block bar, a `⏸` and a user's command could land on it: one
        kanji is two columns and one character, so a character slice measured
        the row in the wrong unit and over-ran the pane by however many wide
        characters it contained.

        `roster` is set where this row IS the roster panel's row rather than
        the conversation's — the roster-only view, whose one pane has one
        bottom row. It carries the session's figures there instead of a second
        row being stacked above it for them: that pane already has a bar at the
        foot of the roster, and a second one would cost a participant to say
        what the first had room for.
        """
        # WHICHEVER SWITCH OWNS THIS ROW IS THE ONE THAT DECIDES IT. `_bar` is
        # `watch_status`, which governs the row carrying the READER'S figures;
        # the roster row carries the SESSION'S and has its own key. Returning on
        # `_bar` alone meant turning off the personal row silently took the
        # shared one with it — in the roster-only view, which the two keys exist
        # to tell apart and which has no title bar to carry those figures
        # instead.
        # Either switch can put a row here: with the session row off, this pane
        # falls through to the reader's own bar below, which is what `_bar`
        # governs — so the test is whether ANYTHING was asked for, not whether
        # the personal one was.
        if not (self._bar or (roster and self._roster_settings["enabled"])):
            return
        behind = 0 if self.chat.follow or not notice else self.behind()
        what = ""
        if notice and not self.chat.follow:
            what = (f"⏸ {behind} new below" if behind else "⏸ scrolled back")
            what += " — End (or G) jumps to the newest"
        keep = 1
        if roster and self._roster_settings["enabled"]:
            parts = self._roster_bar(keys=keys)
            # EVERY FIGURE ON THIS ROW IS KEPT, and only the legend may go. The
            # figures are what the row is for; the legend is the same words
            # every session. `compose` appends the very object it was handed
            # for the keys, so identity is what tells the legend apart from a
            # figure that happened to render as the same text.
            keep = sum(1 for part in parts if part is not keys)
        else:
            parts = statusbar.compose(notice=what, keys=keys,
                                      batch=self.model.status.get("batch"),
                                      stats=self.model.own_stats,
                                      command=self._command.text(),
                                      segments=self._settings["segments"])
        line = self._paint_bar(win, height - 1, width, parts, behind=behind,
                               keep=keep)
        # THE NOTICE IS THE WAY BACK, so a click on it is too. It already says
        # what the click would do — «End (or G) jumps to the newest» — and it
        # is the one part of this row that is news rather than a reminder, so
        # it needs no bracket around it to earn a hand: the alternative was
        # three more columns saying a second time what the sentence says once.
        #
        # MEASURED FROM THE LINE THAT WAS DRAWN, not from `what`. `fit` puts
        # the notice first and never drops it, but it will CLIP it when the row
        # cannot hold it, and a span taken from the unclipped text would then
        # reach past the end of the row and answer clicks that landed on
        # nothing.
        #
        # AND NEVER ON THE ROSTER'S ROW. That one is drawn by this same method
        # with `roster=True`, over a pane with no conversation in it: there is
        # nowhere to jump to, so it claims no row and the click stays a no-op.
        self._jump_y = -1 if roster else height - 1
        self._jump = ((0, min(_w(what), _w(line)))
                      if what and not roster else (0, 0))

    def _roster_bar(self, keys: Any = "") -> list[Any]:
        """The roster panel's row: what is true for EVERY participant, or nothing.

        Composed by the same function and drawn by the same renderers as the
        conversation's row — one batch bar and not two that could drift — but
        out of a different and much shorter list of ingredients. Everything on
        it comes off the hub's own snapshot, counted once by the hub and copied
        into `status.json` whole.

        WHAT IS DELIBERATELY ABSENT is the point of the method. Most of what
        the daemon writes into that file is written from the READER's point of
        view: `others_connected` and `others_total` filter the reader out by
        participant id so a daemon does not count itself, `unread` and
        `unread_messages` are properties of one inbox, `watchers` and
        `ws_clients` are that daemon's own subscribers. Four participants would
        read four different numbers off any of them, and they would do it
        beside a hub-counted batch bar that genuinely is shared, lending them
        credit they had not earned — the exact failure the batch feature exists
        to prevent. `config.WATCH_ROSTER_SEGMENTS` is what enforces the rule: a
        reader cannot put `stats` or `command` on this row even by hand.

        Empty when there is nothing true to say, and `_draw` then leaves the
        row to the roster. No figure is better than a false zero.
        """
        return statusbar.compose(
            keys=keys,
            batch=self.model.status.get("batch"),
            messages=self.model.status.get("messages"),
            segments=self._roster_settings["segments"])

    def _paint_bar(self, win, y: int, width: int, parts: list[Any], *,
                   behind: int = 0, keep: int = 1) -> str:
        """Fit a composed row to the pane and put it on the screen.

        One painter for both rows, so the column arithmetic is written once and
        the second row cannot acquire its own version of it. Through
        `statusbar.fit` with this file's own `_w`/`_clip`: a row holds a block
        bar, a `⏸`, a `→` and whatever a user's command printed, and cutting
        CHARACTERS to fit COLUMNS is only ever right for ASCII. A write past
        the last cell of a pane is what ends the viewer rather than the frame.

        RETURNS THE LINE IT DREW, so a caller with a control on the row can
        measure its span from what actually reached the screen rather than from
        what it hoped would fit. `fit` narrows and clips; a span taken from the
        text before that answers clicks that landed on empty terminal.

        `keep` is how many of the leading parts may be clipped but never
        dropped — one for the reader's row, where it is the scrolled-back
        notice, and every figure for the roster's; see `statusbar.fit`.
        """
        room = max(width - 1, 0)
        if not room:
            return ""
        line = statusbar.fit(parts, room, _w, _clip, keep=keep)
        attr = (curses.color_pair(C_ACCENT) | curses.A_BOLD if behind
                else curses.color_pair(C_DIM) | curses.A_DIM)
        try:
            win.addnstr(y, 0, line, room, attr)
        except curses.error:
            pass
        return line

    def _roster_label(self, people: list) -> str:
        """Say when this is a memory rather than an observation.

        The count and the names stay — they are still who was here — but a
        header that reads the same whether the hub answered a second ago or
        died an hour ago is what let a dead session look like a busy one.
        """
        label = f"PARTICIPANTS ({len(people)})"
        if not self.model.roster_is_current():
            age = self.model.snapshot_age()
            label += f" · not connected — as of {age}" if age else " · not connected"
        return label

    def _chat_label(self) -> str:
        """Say when the top of the screen is not the top of the conversation.

        A viewer that opens on the last hundred messages and says nothing about
        it is indistinguishable from one showing everything there is, which is
        how you end up believing history was lost.
        """
        if self.model.more_above():
            return "CONVERSATION · older above (keep scrolling, or g)"
        return "CONVERSATION"

    def behind(self) -> int:
        """How many messages there are below what is on screen.

        Counted in MESSAGES and not in rows: rows are a rendering detail —
        the same message is five of them in one theme and one in another — and
        «37 new below» would mean nothing to the reader if it moved with the
        theme.
        """
        rows = self._chat_rows
        if not rows or self.chat.follow:
            return 0
        last = self.chat.offset + max(self.chat.rows, 1) - 1
        if last >= len(rows) - 1:
            return 0
        visible = [row.seq for row in rows[self.chat.offset:last + 1] if row.seq]
        if not visible:
            # A viewport showing nothing but separators: fall back to counting
            # the messages that begin below it.
            return len({row.seq for row in rows[last + 1:] if row.seq})
        top = max(visible)
        loaded = sum(1 for env in self.model.events
                     if int(getattr(env, "seq", 0) or 0) > top)
        # PLUS WHAT IS NOT LOADED. The window is held still while you read, so
        # what has arrived since sits on disk — and it is the part of «how far
        # behind am I» that actually grows.
        return loaded + self.model.pending()

    # -- input --------------------------------------------------------------

    def pane_at(self, y: int) -> str:
        """Which pane is under this screen row.

        The wheel should scroll what the pointer is over — asking someone to
        first tab focus across and then scroll is asking them to know the
        thing they are trying to find out.
        """
        if self.view != "both":
            return "roster" if self.view == "roster" else "chat"
        if self._chat_top and y >= self._chat_top:
            return "chat"
        return "roster"

    def handle_mouse(self) -> bool:
        """One wheel notch, or a click on a «show more» button.

        BOTH, and in one place: the wheel and the click arrive as the same
        KEY_MOUSE, and `getmouse` hands out an event once — a second handler
        reading it after this one gets nothing. Splitting them cost the button
        its click, because whichever branch ran first answered for the other.

        Silent when the terminal reports no mouse.
        """
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return True
        where = self.pane_at(y)
        pane = self.roster if where == "roster" else self.chat
        up = state & getattr(curses, "BUTTON4_PRESSED", 0)
        # Wheel-down is button 5 wherever ncurses was built with five buttons.
        # Where it was not, the wheel arrives as button 2 instead — and there,
        # middle-click is indistinguishable from a scroll, so only fall back to
        # it when there is no button 5 to prefer.
        if hasattr(curses, "BUTTON5_PRESSED"):
            down = state & curses.BUTTON5_PRESSED
        else:
            down = state & getattr(curses, "BUTTON2_PRESSED", 0)
        if up:
            pane.scroll(-WHEEL_LINES)
        elif down:
            pane.scroll(WHEEL_LINES)
        else:
            # Not the wheel, so a button. Two things on screen answer a click:
            # the bottom bar, and a «show more» in the conversation. Anywhere
            # else it does nothing — the mouse must not move the focus or
            # select by accident while somebody is reading.
            if state & _CLICK:
                if y == self._jump_y:
                    self._bar_click(x)
                elif not self._gutter_click(x, y):
                    self._fold_at(y, where)
            return True
        # Scrolling a pane is also a statement about which one you care about.
        if self.view == "both":
            self.focus = where
        if where == "chat":
            self.reach_back() if up else self.reach_forward()
        return True

    #: The keys that mean «earlier» and «later». Reaching for more history is
    #: decided by which of these was pressed, not by where the offset landed.
    _BACKWARD = frozenset({curses.KEY_UP, ord("k"), curses.KEY_PPAGE, 21})
    _FORWARD = frozenset({curses.KEY_DOWN, ord("j"), curses.KEY_NPAGE, 4})

    def handle(self, key: int) -> bool:
        """Returns False when the user asked to leave."""
        if self.view != "both":
            self.focus = "roster" if self.view == "roster" else "chat"
        if key == curses.KEY_MOUSE:
            return self.handle_mouse()
        pane = self.roster if self.focus == "roster" else self.chat
        # Deliberately not ESC: terminals send a bare ESC as the first byte of
        # every escape sequence — focus events, bracketed paste, cursor-position
        # replies — so quitting on it makes the view close itself at random.
        if key in (ord("q"), ord("Q")):
            return False
        if key == ord("\t"):
            self.focus = "roster" if self.focus == "chat" else "chat"
        elif key in (curses.KEY_UP, ord("k")):
            pane.scroll(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            pane.scroll(1)
        elif key == curses.KEY_PPAGE:
            pane.scroll(-max(pane.rows - 1, 1))
        elif key == curses.KEY_NPAGE:
            pane.scroll(max(pane.rows - 1, 1))
        # BACK TO THE NEWEST, on the key people actually press. `G` is vi's and
        # stays, but somebody who has scrolled up in a chat window reaches for
        # End — and when it did nothing, the way back to the live tail was to
        # hold ↓ through the history you had just scrolled past.
        elif key in (ord("G"), curses.KEY_END, curses.KEY_LL):
            if pane is self.chat:
                self.jump_to_newest()
            else:
                pane.to_end()
        elif key in (ord("g"), curses.KEY_HOME, curses.KEY_FIND):
            if pane is self.chat:
                # And the top means the top of the CONVERSATION, not the top of
                # what happened to be loaded.
                self.model.load_start()
                self._chat_rows = self._conversation(self._chat_width or 80)
                self.chat.total = len(self._chat_rows)
            pane.to_start()
        # Ctrl-D / Ctrl-U, half a pane at a time: the wheel's step is too small
        # for crossing a long history and a full page is too big to read across.
        elif key == 4:
            pane.scroll(max(pane.rows // 2, 1))
        elif key == 21:
            pane.scroll(-max(pane.rows // 2, 1))
        # The roster is the pane you dip into and leave, so it gets keys that
        # do not require taking focus away from the conversation first.
        elif key in (ord("["), curses.KEY_SR):
            self.roster.scroll(-1)
        elif key in (ord("]"), curses.KEY_SF):
            self.roster.scroll(1)
        # BY THE DIRECTION ASKED FOR, not by where the offset ended up. A
        # window smaller than the pane is at the top and at the bottom at the
        # same time, so the state cannot tell «show me what came before» from
        # «I am at the live end» — and that is the ordinary case now that the
        # pane opens on a handful of messages.
        if pane is self.chat and key in self._BACKWARD:
            self.reach_back()
        elif pane is self.chat and key in self._FORWARD:
            self.reach_forward()
        return True

    def jump_to_newest(self) -> None:
        """Back to what is being said NOW, not to the end of the window.

        A reader who scrolled away an hour ago is fifty messages behind the
        live end, and walking there a page at a time is the exact thing this
        exists to avoid. One implementation because two ways to ask for it —
        the End key and the button on the bar — must not be able to disagree
        about what «the newest» means.
        """
        self.model.load_tail()
        self._chat_rows = self._conversation(self._chat_width or 80)
        self.chat.total = len(self._chat_rows)
        self.chat.to_end()

    def seek(self, fraction: float, pane: "Pane | None" = None) -> None:
        """Put the reader that far down the loaded rows of a pane.

        What a click on a scrollbar means, on either axis. It travels the
        LOADED window and does not page: the rail is what it is pointing at,
        and a click that also pulled a page in would land somewhere other than
        where it was aimed.
        """
        pane = pane if pane is not None else self.chat
        limit = max(pane.total - pane.rows, 0)
        pane.follow = False
        pane.offset = int(round(max(0.0, min(fraction, 1.0)) * limit))
        pane.clamp()
        if pane.offset >= limit:
            # The roster does not follow anything, and saying it does would
            # make it jump to the newest arrival under whoever is reading it.
            pane.follow = pane is self.chat

    def _gutter_click(self, x: int, y: int) -> bool:
        """A click on a vertical scrollbar. False when it landed on neither."""
        for gutter in self._gutters:
            if gutter.holds(x, y):
                self.seek(gutter.fraction(y), gutter.pane)
                if self.view == "both":
                    # Scrolling a pane is a statement about which one you care
                    # about, and so is aiming at its scrollbar.
                    self.focus = "roster" if gutter.pane is self.roster else "chat"
                return True
        return False

    def _bar_click(self, x: int) -> bool:
        """A click on the bottom row. False when it landed on nothing.

        Nothing is the ordinary case — most of that row is a batch figure, a
        spend and a reminder of which keys exist — and it stays a no-op rather
        than being rounded to the nearest control. The one thing there that
        answers is the way back to the live end.

        RESOLVED AGAINST THE SPAN THE DRAW RECORDED, and only while the notice
        is actually on the row. Following the live end there is nothing to go
        back to, `_jump` is `(0, 0)`, and a click anywhere on the row does
        nothing at all.
        """
        start, end = self._jump
        if end > start and start <= x < end:
            self.jump_to_newest()
            return True
        return False

    def _fold_at(self, y: int, where: str) -> None:
        """Fold or unfold the message whose button row is at screen row ``y``.

        Anywhere else it does nothing — and «anywhere else» includes the roster
        and the title bar: the row is counted DOWN from the top of the
        conversation, so a click above it still lands on a row by arithmetic.
        """
        if where != "chat" or y < self._chat_top:
            return
        idx = self.chat.offset + (y - self._chat_top)
        if not (0 <= idx < len(self._chat_rows)):
            return
        row = self._chat_rows[idx]
        if not row.button or not row.seq:
            return
        self.expanded.symmetric_difference_update({row.seq})
        # Unfolding makes the conversation longer below: if we were following
        # the bottom, we stay at the bottom and do not end up halfway up.
        self.chat.settle()

    # -- single-pane views ---------------------------------------------------

    def _draw_single(self, win, height: int, width: int) -> None:
        """One half, filling the window. tmux owns the split in this mode."""
        m = self.model
        people = m.participants()
        pane = self.roster if self.view == "roster" else self.chat
        pane.rows = height - 2
        gutter = self._gutter_width(pane)
        content = width - 1 - gutter * 2
        if self.view == "roster":
            rows = self._roster(content)
            label = self._roster_label(people)
        else:
            rows = self._conversation(content)
            label = self._chat_label()
            self._chat_top = 1

        state = m.state()
        state_pair = {"live": C_ONLINE, "reconnecting": C_WARN}.get(state, C_OFFLINE)
        head = f" {m.title()} · {label} "
        win.attron(curses.color_pair(C_TITLE) | curses.A_BOLD)
        win.hline(0, 0, " ", width)
        win.addnstr(0, 0, head[:max(width - 1, 0)], max(width - 1, 0))
        win.attroff(curses.color_pair(C_TITLE) | curses.A_BOLD)
        badge = f" {state} "
        win.addnstr(0, max(width - len(badge) - 1, 0), badge[:max(width - 1, 0)],
                    max(width - 1, 0),
                    curses.color_pair(state_pair) | curses.A_BOLD)

        if self.view == "roster":
            # THE ROW IS RESERVED BY WHICHEVER SWITCH CLAIMS IT — the same
            # test `_hint` makes before it draws one. Reserved on `_bar` alone,
            # the session's row drawn with the personal one off landed on top
            # of the last participant row.
            #
            # AND THE RULE ABOVE IT is the roster's, as in the split view: with
            # the session's row, and only while two rows of participants remain
            # after paying for it. The reader's own row, when it is the one
            # down there, gets no rule — it is not the roster's foot.
            #
            # THE PADDING ABOVE THE RULE on the same terms, and none below the
            # row: the row is on the pane's last line, where the conversation's
            # own bar is in its pane, and under it is a tmux border that
            # already separates it from whatever is next.
            row = self._bar or self._roster_settings["enabled"]
            rule = self._roster_settings["enabled"] and height - 3 >= 2
            pad = rule and height - 4 >= 2
        else:
            row, rule, pad = self._bar, False, False
        pane.rows = height - 1 - (1 if row else 0) - (1 if rule else 0) - (1 if pad else 0)
        pane.total = len(rows)
        pane.settle()
        for i in range(pane.rows):
            idx = pane.offset + i
            if idx >= len(rows):
                break
            self._paint_row(win, 1 + i, rows[idx], content)
        if gutter:
            self._paint_gutter(
                win, width - 2, 1, pane.rows, pane,
                more_above=pane is self.chat and m.more_above(),
                more_below=pane is self.chat and bool(m.pending()))
        if rule:
            self._hline(win, height - 2, width, "STATUS")

        if self.view == "roster":
            # Its own keys, and no scrolled-back notice: the roster does not
            # follow a tail, so there is nothing to be behind.
            #
            # NO SECOND ROW IS SPENT HERE. This pane's one bottom row is the
            # roster panel's bottom row, so it carries the session's figures
            # rather than the reader's — the same bar the split view draws at
            # the foot of its roster, in the only place this view has for it.
            # A tmux pane showing nothing but the roster is also where those
            # figures are least otherwise available: the split view's title bar
            # says «2/3 online» and this one has no title bar to say it in.
            self._hint(win, height, width, notice=False, roster=True,
                       keys=(ROSTER_KEYS, ROSTER_KEYS_SHORT))
        else:
            self._hint(win, height, width)


def _pair_or(pair: int, colour: int, fallback: int) -> None:
    """`colour` on the default ground, or `fallback` where it does not exist.

    A hex colour resolves to an index out of the 256, and an eight-colour
    terminal has no such index — asking for it raises and would take the whole
    viewer down over a shade of blue.
    """
    try:
        curses.init_pair(pair, colour, -1)
    except (curses.error, ValueError):
        curses.init_pair(pair, fallback, -1)


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_ONLINE, curses.COLOR_GREEN, -1)
    curses.init_pair(C_OFFLINE, curses.COLOR_RED, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_CYAN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(C_BAD, curses.COLOR_RED, -1)
    curses.init_pair(C_WARNLINE, curses.COLOR_YELLOW, -1)
    _pair_or(C_INFO, _colour_index(INFO_HEX), curses.COLOR_BLUE)
    curses.init_pair(C_TEXT, curses.COLOR_WHITE, -1)
    curses.init_pair(C_BUTTON, curses.COLOR_YELLOW, -1)
    _deal_colours(curses.COLORS)
    for i, colour in enumerate(SPEAKER_COLORS):
        curses.init_pair(C_SPEAKER_BASE + i, colour, -1)


def run(profile: SessionProfile, view: str = "both", limit: int = OPEN_WITH,
        model: "Model | None" = None) -> int:
    """The viewer. ``model`` is for the simulated session: hand one in and the
    conversation comes from there instead of from this machine's log."""
    # MI PROPIO COLOR, DE LA CONFIG LOCAL, ANTES DE MIRAR EL ROSTER.
    #
    # Chosen colours arrive through the roster, which comes from the hub. Mine
    # does too — but only AFTER publishing it, and until then I saw myself in
    # a random colour on my own screen. You set your colour, open the chat and
    # it is not there: no way to tell whether it failed to save, failed to
    # travel, or the viewer ignores it. It is seeded here and the roster wins
    # afterwards,
    # which is right — what is published is the truth for everyone, me included.
    mine = default_color()
    if mine not in (None, ""):
        record_colours([{"name": n, "color": mine}
                          for n in my_names(profile.name)])
    model = model if model is not None else Model(profile=profile)
    model.load_initial(limit=limit)
    tui = Tui(model, view=view)

    def loop(win) -> int:
        # The mouse, for the «show more» button and the wheel. mouseinterval(0)
        # delivers the click on press instead of waiting for a double click,
        # which feels like an odd lag on a button.
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        _init_colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # some terminals cannot hide the cursor
        win.nodelay(True)
        win.keypad(True)
        # Ask for wheel events. A terminal that cannot do mice simply never
        # sends any, so this costs nothing where it is not supported — but
        # without it the wheel scrolls the terminal's scrollback instead, which
        # looks exactly like a pane that refuses to scroll.
        try:
            # Buttons only. Asking for motion reports as well turns every
            # pointer movement over the pane into an event to drain.
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except (AttributeError, curses.error):
            pass
        # Swallow the terminal's own replies rather than treating them as input.
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        # WAIT FOR INPUT INSTEAD OF SLEEPING PAST IT. With nodelay the loop
        # asked once, found nothing and slept a quarter of a second, so a key
        # pressed just after the ask sat unread for that long before anything
        # moved. Blocking with a timeout gives the same idle refresh rate and
        # answers a keystroke the moment it arrives.
        win.nodelay(False)
        win.timeout(int(POLL_SECONDS * 1000))
        last_side = 0.0
        while True:
            now = time.time()
            if now - last_side > 1.0:
                model.refresh_side()
                last_side = now
            # Only while the reader is at the bottom: see poll_events.
            model.poll_events(tui.chat.follow)
            tui.draw(win)

            # EVERYTHING WAITING, THEN ONE REDRAW. A wheel notch is three key
            # events and a spin is thirty; handling each with its own repaint
            # is what made a fast scroll lag behind the hand and then carry on
            # travelling after it stopped. Drained in one go, a spin moves the
            # pane once, by the whole distance.
            key = win.getch()
            if key == -1:
                continue
            win.timeout(0)
            try:
                while key != -1:
                    if key == 27:
                        # The tail of an escape sequence is not a command: an
                        # arrow key would otherwise scroll unbidden.
                        while win.getch() != -1:
                            pass
                        break
                    if key == curses.KEY_RESIZE:
                        _on_resize(win)
                    elif not tui.handle(key):
                        return 0
                    key = win.getch()
            except curses.error:
                pass
            finally:
                win.timeout(int(POLL_SECONDS * 1000))

    return curses.wrapper(loop)


def _on_resize(win) -> None:
    """A resize invalidates more than the geometry.

    `_screen_width` remembers what tmux said the window measured, keyed by the
    pane's width — and dragging a border changes both, so the cached answer has
    to go with them. The rows are rebuilt at the new width by the next draw.
    """
    _SCREEN.clear()
    try:
        curses.update_lines_cols()
    except (AttributeError, curses.error):
        pass
    try:
        win.clear()
    except curses.error:
        pass
