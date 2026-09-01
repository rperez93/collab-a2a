"""The local inbox the daemon writes and the agent reads.

Two consumers are served from one write: a JSONL file that ``collab listen
--follow`` tails (what a Monitor watches), and a SQLite table that gives
``collab recv`` a durable cursor and remembers the last ``seq`` for resume.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from ..protocol import Envelope

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    seq     INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    sender  TEXT NOT NULL,
    payload TEXT NOT NULL,
    read    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

#: How long a write here may wait for the database, and no longer.
#:
#: `record` is a synchronous sqlite call made from inside the daemon's `async
#: for` over the feed, so a busy wait does not block one write — it blocks the
#: heartbeat, the bridge and the feed together, for as long as it lasts.
#: Python's default is five seconds, which is enough to take a local lock past
#: READ_TIMEOUT and turn it into a reconnect. Sat well under STATUS_HEARTBEAT,
#: so a wait that does happen costs at most one beat.
BUSY_TIMEOUT_MS = 1000


class Inbox:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "inbox.jsonl"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.dir / "inbox.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # WAL, because the readers are the point: `collab recv`, the
            # viewer and the status line all read this file while the daemon
            # writes it, and under the rollback journal a reader holding a
            # snapshot locks the daemon out of its own inbox. WAL lets them
            # miss each other entirely. It is set every time rather than once:
            # the mode lives in the file, and a database restored from a copy
            # or created by an older collab arrives without it.
            with contextlib.suppress(sqlite3.DatabaseError):
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, env: Envelope) -> bool:
        """Store one event.  Returns False if this seq was already stored.

        Replay after a reconnect can legitimately resend an event we already
        have; the primary key makes that a no-op rather than a duplicate
        notification.
        """
        if env.seq is None:
            return False
        with self._lock:
            existing = self._db.execute(
                "SELECT 1 FROM inbox WHERE seq=?", (env.seq,)
            ).fetchone()
            if existing:
                return False
            self._db.execute(
                "INSERT INTO inbox (seq, ts, kind, sender, payload) VALUES (?,?,?,?,?)",
                (env.seq, env.ts, env.kind, env.sender, json.dumps(env.to_dict())),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seq', ?)",
                (str(env.seq),),
            )
            self._db.commit()
        # The JSONL append is what a `collab listen --follow` tail sees.
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(env.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
        return True

    def last_seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key='last_seq'").fetchone()
        return int(row["value"]) if row else 0

    def unread_count(self, *, exclude_sender: str | None = None,
                     kinds: tuple[str, ...] = ()) -> int:
        """How many messages are waiting for you.

        Your own messages come back down the feed (that is what keeps every
        participant's log identical), but counting them as unread would show a
        badge for talking to yourself.

        ``kinds`` narrows it to what somebody actually SAID. An arrival, a
        rename or a file notice is an event, not something anybody has to act
        on, and counting them told an agent it had «1 unread — nobody has acted
        on them» because a colleague had walked in.
        """
        sql = "SELECT COUNT(*) AS c FROM inbox WHERE read=0"
        args: list[Any] = []
        if exclude_sender:
            sql += " AND sender <> ?"
            args.append(exclude_sender)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        with self._lock:
            row = self._db.execute(sql, tuple(args)).fetchone()
        return int(row["c"])

    def take_unread(self, limit: int = 100, *, mark: bool = True) -> list[Envelope]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, payload FROM inbox WHERE read=0 ORDER BY seq LIMIT ?", (limit,)
            ).fetchall()
            if mark and rows:
                self._db.executemany(
                    "UPDATE inbox SET read=1 WHERE seq=?", [(r["seq"],) for r in rows]
                )
                self._db.commit()
        return [Envelope.from_dict(json.loads(r["payload"])) for r in rows]

    def all_events(self, limit: int = 100) -> list[Envelope]:
        """The last ``limit`` events, or every one of them when it is 0.

        SQLite reads a negative LIMIT as no limit at all, which is how «show me
        the whole conversation» is said without building the query twice.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM inbox ORDER BY seq DESC LIMIT ?",
                (limit if limit > 0 else -1,),
            ).fetchall()
        return [Envelope.from_dict(json.loads(r["payload"])) for r in reversed(rows)]

    def before(self, seq: int, limit: int = 200) -> list[Envelope]:
        """The ``limit`` events immediately before ``seq``, oldest first.

        What the viewer reaches for when somebody scrolls off the top of what
        it opened with: the log is complete on disk, so running out of screen
        is not the same as running out of conversation.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM inbox WHERE seq < ? ORDER BY seq DESC LIMIT ?",
                (seq, limit if limit > 0 else -1),
            ).fetchall()
        return [Envelope.from_dict(json.loads(r["payload"])) for r in reversed(rows)]

    def after(self, seq: int, limit: int = 200) -> list[Envelope]:
        """The ``limit`` events immediately after ``seq``, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM inbox WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit if limit > 0 else -1),
            ).fetchall()
        return [Envelope.from_dict(json.loads(r["payload"])) for r in rows]

    def first(self, limit: int = 50) -> list[Envelope]:
        """The oldest ``limit`` events: the beginning of the conversation."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM inbox ORDER BY seq LIMIT ?",
                (limit if limit > 0 else -1,),
            ).fetchall()
        return [Envelope.from_dict(json.loads(r["payload"])) for r in rows]

    def count_after(self, seq: int) -> int:
        """How many events are newer than ``seq``.

        A count and not a fetch: the viewer holds a window of the conversation
        and still has to say how much is below it, which is the number and not
        the messages.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS c FROM inbox WHERE seq > ?", (seq,)
            ).fetchone()
        return int(row["c"])

    def has_before(self, seq: int) -> bool:
        """Is there anything older than ``seq`` in here?

        Asked rather than inferred from the seq itself: the numbers are the
        hub's, and a participant never receives what was said in other people's
        direct messages, so «my oldest is 12» does not mean eleven are missing.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM inbox WHERE seq < ? LIMIT 1", (seq,)
            ).fetchone()
        return row is not None

    def gaps(self) -> list[int]:
        """Missing seq values — used by the tests to prove nothing was dropped."""
        with self._lock:
            rows = self._db.execute("SELECT seq FROM inbox ORDER BY seq").fetchall()
        seqs = [r["seq"] for r in rows]
        if not seqs:
            return []
        return [n for n in range(seqs[0], seqs[-1] + 1) if n not in set(seqs)]
