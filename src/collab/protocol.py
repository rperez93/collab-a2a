"""The collab extension v1 wire format.

Every collab payload travels inside a standard A2A ``Message`` as a structured
(JSON) ``Part``, so a stock A2A client sees valid A2A while a collab-aware
client sees the envelope below.  The same envelope shape is what the SSE feed
emits, one JSON object per event.
"""

from __future__ import annotations

import time
import unicodedata
import uuid
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from typing import Any

EXTENSION_URI = "https://github.com/collab-a2a/collab/ext/v1"
EXTENSION_VERSION = "v1"

#: Mounted paths.  The A2A JSON-RPC endpoint and our extension live on one app.
RPC_PATH = "/a2a"
EXT_PREFIX = "/ext/collab/v1"
#: The SDK REST binding mounts a greedy "/{tenant}" catch-all at its prefix root,
#: so it gets its own namespace rather than owning "/".
REST_PREFIX = "/rest"

DEFAULT_ROOM = "general"

# Envelope kinds.
KIND_CHAT = "chat"
KIND_TASK = "task"
KIND_HELLO = "hello"
KIND_PRESENCE = "presence"
KIND_FILE = "file"
KIND_SYSTEM = "system"
#: What an agent is doing right now — see collab.activity. It rides the feed
#: like everything else, because "are you working, and on what" is a question
#: that should be answered before it is asked.
KIND_ACTIVITY = "activity"

#: Every kind on the wire. `activity` was defined above and left out of this
#: set for a release, so the set that said "all of them" was one short — and
#: `test_all_kinds_is_every_kind_constant` now holds it to the constants.
ALL_KINDS = frozenset({KIND_CHAT, KIND_TASK, KIND_HELLO, KIND_PRESENCE,
                       KIND_FILE, KIND_SYSTEM, KIND_ACTIVITY})

#: The kinds a CLIENT may put on the wire itself. One. Every other kind is
#: stamped by the hub on the route that performs it — join writes `hello` and
#: `presence`, the task routes write `task`, the file routes write `file`, the
#: activity route writes `activity`, and `system` is the hub speaking for
#: itself — so a message arriving under any of them was either a client bug or
#: a client pretending to be one of those routes. Until this was checked, a
#: guest could post a `system` line that rendered as the hub's own words, put
#: text in front of everyone under a kind no unread count includes and no wake
#: fires for, and make every connected daemon re-pull the snapshot at will,
#: because four of these kinds are what tells a daemon its roster is stale.
CLIENT_KINDS = frozenset({KIND_CHAT})


def client_kind_refusal(kind: str) -> str | None:
    """Why a client may not send an envelope of this `kind`, or None if it may.

    The rule is stated here once and read by both doors a client can send
    through — the message route and A2A `SendMessage` — so the two cannot
    drift into refusing different things. Refused, not coerced to chat: a
    client that says `system` is either wrong or trying it on, and both
    deserve an answer they can see rather than a message that quietly landed
    as something else. An unknown kind gets the same answer as a hub-stamped
    one, since "not a kind at all" is no reason to let it through.
    """
    if kind in CLIENT_KINDS:
        return None
    return f"kind {kind!r} refused: chat is the only kind a client may send"


def short_state(state: str) -> str:
    """«TASK_STATE_WORKING» -> «working», the way a task state is shown.

    Here rather than in the caller because there are four of them — the CLI,
    the viewer, the TUI and the hub's own task line — and they each had their
    own copy of this. A renderer that cannot import the CLI writes the strip
    again, so the helper has to live where every renderer already looks.
    """
    return str(state).replace("TASK_STATE_", "").lower()


def task_line(body: dict[str, Any]) -> str:
    """«claim t_1 “ship it” [working] · alice» — one task envelope, on one line.

    The TUI and the watcher render a task identically and have to go on doing
    so; all that differs is what each does with the line afterwards — one wraps
    it to a width, the other paints it. Beside `short_state` for the same
    reason: neither renderer can import the other.

    `Envelope.render_line` deliberately does NOT use this. It writes the sender
    into the line and has no surrounding layout to lean on, so it says more.
    """
    state = short_state(body.get("state", ""))
    owner = f" · {body['owner']}" if body.get("owner") else ""
    return (f"{body.get('action', '')} {body.get('id', '')} "
            f"“{body.get('title', '')}” [{state}]{owner}")


