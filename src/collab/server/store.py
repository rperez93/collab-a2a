"""Durable session state.

One append-only ``events`` table is the backbone: ``seq`` is the primary key,
it is handed out on append, and it doubles as the SSE ``id:``.  Resume after a
disconnect, ``/history`` backfill, and surviving a hub restart all fall out of
that single design.

**Identity is an id, never a display name.**  Names are what people see and
they change; routing a message or a permission check on one breaks the instant
someone renames themselves.  Every participant gets a stable ``p_...`` id, and
``participant_names`` remembers every name they have ever held, so a reference
someone still holds to an old name resolves to the right person.

Everything here is synchronous sqlite3 called through ``asyncio.to_thread`` by
the callers, so we take no async-driver dependency.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..protocol import Envelope

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    room         TEXT,
    sender       TEXT NOT NULL,
    recipient    TEXT,
    sender_id    TEXT,
    recipient_id TEXT,
    ts           TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_room ON events(room, seq);

CREATE TABLE IF NOT EXISTS participants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    is_host    INTEGER NOT NULL DEFAULT 0,
    joined_at  REAL NOT NULL,
    last_seen  REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS participant_names (
    name           TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    claimed_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL,
    max_uses   INTEGER NOT NULL DEFAULT 0,
    uses       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rooms (
    name       TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS files (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    sender     TEXT NOT NULL,
    recipient  TEXT,
    room       TEXT,
    created_at REAL NOT NULL,
    acked_at   REAL,
    acked_by   TEXT,
    state      TEXT NOT NULL DEFAULT 'available'
);

-- WHO A ROOM FILE IS WAITING FOR. A file addressed to one person has one
-- collector and `files.acked_by` is enough; a file shared with a room has as
-- many as were in the session when it was sent, and the blob stays until each
-- of them has it. One row per awaited participant, written at send time with
-- `collected_at` NULL and filled in by their ack — so one who acks twice
-- changes nothing. A participant who joins afterwards may still fetch it and
-- gets a row when they do, marked `awaited` 0: the history says they have it,
-- and they hold nothing up and complete nothing.
CREATE TABLE IF NOT EXISTS file_collections (
    file_id        TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    collected_at   REAL,
    awaited        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (file_id, participant_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    state      TEXT NOT NULL,
    owner      TEXT,
    room       TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    batch      TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    state     TEXT NOT NULL DEFAULT 'open',
    opened_by TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL
);
-- ONE OPEN BATCH, ENFORCED HERE AND NOT ONLY CHECKED ABOVE. Two agents can
-- open a batch in the same instant, and a read-then-insert would let both
-- through: from then on each new task joins one denominator or the other,
-- while both agents believe they are watching one shared figure.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_batch
    ON batches(state) WHERE state = 'open';
"""
# The index on tasks.batch is NOT here. This script runs before the migration,
# and on a database written before batches existed the column does not exist
# yet — `CREATE INDEX` on a missing column raises and takes the whole hub down
# on start-up. It is created in `_migrate`, once the column is certain.


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_participant_id() -> str:
    return "p_" + uuid.uuid4().hex[:12]


