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
        with self._lock:
            self._db = self._connect()

    def _connect(self) -> sqlite3.Connection:
        """Open the inbox, set up the way every connection to it must be.

        In one place because there are two reasons to open it: starting up,
        and replacing a connection whose transaction could not be rolled back.
        """
        db = sqlite3.connect(self.dir / "inbox.db", check_same_thread=False)
        db.row_factory = sqlite3.Row
        # WAL, because the readers are the point: `collab recv`, the viewer and
        # the status line all read this file while the daemon writes it, and
        # under the rollback journal a reader holding a snapshot locks the
        # daemon out of its own inbox. WAL lets them miss each other entirely.
        # It is set every time rather than once: the mode lives in the file,
        # and a database restored from a copy or created by an older collab
        # arrives without it.
        with contextlib.suppress(sqlite3.DatabaseError):
            db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        db.executescript(SCHEMA)
        db.commit()
        return db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, env: Envelope) -> bool:
        """Store one event.  Returns False if this seq was already stored.

        Replay after a reconnect can legitimately resend an event we already
        have; the primary key makes that a no-op rather than a duplicate
        notification.

        THE JSONL IS WRITTEN BEFORE THE DATABASE COMMITS, and the order is not
        an accident — do not tidy it back. Resume asks the database for
        `last_seq`, so an event committed there is an event that will never be
        fetched again; if the process died between the commit and the append,
        the line stream that `collab listen --follow` tails —which is the
        arrangement every skill here prescribes— lost that message silently and
        for good, while `collab recv` still had it and the two views disagreed
        with nothing to say so.

        Written this way round the same crash costs a duplicate line instead,
        because resume re-delivers an event the log already has. That is the
        direction this codebase already argues for out loud in `wake`: an agent
        that was briefly broken should be told twice rather than not at all.
        """
        if env.seq is None:
            return False
        with self._lock:
            existing = self._db.execute(
                "SELECT 1 FROM inbox WHERE seq=?", (env.seq,)
            ).fetchone()
            if existing:
                return False
            try:
                self._db.execute(
                    "INSERT INTO inbox (seq, ts, kind, sender, payload) VALUES (?,?,?,?,?)",
                    (env.seq, env.ts, env.kind, env.sender, json.dumps(env.to_dict())),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seq', ?)",
                    (str(env.seq),),
                )
                # The append goes here, inside the lock and BEFORE the commit:
                # the commit is the moment the event becomes one we will never
                # ask for again, so nothing may become unfetchable while the log
                # a Monitor tails does not have it yet. Inside the lock because
                # two writers appending at once interleave their lines.
                with self.jsonl.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(env.to_dict(), ensure_ascii=False) + "\n")
                    fh.flush()
                self._db.commit()
            except sqlite3.IntegrityError:
                # SOMEBODY ELSE GOT THERE BETWEEN THE LOOK AND THE LEAP. The
                # primary key is doing its job and the answer is the same one
                # the SELECT above would have given: we already have this
                # event. Uncaught it reached `_connect_forever`'s general
                # handler, where it was logged as a dropped feed and counted as
                # a failure — and after eight of those the hint tells a guest
                # the hub is unreachable and to go and ask a human for a fresh
                # link, over a race that never left this machine.
                #
                # Through `_discard` for the same reason as the arm below: a
                # bare rollback that fails leaves the transaction open AND
                # turns this answer back into a raised exception, which lands
                # on the very hint this path was written to stop — undone by
                # its own error handler. A constraint-aborted INSERT leaves
                # nothing pending, so there is no orphan to commit here and
                # nothing is lost; what is at stake is the misdiagnosis and a
                # read transaction left pinning the WAL against checkpointing.
                self._discard()
                return False
            except BaseException:
                # AND THE APPEND CAN FAIL, not merely be interrupted — a
                # read-only state directory or a full disk raises right here.
                # Without this the write transaction escaped still open, and
                # everything after it read through the transaction rather than
                # the database: `last_seq` answered with a seq that had not
                # been committed, so the resume skipped that event, and the
                # next successful record committed the orphaned row alongside
                # its own. The seq then existed in the database, was absent
                # from the log, and could never be fetched again — the exact
                # divergence the ordering above exists to prevent, moved one
                # step along. Anything that leaves this block leaves it clean,
                # which `_discard` is responsible for even when the rollback
                # itself is what fails.
                #
                # BaseException rather than Exception, and wider than today's
                # callers need on purpose: `record` is synchronous with no
                # await and no yield, so CancelledError and GeneratorExit
                # cannot reach it as things stand. Moving this write off the
                # event loop is the next change this file is likely to see, and
                # it opens that path the moment it lands — a handler that holds
                # only while nobody threads the caller is a trap laid for
                # whoever does.
                self._discard()
                raise
        return True

    def _discard(self) -> None:
        """Undo the half-written event, even when the rollback itself fails.

        A rollback that raises leaves the transaction OPEN, and everything
        after it reads through the transaction rather than through the
        database: `last_seq` answers with the uncommitted seq, the next
        `record` finds its own row and reports that we already have it, and
        the first write that does succeed commits the orphan behind it.
        Measured with both failing, the database ended [1, 50, 51] and the log
        [1, 51] — the divergence this ordering exists to prevent, arriving
        through the handler written for it.

        So the connection is closed, which discards the transaction whatever
        state it is in, and a fresh one is opened in its place. A new
        connection starts clean and reads only what was committed, which is
        the consistent state.

        Reopening rather than staying closed, because a transient fault must
        not become a permanent one: this object is built once and never
        rebuilt, so a closed connection would deafen the daemon for the rest
        of its life over one bad moment. Nothing is masked by recovering —
        a fault that persists, a full disk or a read-only directory, fails the
        append again immediately and is just as loud through that path.

        And if the reopen fails too, the closed connection stays: every later
        call then raises, the daemon reports a dropped feed and retries, and
        it is loud and stuck rather than quiet and wrong. Being stuck is the
        intended outcome there and not an oversight — a daemon nobody can hear
        from gets looked at; a log missing one message does not.

        Every failure in here is suppressed because the caller re-raises the
        ORIGINAL one, and that is the useful one: a full disk or a read-only
        directory is what a person can act on. Neither the rollback's
        complaint nor the reopen's may take its place.

        WHAT IS NOT KNOWN is how this is reached. The obvious theory —that the
        append and the rollback share a cause— was tested and did not hold: the
        rollback succeeded through a read-only directory, read-only `-wal` and
        `-shm`, unlinked `-wal` and `-shm`, and a read-only `inbox.db`, because
        a WAL rollback discards frames rather than writing them. It could only
        be made to fail by injection. Genuine ENOSPC and a hardware EIO remain
        the candidates and neither is stageable without root, so this is three
        lines against a consequence that is silent, not against a rate anybody
        has measured. Do not quote a likelihood for it; there is not one.
        """
        try:
            self._db.rollback()
        except BaseException:
            with contextlib.suppress(BaseException):
                self._db.close()
            with contextlib.suppress(BaseException):
                self._db = self._connect()

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
