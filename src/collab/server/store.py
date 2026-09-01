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
from typing import Any

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

CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    state      TEXT NOT NULL,
    owner      TEXT,
    room       TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
"""


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

    def consume_invite(self, code: str) -> tuple[bool, str]:
        """Validate and spend one use.  Returns ``(ok, reason)``."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM invites WHERE code_hash=?", (token_hash(code),)
            ).fetchone()
            if row is None:
                return False, "unknown invite code"
            if row["expires_at"] is not None and time.time() > row["expires_at"]:
                return False, "invite expired"
            if row["max_uses"] and row["uses"] >= row["max_uses"]:
                return False, "invite already used the maximum number of times"
            self._db.execute(
                "UPDATE invites SET uses=uses+1 WHERE code_hash=?", (token_hash(code),)
            )
            self._db.commit()
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
                    room: str | None, created_by: str, detail: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if existing is None:
                self._db.execute(
                    "INSERT INTO tasks (id,title,state,owner,room,created_by,created_at,updated_at,detail)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, title, state, owner, room, created_by, now, now, detail),
                )
            else:
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

    # --- shared files -----------------------------------------------------------

    def add_file(self, file_id: str, *, name: str, size: int, sha256: str, sender: str,
                 recipient: str | None, room: str | None) -> dict[str, Any]:
        with self._lock:
            self._db.execute(
                "INSERT INTO files (id,name,size,sha256,sender,recipient,room,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (file_id, name, size, sha256, sender, recipient, room, time.time()),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row)

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

    def expired_files(self, ttl_seconds: float) -> list[dict[str, Any]]:
        cutoff = time.time() - ttl_seconds
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM files WHERE state='available' AND created_at < ?", (cutoff,)
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
