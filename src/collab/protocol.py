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
from datetime import datetime, timezone
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

ALL_KINDS = frozenset({KIND_CHAT, KIND_TASK, KIND_HELLO, KIND_PRESENCE,
                       KIND_FILE, KIND_SYSTEM})

#: Files are shared out of band rather than pasted as text, so binaries and
#: build artifacts never have to be squeezed through a chat message.
MAX_FILE_BYTES = 10 * 1024 * 1024
FILE_TTL_SECONDS = 24 * 3600


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
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    """Timestamps travel in UTC so participants in different zones agree."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_clock(ts: str, fmt: str = "%H:%M") -> str:
    """Render a wire timestamp in the reader's own timezone.

    UTC is right for the wire and wrong for a person reading a transcript on
    their own machine.
    """
    if not ts:
        return ""
    try:
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts[11:16] if len(ts) >= 16 else ts
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime(fmt)


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
                return f"[file] {self.sender} confirmed receipt of {b.get('name')} — deleted"
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
            state = str(b.get("state", "")).replace("TASK_STATE_", "").lower()
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