def file_outcome(body: dict[str, Any]) -> str:
    """What an ack did to the host's copy, for every renderer of a `received`.

    A room file is not gone when one person has it. The event says how many
    are still to collect, and the transcript, the watch pane and the TUI all
    have to say the same thing about it — so the words are made here. An event
    from a hub that predates the count carries none of these keys and reads as
    the deletion it was.
    """
    if body.get("deleted", "remaining" not in body):
        return "deleted from the host"
    # A COUNT THAT IS NOT A COUNT SAYS NOTHING. This was `int(x or 0)`, and the
    # body is a remote party's: `"lots"` raised, and none of the four renderers
    # that call this — the transcript, the watch pane, the viewer, `file get`
    # — wrap the call, so a hub sending junk here took the reader down rather
    # than one line. `True` is an int to `int()` and is not one person still
    # to collect; a negative is not a count of anything.
    raw = body.get("remaining")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return "kept on the host for now"
    if raw == 0:
        # Nobody was awaited — an empty room, or a hub that never wrote the
        # audience down — so the clock ends it, not an ack.
        return "kept on the host for the rest of the room"
    # Names are the hub's too: scrubbed like every other remote string that
    # lands in a terminal, and a non-list is nobody rather than an error.
    names = body.get("awaiting")
    who = ", ".join(scrub(str(n)) for n in names
                    if isinstance(n, (str, int))) if isinstance(names, list) else ""
    still = f"{raw} still to collect"
    return f"{still} ({who})" if who else still


#: Files are shared out of band rather than pasted as text, so binaries and
#: build artifacts never have to be squeezed through a chat message.
MAX_FILE_BYTES = 10 * 1024 * 1024
#: A file addressed to ONE person waits a day for them to collect it.
FILE_TTL_SECONDS = 24 * 3600
#: A file shared with a ROOM waits for everyone who was there when it was sent,
#: or for this long — whichever comes first. Shorter than the direct TTL because
#: the room's copy is not held for anyone in particular: once the people it was
#: for have moved on, keeping it another day serves nobody and fills the host's
#: disk with artifacts nobody will ask for.
ROOM_FILE_TTL_SECONDS = 30 * 60