@dataclass
class Participant:
    id: str
    name: str
    is_host: bool
    joined_at: float
    last_seen: float
    revoked: bool
    meta: dict[str, Any]


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # WAL before the migration, not after: the back-fills below are DML,
            # sqlite3 opens an implicit transaction for them, and journal_mode
            # cannot change inside one.  Run after, the pragma quietly does
            # nothing and every migrated session stays on the rollback journal.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self._db.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Bring a database written by an older collab up to the current shape.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as it
        was, so a session recorded before identity moved from names to ids has
        no ``id`` column and every read of it fails. Sessions are meant to be
        resumable — a conversation from last month should still open — so the
        missing pieces are added and backfilled rather than the session being
        written off.
        """
        now = time.time()

        # Identity used to be the display name. Give everyone an id, and record
        # the name they hold so references to it still resolve.
        people = self._columns("participants")
        if people and "id" not in people:
            self._db.execute("ALTER TABLE participants ADD COLUMN id TEXT")
            for row in self._db.execute("SELECT name FROM participants").fetchall():
                pid = new_participant_id()
                self._db.execute(
                    "UPDATE participants SET id=? WHERE name=?", (pid, row["name"]))
                self._db.execute(
                    "INSERT OR REPLACE INTO participant_names"
                    " (name, participant_id, claimed_at) VALUES (?,?,?)",
                    (row["name"], pid, now),
                )

        # A name freed by `collab kick` was still held by the revoked row, and
        # `participants.name` is UNIQUE — so rejoining under a name you had
        # used before raised IntegrityError inside the request and reached the
        # agent as a bare HTTP 500. Sessions already carrying those rows would
        # keep refusing those names for as long as they live, so the names are
        # released here rather than only for sessions started from now on.
        if self._columns("participants"):
            freed = self._db.execute(
                "SELECT id, name FROM participants WHERE revoked = 1"
                " AND name NOT LIKE '%~%'"
            ).fetchall()
            for row in freed:
                self._db.execute(
                    "UPDATE participants SET name = ? WHERE id = ?",
                    (f"{row['name']}~{str(row['id'])[2:10]}", row["id"]),
                )

        # Events carried names only. Resolve them to the ids just assigned, so
        # old direct messages stay visible to exactly the two people involved.
        events = self._columns("events")
        if events:
            added = [col for col in ("sender_id", "recipient_id")
                     if col not in events]
            for col in added:
                self._db.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
            if added:
                self._db.execute(
                    "UPDATE events SET sender_id = (SELECT participant_id FROM"
                    " participant_names WHERE name = events.sender)"
                    " WHERE sender_id IS NULL")
                self._db.execute(
                    "UPDATE events SET recipient_id = (SELECT participant_id FROM"
                    " participant_names WHERE name = events.recipient)"
                    " WHERE recipient_id IS NULL AND recipient IS NOT NULL")

        # A batch is a denominator: which tasks it holds is the whole of what
        # is being counted. A session recorded before batches existed has a
        # `tasks` table with no such column, and every task read from it —
        # `collab task list`, the roster snapshot, the hub's own start-up —
        # fails on the missing name rather than on anything the user did.
        #
        # Left NULL rather than back-filled into some invented batch. Those
        # tasks predate every batch there is, and putting them in one would
        # invent a denominator nobody agreed to and report a percentage for
        # work that was never scoped as a batch.
        tasks = self._columns("tasks")
        if tasks and "batch" not in tasks:
            self._db.execute("ALTER TABLE tasks ADD COLUMN batch TEXT")
        if self._columns("tasks"):
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch)")

        # HOW MANY THINGS HAVE BEEN SAID is one kind out of the fattest table
        # in the schema, and `events` has no index on `kind`. Without one the
        # count is a scan of every row — payloads included, because the rows
        # live in the table b-tree — and it is read on the snapshot path, which
        # every join and every client's refresh goes through, under the lock
        # that every append wants.
        #
        # Measured on this machine over events sized as this hub writes them,
        # six chat to four of everything else:
        #
        #     10k events (4.3 MB):    1.08 ms median, 1.9 ms worst
        #                   indexed:  0.18 ms median, 0.3 ms worst  (+0.1 MB)
        #     100k events (43 MB):   10.57 ms median, 20.8 ms worst
        #                   indexed:  1.08 ms median, 3.2 ms worst  (+1.4 MB)
        #
        # A tenth of the time for three per cent of the file, and that is with
        # a warm page cache — the scan is 43 MB of reads that a cold hub pays
        # in full. The write side is one short-TEXT b-tree insert per appended
        # event, against a row that already carries its whole payload.
        #
        # In `_migrate` rather than in SCHEMA so that a session recorded before
        # this existed gains it on its next open, like `idx_tasks_batch` above.
        if self._columns("events"):
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")

        # A file used to be collected ONCE: the first ack, whoever it came
        # from, unlinked the blob and wrote the collector into `files.acked_by`.
        # Collections now live in their own table, one row per awaited
        # participant, and every reader of «who has this» reads that table —
        # so the one collector an older session recorded is folded into it,
        # or the history would say nobody ever collected those files.
        #
        # The files still WAITING in an older session get no rows. Their
        # audience was never written down and cannot be reconstructed
        # honestly — whoever is in the session now is not who was there when
        # the file was sent — so they are served to anyone in the room and end
        # on their clock or by withdrawal, never on somebody's ack. See
        # `file_progress` for what an empty audience means.
        if self._columns("files"):
            self._db.execute(
                "INSERT OR IGNORE INTO file_collections"
                " (file_id, participant_id, collected_at)"
                " SELECT f.id, n.participant_id, f.acked_at"
                " FROM files f JOIN participant_names n ON n.name = f.acked_by"
                " WHERE f.acked_by IS NOT NULL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- events --------------------------------------------------------------

    def append(self, env: Envelope) -> Envelope:
        """Persist an event and stamp it with its ``seq``.

        Called before fan-out, so a message can never be delivered with a seq
        that isn't already durable.
        """
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (kind, room, sender, recipient, sender_id,"
                " recipient_id, ts, payload) VALUES (?,?,?,?,?,?,?,?)",
                (env.kind, env.room, env.sender, env.to,
                 env.sender_id, env.to_id, env.ts, ""),
            )
            env.seq = int(cur.lastrowid)
            self._db.execute(
                "UPDATE events SET payload=? WHERE seq=?",
                (json.dumps(env.to_dict()), env.seq),
            )
            self._db.commit()
        return env

    def since(self, seq: int, *, viewer: str | None = None, limit: int = 500) -> list[Envelope]:
        """Events after ``seq`` that ``viewer`` (a participant id) may see."""
        return self.since_page(seq, viewer=viewer, limit=limit)[0]

    def since_page(self, seq: int, *, viewer: str | None = None,
                   limit: int = 500) -> tuple[list[Envelope], int]:
        """One page of ``since``, plus how far the read actually got.

        The cursor is the last seq READ, not the last one returned. They differ
        whenever a page is filtered — a run of somebody else's direct messages
        comes back empty — and paging by what was returned would then ask for
        the same page for ever, or stop early and leave a hole.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, payload, recipient_id, sender_id FROM events WHERE seq > ?"
                " ORDER BY seq LIMIT ?",
                (seq, limit),
            ).fetchall()
        out = []
        for r in rows:
            if not _visible_to(r["recipient_id"], r["sender_id"], viewer):
                continue
            out.append(Envelope.from_dict(json.loads(r["payload"])))
        return out, int(rows[-1]["seq"]) if rows else seq

    def history(self, *, room: str | None = None, viewer: str | None = None,
                limit: int = 50) -> list[Envelope]:
        sql = "SELECT payload, recipient_id, sender_id FROM events"
        args: list[Any] = []
        if room:
            sql += " WHERE room = ?"
            args.append(room)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit * 3)  # over-fetch, then filter for visibility
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            if not _visible_to(r["recipient_id"], r["sender_id"], viewer):
                continue
            out.append(Envelope.from_dict(json.loads(r["payload"])))
            if len(out) >= limit:
                break
        return list(reversed(out))

    def max_seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        return int(row["m"])

    def count_kind(self, kind: str) -> int:
        """How many events of ONE kind the session holds.

        `max_seq` above is every kind there is — joins, presence beats, task
        moves, file transfers and chat, all sequenced through one log — so it
        is the session's clock and not a count of anything in particular. A
        figure offered to a reader as «messages» has to mean the thing a person
        said, or it repeats the confusion between activity and conversation
        that the unread count had to be split in two to fix.

        Unfiltered by viewer, deliberately. `history` and `since_page` hide a
        direct message from everybody but its two ends, and a count that did
        the same would be a different number for each reader — which is the one
        thing a session-wide figure may not be. It says how much has been said
        in here, not how much of it you were shown.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind = ?", (kind,),
            ).fetchone()
        return int(row["n"])

    # --- participants --------------------------------------------------------

    def _retire_name(self, name: str) -> None:
        """Take a freed name off a participant who no longer holds it.

        `participants.name` is UNIQUE, and a revoked row keeps occupying its
        name. Every rule above this says such a name is available again — the
        join check, the suffixing loop, the documentation — so the row is the
        only thing that disagrees, and it disagrees by raising IntegrityError
        from inside a request, which reaches the agent as a bare HTTP 500.

        The retired name keeps the original plus the participant's own id, so
        the roster history stays readable and no two retirements collide.
        """
        self._db.execute(
            "UPDATE participants SET name = name || '~' || substr(id, 3, 8)"
            " WHERE name = ? AND revoked = 1",
            (name,),
        )


    def add_participant(self, name: str, token: str, *, is_host: bool = False,
                        meta: dict[str, Any] | None = None) -> Participant:
        """Insert a participant, suffixing the name if it is already taken."""
        now = time.time()
        pid = new_participant_id()
        with self._lock:
            final = name
            n = 2
            # Callers reject a name a live participant holds, so this loop only
            # guards the table's UNIQUE constraint. A name left behind by
            # someone who renamed away is free to claim.
            while self._db.execute(
                "SELECT 1 FROM participants WHERE name=? AND revoked=0", (final,)
            ).fetchone():
                final = f"{name}-{n}"
                n += 1
            # Whoever was removed from it does not hold it any more.
            self._retire_name(final)
            try:
                self._db.execute(
                    "INSERT INTO participants (id, name, token_hash, is_host,"
                    " joined_at, last_seen, meta) VALUES (?,?,?,?,?,?,?)",
                    (pid, final, token_hash(token), int(is_host), now, now,
                     json.dumps(meta or {})),
                )
            except sqlite3.IntegrityError:
                # Whatever we did not foresee, a join must not end as a 500.
                # A suffixed name is a small surprise; a stack trace is not.
                final = f"{name}-{pid[2:8]}"
                self._db.execute(
                    "INSERT INTO participants (id, name, token_hash, is_host,"
                    " joined_at, last_seen, meta) VALUES (?,?,?,?,?,?,?)",
                    (pid, final, token_hash(token), int(is_host), now, now,
                     json.dumps(meta or {})),
                )
            self._db.execute(
                "INSERT OR REPLACE INTO participant_names (name, participant_id,"
                " claimed_at) VALUES (?,?,?)",
                (final, pid, now),
            )
            self._db.commit()
        return Participant(id=pid, name=final, is_host=is_host, joined_at=now,
                           last_seen=now, revoked=False, meta=dict(meta or {}))

    def rebind_participant(self, participant_id: str, token: str, *,
                           meta: dict[str, Any] | None = None) -> Participant | None:
        """Hand an existing participant back to whoever is answering to it now.

        A rejoin, not a new arrival: the row keeps its id, so the history
        addressed to it, the direct messages, the colour and the usage figures
        all still belong to the same person. Only the token changes — the old
        one stops working, which is what makes this a hand-over rather than a
        second key cut for the same door.
        """
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM participants WHERE id=? AND revoked=0",
                (participant_id,),
            ).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE participants SET token_hash=?, last_seen=? WHERE id=?",
                (token_hash(token), now, participant_id),
            )
            self._db.commit()
        person = self.participant_by_id(participant_id)
        if person is not None and meta:
            # Merged, not replaced: what they say on the way back in is newer,
            # and what they said before and did not repeat is still true.
            merged = dict(person.meta)
            merged.update({k: v for k, v in meta.items() if v not in ("", None)})
            self.update_meta(participant_id, merged)
            person = self.participant_by_id(participant_id)
        return person

    def name_taken(self, name: str, *, except_id: str = "") -> bool:
        """Is this name currently held by somebody still in the session?

        Only *current* names count: a name freed by a rename, or belonging to
        someone who was removed, is available again.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM participants WHERE name=? AND revoked=0", (name,)
            ).fetchone()
        return bool(row) and row["id"] != except_id

    def resolve_name(self, name: str) -> str | None:
        """Find a participant id from a name, current or historical.

        Whoever holds the name *now* wins: if somebody renamed away and a new
        arrival took the name, that name means the new arrival. Only when no
        one currently holds it does it fall back to the last person who did,
        which is what keeps a stale reference from before a rename working.
        """
        if not name:
            return None
        with self._lock:
            current = self._db.execute(
                "SELECT id FROM participants WHERE name=? AND revoked=0", (name,)
            ).fetchone()
            if current:
                return str(current["id"])
            historical = self._db.execute(
                "SELECT participant_id FROM participant_names WHERE name=?", (name,)
            ).fetchone()
        return str(historical["participant_id"]) if historical else None

    def participant_by_id(self, participant_id: str) -> Participant | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
        return _to_participant(row) if row else None

    def participant_for_token(self, token: str) -> Participant | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM participants WHERE token_hash=?", (token_hash(token),)
            ).fetchone()
        if row is None or row["revoked"]:
            return None
        return _to_participant(row)

    def participants(self, *, include_revoked: bool = False) -> list[Participant]:
        sql = "SELECT * FROM participants"
        if not include_revoked:
            sql += " WHERE revoked=0"
        sql += " ORDER BY joined_at"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        return [_to_participant(r) for r in rows]

    def touch(self, participant_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE participants SET last_seen=? WHERE id=?",
                (time.time(), participant_id),
            )
            self._db.commit()

    def update_meta(self, participant_id: str, meta: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE participants SET meta=? WHERE id=?",
                (json.dumps(meta), participant_id),
            )
            self._db.commit()

    def revoke(self, participant_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE participants SET revoked=1 WHERE id=? AND is_host=0",
                (participant_id,),
            )
            self._db.commit()
        return cur.rowcount > 0

    def rename(self, participant_id: str, new: str) -> str:
        """Change the display name. The id — and so all routing — is untouched."""
        now = time.time()
        with self._lock:
            final, n = new, 2
            while self._db.execute(
                "SELECT 1 FROM participants WHERE name=? AND id<>? AND revoked=0",
                (final, participant_id),
            ).fetchone():
                final = f"{new}-{n}"
                n += 1
            # The same freed-name hazard as joining: a revoked row still
            # occupies the name, and this column is UNIQUE.
            self._retire_name(final)
            self._db.execute(
                "UPDATE participants SET name=? WHERE id=?", (final, participant_id)
            )
            # Keep the old name pointing here so references to it still resolve.
            self._db.execute(
                "INSERT OR REPLACE INTO participant_names (name, participant_id, claimed_at)"
                " VALUES (?,?,?)",
                (final, participant_id, now),
            )
            self._db.commit()
        return final

    # --- invites -------------------------------------------------------------

    def add_invite(self, code: str, *, ttl_seconds: float | None = None,
                   max_uses: int = 0) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO invites (code_hash, created_at, expires_at, max_uses, uses)"
                " VALUES (?,?,?,?,0)",
                (token_hash(code), now, (now + ttl_seconds) if ttl_seconds else None, max_uses),
            )
            self._db.commit()

    def clear_invites(self) -> int:
        """Retire every invite issued so far.

        Used when a session is resumed: the conversation carries over, the way
        in does not. An old link should not still open the door.
        """
        with self._lock:
            cur = self._db.execute("DELETE FROM invites")
            self._db.commit()
        return cur.rowcount

    def replace_invite(self, code: str, *, ttl_seconds: float | None = None,
                       max_uses: int = 0) -> int:
        """Retire every invite and mint this one, as ONE act.

        Clearing and adding as two statements leaves a moment with no way in at
        all, and lets two rotations racing each other end with two live rows —
        which is the opposite of what rotating is for. One transaction, and the
        write lock taken before the delete rather than at it, so a second
        rotation waits its turn instead of interleaving with this one.

        Returns how many invites were retired.
        """
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cleared = self._db.execute("DELETE FROM invites").rowcount
                self._db.execute(
                    "INSERT INTO invites"
                    " (code_hash, created_at, expires_at, max_uses, uses)"
                    " VALUES (?,?,?,?,0)",
                    (token_hash(code), now,
                     (now + ttl_seconds) if ttl_seconds else None, max_uses),
                )
                self._db.commit()
            finally:
                if self._db.in_transaction:
                    self._db.rollback()
        return cleared

    def consume_invite(self, code: str) -> tuple[bool, str]:
        """Validate and spend one use.  Returns ``(ok, reason)``.

        THE SPEND IS WHAT DECIDES, not the read before it. `self._lock` orders
        threads on one connection and nothing at all between processes, and
        rotation now runs in a process of its own — so between the SELECT and
        the UPDATE a host can retire this very invite, the UPDATE then matches
        no row, and returning success on the strength of the earlier read lets
        somebody in through a door the host has just locked. Taking the write
        lock first and believing the row count instead closes it.
        """
        with self._lock:
            # BEGIN IMMEDIATE takes the write lock now rather than at the first
            # write, so a rotation on another connection waits behind this
            # rather than landing in the middle of it.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM invites WHERE code_hash=?", (token_hash(code),)
                ).fetchone()
                if row is None:
                    return False, "unknown invite code"
                if row["expires_at"] is not None and time.time() > row["expires_at"]:
                    return False, "invite expired"
                if row["max_uses"] and row["uses"] >= row["max_uses"]:
                    return False, "invite already used the maximum number of times"
                spent = self._db.execute(
                    "UPDATE invites SET uses=uses+1 WHERE code_hash=?",
                    (token_hash(code),),
                )
                if spent.rowcount != 1:
                    # Retired between the read and the write. It is the same
                    # answer an unknown code gets, because that is what it now
                    # is.
                    return False, "unknown invite code"
                self._db.commit()
            finally:
                if self._db.in_transaction:
                    self._db.rollback()
        return True, ""

    # --- rooms ---------------------------------------------------------------

    def add_room(self, name: str, created_by: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO rooms (name, created_at, created_by) VALUES (?,?,?)",
                (name, time.time(), created_by),
            )
            self._db.commit()

    def rooms(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT name FROM rooms ORDER BY created_at").fetchall()
        return [r["name"] for r in rows]

    # --- shared task board ----------------------------------------------------

    def upsert_task(self, task_id: str, *, title: str, state: str, owner: str | None,
                    room: str | None, created_by: str, detail: str = "",
                    join_open_batch: bool = False) -> dict[str, Any]:
        """Create or update one task.

        `join_open_batch` resolves the open batch HERE, inside the same lock as
        the insert, rather than being handed an id the caller read earlier. The
        caller read it, awaited, and then wrote — and an `await` is a yield
        point, so a close landing in that window put a task into a batch that
        had already closed, or into none at all. Which tasks a batch holds is
        the denominator everybody is watching; it cannot be decided by whether
        two requests interleaved.
        """
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if existing is None:
                batch = None
                if join_open_batch:
                    open_now = self._db.execute(
                        "SELECT id FROM batches WHERE state='open'"
                        " ORDER BY opened_at DESC"
                    ).fetchone()
                    batch = str(open_now["id"]) if open_now else None
                self._db.execute(
                    "INSERT INTO tasks (id,title,state,owner,room,created_by,"
                    "created_at,updated_at,detail,batch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, title, state, owner, room, created_by, now, now,
                     detail, batch),
                )
            else:
                # `batch` is deliberately absent from this list. Which batch a
                # task belongs to is settled when it is proposed and never
                # again: a task that could move between batches would move the
                # denominator of two of them at once, and the figure everybody
                # is looking at would change for reasons nobody performed.
                self._db.execute(
                    "UPDATE tasks SET title=?, state=?, owner=?, updated_at=?, detail=? WHERE id=?",
                    (title or existing["title"], state, owner, now,
                     detail or existing["detail"], task_id),
                )
            self._db.commit()
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        if open_only:
            sql += (" WHERE state NOT IN ('TASK_STATE_COMPLETED','TASK_STATE_CANCELED',"
                    "'TASK_STATE_FAILED','TASK_STATE_REJECTED')")
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        return [dict(r) for r in rows]

    # --- batches of work --------------------------------------------------------
    #
    # A batch names a denominator. Nothing here computes a percentage: the
    # arithmetic lives in `collab.batch` so that the hub, the status line and
    # every client run the same lines over the same counts. See that module for
    # why the figure is counted rather than reported.

    def add_batch(self, batch_id: str, *, name: str, opened_by: str) -> dict[str, Any] | None:
        """Open a batch, or return None because one already is.

        None rather than an exception reaching the request: two agents opening
        a batch at the same moment is an ordinary race between collaborators,
        and the loser is told which batch is in the way. A raised
        IntegrityError would arrive at the agent as a bare HTTP 500 — the same
        shape of failure a freed display name used to cause on rejoin.

        None says only «the insert was refused», never why. Every constraint on
        this table raises the same IntegrityError — the id primary key and the
        NOT NULLs as well as the one-open-batch index — so a caller that read
        None as «one is already open» would answer a different fault with
        confident and wrong advice. The caller looks, and says what it finds.

        Rolled back rather than left open. sqlite3 opens an implicit
        transaction for the INSERT and the refusal does not end it, so the
        connection went on holding the write lock until some later write
        happened to commit it — on a quiet hub, indefinitely.
        """
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO batches (id, name, state, opened_by, opened_at)"
                    " VALUES (?,?,'open',?,?)",
                    (batch_id, name, opened_by, time.time()),
                )
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None
            self._db.commit()
            row = self._db.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row)

    def open_batch(self) -> dict[str, Any] | None:
        """The batch tasks are currently being proposed into, if any.

        One at a time, by construction: a second open batch would mean a task
        joining whichever the hub happened to read first, and two agents would
        be watching two different denominators while believing they shared one.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM batches WHERE state='open' ORDER BY opened_at DESC"
            ).fetchone()
        return dict(row) if row else None

    def latest_batch(self) -> dict[str, Any] | None:
        """The open batch, or the last one closed.

        Closing does not delete: the counts of finished work stay readable,
        because «that batch is done» is an answer somebody wants after the
        fact and not only during.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM batches"
                " ORDER BY (state='open') DESC, COALESCE(closed_at, opened_at) DESC"
            ).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def close_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Close it, or return None because this call closed nothing.

        The `AND state='open'` guard already protected `closed_at` from being
        overwritten by a second close, but the row came back either way — so
        closing twice answered 200 and announced «closed the batch X» to the
        room a second time. Another agent was then told about an event that had
        not happened, which is the same class of untruth as a stale figure: a
        statement about now, built out of something that was true before.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE batches SET state='closed', closed_at=?"
                " WHERE id=? AND state='open'",
                (time.time(), batch_id),
            )
            self._db.commit()
            if not cur.rowcount:
                return None
            row = self._db.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def batch_tasks(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE batch=? ORDER BY created_at", (batch_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- shared files -----------------------------------------------------------

    def add_file(self, file_id: str, *, name: str, size: int, sha256: str, sender: str,
                 recipient: str | None, room: str | None,
                 audience: Iterable[str] = ()) -> dict[str, Any]:
        """Record an upload, and for a room file, who it is waiting for.

        ``audience`` is the participant ids the file is held for — the people in
        the session at this moment, minus the sender. Written now rather than
        computed at ack time because the question is «who was there when it was
        sent», and the roster keeps changing after that.
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO files (id,name,size,sha256,sender,recipient,room,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (file_id, name, size, sha256, sender, recipient, room, now),
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO file_collections (file_id, participant_id)"
                " VALUES (?,?)",
                [(file_id, pid) for pid in audience],
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row)

    def record_collection(self, file_id: str, participant_id: str) -> bool:
        """Note that ``participant_id`` has the file. True the first time only.

        Idempotent: a second ack from the same participant leaves the row and
        its timestamp alone. Someone who was not awaited — they joined after the
        file was sent — gets a row too, so the history says they collected it,
        but they were never counted among the remaining and so settle nothing.
        """
        now = time.time()
        with self._lock:
            already = self._db.execute(
                "SELECT collected_at FROM file_collections"
                " WHERE file_id=? AND participant_id=?", (file_id, participant_id),
            ).fetchone()
            if already is not None and already["collected_at"] is not None:
                return False
            # A new row here is somebody who was not awaited; an existing one
            # keeps its `awaited`, whatever it was.
            self._db.execute(
                "INSERT INTO file_collections"
                " (file_id, participant_id, collected_at, awaited)"
                " VALUES (?,?,?,0) ON CONFLICT(file_id, participant_id)"
                " DO UPDATE SET collected_at=excluded.collected_at",
                (file_id, participant_id, now),
            )
            self._db.commit()
        return True

    def collectors(self, file_id: str) -> list[str]:
        """Participant ids that have collected the file, in the order they did."""
        with self._lock:
            rows = self._db.execute(
                "SELECT participant_id FROM file_collections"
                " WHERE file_id=? AND collected_at IS NOT NULL ORDER BY collected_at",
                (file_id,),
            ).fetchall()
        return [str(r["participant_id"]) for r in rows]

    def file_progress(self, file_id: str) -> dict[str, Any]:
        """How far a room file is from being everyone's.

        ``collected`` counts every ack recorded; ``remaining`` and ``awaiting``
        are the awaited participants who have not acked AND are still in the
        session. Someone removed with `collab kick` is never coming back for
        it, so they drop out of the count rather than holding the blob until
        the clock runs out. ``expected`` is how many were awaited to begin
        with: zero means nobody was there to be counted — a file shared with an
        empty room, or one recorded before audiences were — and then no ack
        can complete it; only its clock or a withdrawal ends it.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT c.collected_at, c.awaited, p.name, p.revoked"
                " FROM file_collections c"
                " LEFT JOIN participants p ON p.id = c.participant_id"
                " WHERE c.file_id=? ORDER BY p.joined_at",
                (file_id,),
            ).fetchall()
        collected = sum(1 for r in rows if r["collected_at"] is not None)
        awaiting = [str(r["name"]) for r in rows
                    if r["awaited"] and r["collected_at"] is None
                    and r["name"] is not None and not r["revoked"]]
        return {"expected": sum(1 for r in rows if r["awaited"]),
                "collected": collected,
                "remaining": len(awaiting), "awaiting": awaiting}

    def files_nobody_awaits(self) -> list[dict[str, Any]]:
        """Room files still on disk that every remaining awaited person has.

        The last collector's ack normally settles a file, but the last awaited
        person may instead have been removed from the session after the others
        collected — and then no ack is ever coming. These are those files:
        somebody has collected, and nobody still here is owed a copy. A file
        with an empty audience, or one nobody at all has collected, is not
        listed: it waits for its clock like any other.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT f.* FROM files f"
                " WHERE f.state='available' AND f.recipient IS NULL"
                " AND EXISTS (SELECT 1 FROM file_collections c"
                "             WHERE c.file_id=f.id AND c.collected_at IS NOT NULL)"
                " AND NOT EXISTS (SELECT 1 FROM file_collections c"
                "                 JOIN participants p ON p.id = c.participant_id"
                "                 WHERE c.file_id=f.id AND c.collected_at IS NULL"
                "                 AND p.revoked = 0)"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None

    def files(self, *, viewer: str | None = None, include_gone: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files"
        if not include_gone:
            sql += " WHERE state='available'"
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        out = [dict(r) for r in rows]
        if viewer is None:
            return out
        # A file addressed to someone is visible only to the two ends.
        return [f for f in out
                if not f["recipient"] or viewer in (f["recipient"], f["sender"])]

    def mark_file(self, file_id: str, state: str, *, acked_by: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE files SET state=?, acked_at=?, acked_by=? WHERE id=?",
                (state, time.time() if acked_by else None, acked_by, file_id),
            )
            self._db.commit()

    def expired_files(self, ttl_seconds: float,
                      room_ttl_seconds: float | None = None) -> list[dict[str, Any]]:
        """Files still waiting past their clock.

        A file addressed to somebody gets ``ttl_seconds``; one shared with a
        room gets ``room_ttl_seconds`` — the same clock when none is given.
        """
        now = time.time()
        direct_cutoff = now - ttl_seconds
        room_cutoff = now - (ttl_seconds if room_ttl_seconds is None else room_ttl_seconds)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM files WHERE state='available' AND ("
                " (recipient IS NOT NULL AND created_at < ?)"
                " OR (recipient IS NULL AND created_at < ?))",
                (direct_cutoff, room_cutoff),
            ).fetchall()
        return [dict(r) for r in rows]


def _visible_to(recipient_id: str | None, sender_id: str | None,
                viewer_id: str | None) -> bool:
    """DMs are visible only to their two ends; everything else is room-wide.

    Compared by id, so a rename on either end changes nothing.
    """
    if not recipient_id:
        return True
    if viewer_id is None:
        return True
    return viewer_id in (recipient_id, sender_id)


def _to_participant(row: sqlite3.Row) -> Participant:
    return Participant(
        id=row["id"],
        name=row["name"],
        is_host=bool(row["is_host"]),
        joined_at=row["joined_at"],
        last_seen=row["last_seen"],
        revoked=bool(row["revoked"]),
        meta=json.loads(row["meta"] or "{}"),
    )