def scrub(text: str) -> str:
    """Strip control characters from a string before it reaches a terminal.

    A display name, a message, a task title and a file name are all chosen by
    another participant and all end up printed to this machine's terminal —
    `collab recv`, `collab listen`, `collab who`, `collab file list`, and the
    one-line Monitor render below. A raw ESC in one of those is not text: it is
    a command to the terminal. ``\\x1b[2J`` clears the reader's screen,
    ``\\x1b]0;…\\x07`` rewrites their window title, and a bare carriage return
    paints a forged line over a real one — so a remote name reading `alice`
    could carry a cursor-up and overwrite the line above it with anything.

    None of that is anything a name or a sentence needs, so every C0/C1 control
    byte (Unicode category ``Cc`` — ESC, CR, BEL, backspace, DEL and the rest)
    is removed, while every printable character is left exactly as it was:
    letters, spaces, emoji, CJK and combining marks all survive unchanged.

    The curses TUI needs none of this — ncurses renders a control byte as
    ``^[`` rather than passing it to the terminal — so this is for the
    plain-print paths, which hand the string straight to a real terminal.

    **Newlines and tabs go too, and that is deliberate.** Everything above
    renders one FIELD into one LINE — a name in a roster row, a title in a
    board listing, a message in the Monitor's single-line notification. A
    newline surviving into any of those does not merely spoil the layout: it
    lets a sender write a second line of their own into a transcript, and a
    forged ``[dm→you] alice: …`` reads exactly like a real one. A tab does the
    quieter version of the same thing to a column. For a whole message that is
    ALLOWED to span lines, use `scrub_block`.
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def scrub_block(text: str) -> str:
    """The same, for text that is a message rather than a field.

    `scrub` empties a string of every control character because its callers are
    building one line and a line break in one is a forgery. That is the wrong
    trade for an error: `HubError` falls back to the response body, so a dead
    tunnel's HTML 502 arrives as many lines, and flattening it turns something
    barely readable into something not readable at all.

    So newlines and tabs survive here and nothing else does — carriage return
    included, because CR is the character that paints a forged line over a real
    one, and it is no part of a line break that ``\\n`` does not already carry.
    """
    return "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) != "Cc")


#: Bounds on the free text a participant declares about itself. Every one of
#: these arrives from an untrusted joiner and is then stored and replayed to
#: every roster, so it is capped in length and count rather than trusted.
MAX_NAME = 64
MAX_ROOM = 64
MAX_TITLE = 200
MAX_DETAIL = 4_000
MAX_META_VALUE = 500
MAX_META_KEYS = 24


def clip(value: Any, limit: int) -> str:
    """A single field, trimmed and length-bounded on the way into the store."""
    return str(value).strip()[:limit]


def bounded_meta(raw: Any) -> dict[str, Any]:
    """A joiner's self-declared identity, capped so it cannot be a weapon.

    The join handshake's `hello` — focus, repo, branch and the like — is chosen
    by an untrusted joiner and stored straight into that participant's meta,
    which every roster snapshot then carries to everyone in the room every few
    seconds. Unbounded, a megabyte of display text is amplified across the whole
    session on a timer; and a nested `stats` or `activity` object smuggled in
    here would land in meta without ever passing the sanitiser that field has of
    its own. So only scalar values survive, each key and string is clipped, and
    the number of keys is capped. Anything richer belongs on its own endpoint.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= MAX_META_KEYS:
            break
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key[:MAX_META_VALUE]] = value
        elif isinstance(value, str):
            out[key[:MAX_META_VALUE]] = value[:MAX_META_VALUE]
        # A dict or a list is dropped on purpose: stats and activity reach the
        # roster through their own sanitised endpoints, never through hello.
    return out


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    """Timestamps travel in UTC so participants in different zones agree."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _reading_zone() -> Any:
    """The zone the reader has configured, or None for the computer's own.

    IMPORTED HERE AND NOT AT THE TOP: `config` imports this module, so the
    dependency only runs one way at import time. It costs nothing per call —
    `load_config` caches on the file's stamp — and reading it live is what
    makes `collab config timezone` reach a viewer that is already open.
    """
    try:
        from .config import reading_timezone
        return reading_timezone()
    except ImportError:  # pragma: no cover - protocol must render regardless
        return None


def local_datetime(ts: str) -> datetime | None:
    """A wire timestamp as one aware datetime in the reader's own timezone.

    THE SINGLE PLACE THE CONVERSION HAPPENS. Everything a person reads beside a
    message — the clock, the day it belongs to, whether that day is today — has
    to come off this one datetime. When the clock was converted here and the
    date was sliced out of the raw UTC string instead, the two halves of one
    stamp described different days for anybody whose evening or morning falls
    on the far side of the UTC midnight.

    The zone is the computer's unless `collab config timezone` says otherwise;
    either way it is the same zone for both halves, which is the point.

    ``None`` when the stamp cannot be read at all; callers degrade to showing
    the raw characters rather than inventing a date.
    """
    if not ts:
        return None
    try:
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(_reading_zone())
    except (OverflowError, ValueError, OSError):
        # A stamp at the edge of the calendar: `astimezone` overflows when the
        # zone's offset would push it past year 1 or 9999. No wire stamp gets
        # near it; a hostile one can, and this runs on the draw path of a
        # curses program. Unreadable, like a stamp that does not parse.
        return None


def local_clock(ts: str, fmt: str = "%H:%M") -> str:
    """Render a wire timestamp in the reader's own timezone.

    UTC is right for the wire and wrong for a person reading a transcript on
    their own machine.
    """
    if not ts:
        return ""
    parsed = local_datetime(ts)
    if parsed is None:
        return ts[11:16] if len(ts) >= 16 else ts
    return parsed.strftime(fmt)


def local_today() -> date:
    """Today IN THE SAME ZONE the stamps are rendered in.

    `date.today()` is the computer's, and asking «is this message from today?»
    with the computer's answer while dating the message in a configured zone is
    the original bug again, one layer up: an hour either side of midnight the
    two disagree and a message gets labelled with the wrong day.
    """
    return datetime.now(_reading_zone()).date()


#: SPELLED OUT RATHER THAN ASKED OF strftime. `%b` is locale-dependent, so a
#: date beside a figure came out in whatever language the machine happened to
#: be set to — and two people comparing one `collab stats` output cannot have
#: half its dates in one language. The same table the transcript spells its
#: dates from.
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")


def local_day_clock(ts: str, *, today: date | None = None) -> str:
    """«15:22» when it is today, «30 aug 15:22» when it is not.

    The date appears only when it is needed: always showing it spends six
    columns on every row to say something the reader already knew. Judged on
    the reader's own calendar, like the clock — a stamp from 23:50 UTC is
    «today» or «yesterday» according to where the reader sits.
    """
    clock = local_clock(ts)
    if not clock:
        return ""
    # THE SAME CONVERSION THE TRANSCRIPT USES, and the same «today». This took
    # its clock from `local_datetime` and then built the date with a bare
    # `.astimezone()` — the machine's zone — and judged «today» by the
    # machine's calendar, so a reader who had pinned a zone with `collab
    # config timezone` saw the transcript honour it and the stats row ignore
    # it: one stamp, two days, an hour either side of midnight.
    parsed = local_datetime(ts)
    if parsed is None:
        return clock
    day = parsed.date()
    if day == (today if today is not None else local_today()):
        return clock
    return f"{day.day} {MONTHS[day.month - 1]} {clock}"


@dataclass
class Envelope:
    """One collab event.

    ``seq`` is assigned by the hub on append and is monotonic per session; it
    doubles as the SSE ``id:`` field, which is what makes gap-free resume work.
    """

    kind: str
    text: str = ""
    room: str | None = DEFAULT_ROOM
    to: str | None = None
    thread: str | None = None
    sender: str = ""
    body: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    ts: str = field(default_factory=now_iso)
    #: Routing identity. ``sender``/``to`` are display names and may change at
    #: any moment; delivery and visibility are decided on these instead.
    sender_id: str = ""
    to_id: str = ""
    #: Optional self-reported usage, piggybacked on any message. Riding along
    #: with normal traffic keeps it current without a separate heartbeat.
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "collab": EXTENSION_VERSION,
            "kind": self.kind,
            "from": self.sender,
            "ts": self.ts,
        }
        if self.text:
            d["text"] = self.text
        if self.room:
            d["room"] = self.room
        if self.to:
            d["to"] = self.to
        if self.thread:
            d["thread"] = self.thread
        if self.body:
            d["body"] = self.body
        if self.seq is not None:
            d["seq"] = self.seq
        if self.sender_id:
            d["fromId"] = self.sender_id
        if self.to_id:
            d["toId"] = self.to_id
        if self.stats:
            d["stats"] = self.stats
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Envelope:
        seq = d.get("seq")
        return cls(
            kind=str(d.get("kind") or KIND_CHAT),
            text=str(d.get("text") or ""),
            room=d.get("room") or None,
            to=d.get("to") or None,
            thread=d.get("thread") or None,
            sender=str(d.get("from") or ""),
            body=dict(d.get("body") or {}),
            # proto Struct coerces numbers to float; normalise back to int.
            seq=int(seq) if seq is not None else None,
            ts=str(d.get("ts") or now_iso()),
            sender_id=str(d.get("fromId") or ""),
            to_id=str(d.get("toId") or ""),
            stats=dict(d.get("stats") or {}),
        )

    def is_direct(self) -> bool:
        return bool(self.to)

    def render_line(self) -> str:
        """One-line rendering — this is what a Monitor turns into a notification.

        A direct message names its *recipient*, since the sender is already
        printed after the bracket; that reads correctly from both ends.

        Every field woven in here — the text, the sender, a task title, a file
        name — was chosen by another participant, and the result is printed
        straight to a terminal. So the whole line is scrubbed of control
        characters on the way out (see `scrub`): a name carrying an escape
        sequence must not get to rewrite the reader's screen.
        """
        return scrub(self._render_line())

    def _render_line(self) -> str:
        if self.kind == KIND_CHAT:
            where = f"dm→{self.to}" if self.is_direct() else f"#{self.room}"
            return f"[{where}] {self.sender}: {self.text}"
        if self.kind == KIND_HELLO:
            b = self.body
            bits = [x for x in (b.get("repo"), b.get("branch")) if x]
            ctx = f" ({', '.join(bits)})" if bits else ""
            focus = f" — {b['focus']}" if b.get("focus") else ""
            return f"[joined] {self.sender}{ctx}{focus}"
        if self.kind == KIND_FILE:
            b = self.body
            action = b.get("action", "shared")
            if action == "received":
                return f"[file] {self.sender} collected {b.get('name')} — {file_outcome(b)}"
            size = _human_size(int(b.get("size") or 0))
            to = f" → {self.to}" if self.to else ""
            return (f"[file{to}] {self.sender} shared {b.get('name')} ({size}) — "
                    f"fetch it with: collab file get {b.get('id')}")
        if self.kind == KIND_PRESENCE:
            return f"[presence] {self.sender}: {self.body.get('event', '')}"
        if self.kind == KIND_ACTIVITY:
            from .activity import describe

            said = describe(self.body) or "idle"
            return f"[{self.body.get('state', 'activity')}] {self.sender}: {said}"
        if self.kind == KIND_TASK:
            b = self.body
            state = short_state(b.get("state", ""))
            owner = f" ({b['owner']})" if b.get("owner") else ""
            return (
                f"[task {b.get('id', '?')}] {self.sender} {b.get('action', '')}: "
                f"{b.get('title') or self.text} [{state}]{owner}"
            )
        return f"[{self.kind}] {self.text or self.body}"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} MB"
