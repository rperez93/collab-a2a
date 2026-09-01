"""Opening a database written by an older collab.

Sessions are meant to be resumable — a conversation from last month should
still open. `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it
was, so when identity moved from display names to ids, every older session
became unreadable: the first read of `participants` raised and took `collab
sessions`, resume, and the hub itself down with it.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from collab.server.store import Store, token_hash

#: The shape collab wrote before identity became an id.
OLD_SCHEMA = """
CREATE TABLE events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    room      TEXT,
    sender    TEXT NOT NULL,
    recipient TEXT,
    ts        TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE TABLE participants (
    name       TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    is_host    INTEGER NOT NULL DEFAULT 0,
    joined_at  REAL NOT NULL,
    last_seen  REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}'
);
"""

#: The shape collab wrote before work could be gathered into a batch. The task
#: board existed; the column saying which batch a task belongs to did not.
PRE_BATCH_TASKS = """
CREATE TABLE tasks (
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


@pytest.fixture()
def old_db(tmp_path):
    """A session as an older collab would have left it."""
    path = tmp_path / "hub.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    now = time.time()
    for name, host, token in (("jarvis", 1, "aaa"), ("cortana", 0, "bbb")):
        con.execute(
            "INSERT INTO participants (name, token_hash, is_host, joined_at,"
            " last_seen, meta) VALUES (?,?,?,?,?,?)",
            # Tokens were only ever stored hashed, then as now.
            (name, token_hash(token), host, now, now, "{}"),
        )
    for i, (sender, recipient) in enumerate(
            [("jarvis", None), ("cortana", None), ("jarvis", "cortana")]):
        payload = json.dumps({"collab": "v1", "kind": "chat", "from": sender,
                              "text": f"m{i}", "room": "general", "seq": i + 1,
                              "ts": "2026-08-01T10:00:00Z"})
        con.execute(
            "INSERT INTO events (kind, room, sender, recipient, ts, payload)"
            " VALUES (?,?,?,?,?,?)",
            ("chat", "general", sender, recipient, "2026-08-01T10:00:00Z", payload),
        )
    con.commit()
    con.close()
    return path


def test_an_old_session_opens_instead_of_raising(old_db):
    """This is the crash: reading participants blew up on a missing column."""
    store = Store(old_db)
    try:
        assert {p.name for p in store.participants()} == {"jarvis", "cortana"}
    finally:
        store.close()


def test_everyone_gets_an_id(old_db):
    store = Store(old_db)
    try:
        people = store.participants()
        assert all(p.id.startswith("p_") for p in people)
        assert len({p.id for p in people}) == 2, "and they are distinct"
    finally:
        store.close()


def test_names_still_resolve_after_the_migration(old_db):
    """Someone holding the old name must still be reachable."""
    store = Store(old_db)
    try:
        jarvis = next(p for p in store.participants() if p.name == "jarvis")
        assert store.resolve_name("jarvis") == jarvis.id
    finally:
        store.close()


def test_the_history_is_all_still_there(old_db):
    store = Store(old_db)
    try:
        assert len(store.history(limit=100)) == 3
        assert store.max_seq() == 3
    finally:
        store.close()


def test_old_direct_messages_stay_private(old_db):
    """Events carried names only; visibility now compares ids.

    Without backfilling them, a message addressed to someone becomes visible
    to nobody — or, worse, to everybody.
    """
    store = Store(old_db)
    try:
        people = {p.name: p.id for p in store.participants()}
        jarvis_sees = [e.text for e in store.since(0, viewer=people["jarvis"])]
        cortana_sees = [e.text for e in store.since(0, viewer=people["cortana"])]

        assert "m2" in jarvis_sees, "the sender sees their own direct message"
        assert "m2" in cortana_sees, "and so does the recipient"

        store.add_participant("dave", "ccc")
        dave = next(p for p in store.participants() if p.name == "dave")
        assert "m2" not in [e.text for e in store.since(0, viewer=dave.id)]
    finally:
        store.close()


def test_the_token_still_works(old_db):
    """Nobody should have to rejoin because collab was upgraded."""
    store = Store(old_db)
    try:
        assert store.participant_for_token("aaa").name == "jarvis"
    finally:
        store.close()


def test_migrating_twice_changes_nothing(old_db):
    first = Store(old_db)
    try:
        before = {p.name: p.id for p in first.participants()}
    finally:
        first.close()

    second = Store(old_db)
    try:
        assert {p.name: p.id for p in second.participants()} == before
    finally:
        second.close()


def test_a_current_database_is_untouched(tmp_path):
    store = Store(tmp_path / "new.db")
    try:
        person = store.add_participant("alice", "tok", is_host=True)
        original = person.id
    finally:
        store.close()

    reopened = Store(tmp_path / "new.db")
    try:
        assert reopened.participants()[0].id == original
    finally:
        reopened.close()


@pytest.fixture()
def pre_batch_db(tmp_path):
    """A session with a task board, from before batches existed."""
    path = tmp_path / "board.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_BATCH_TASKS)
    now = time.time()
    for task_id, state in (("T_old1", "TASK_STATE_COMPLETED"),
                           ("T_old2", "TASK_STATE_WORKING")):
        con.execute(
            "INSERT INTO tasks (id,title,state,owner,room,created_by,created_at,"
            "updated_at,detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, f"the {task_id}", state, "jarvis", "general", "jarvis",
             now, now, ""),
        )
    con.commit()
    con.close()
    return path


def test_a_board_from_before_batches_still_reads(pre_batch_db):
    """`CREATE TABLE IF NOT EXISTS` left the old table exactly as it was.

    Without the migration, every read of `tasks` names a `batch` column that
    the table does not have — which takes `collab task list`, the roster
    snapshot and the hub's own start-up down with it, on a session that was
    working perfectly well until it was upgraded.
    """
    store = Store(pre_batch_db)
    try:
        assert {t["id"] for t in store.tasks()} == {"T_old1", "T_old2"}
    finally:
        store.close()


def test_tasks_that_predate_batches_belong_to_no_batch(pre_batch_db):
    """Back-filling them into some invented batch would report a percentage
    for a set of work nobody ever scoped as one."""
    store = Store(pre_batch_db)
    try:
        assert all(t["batch"] is None for t in store.tasks())
        assert store.open_batch() is None
    finally:
        store.close()


def test_an_upgraded_board_can_hold_a_batch_like_a_fresh_one(pre_batch_db):
    """The migration has to leave the two databases genuinely equivalent, not
    merely readable: a session resumed from last month must be able to open a
    batch and be counted exactly as a new one is."""
    store = Store(pre_batch_db)
    try:
        store.add_batch("B_1", name="the migration", opened_by="jarvis")
        store.upsert_task("T_new", title="new work", state="TASK_STATE_COMPLETED",
                          owner="jarvis", room="general", created_by="jarvis",
                          join_open_batch=True)
        assert [t["id"] for t in store.batch_tasks("B_1")] == ["T_new"]
        assert store.open_batch()["id"] == "B_1"
    finally:
        store.close()


def test_a_fresh_database_gets_the_batch_column_without_a_migration(tmp_path):
    """The two paths into the current shape must arrive at the same place.

    A column added only by the migration is a column a brand-new hub does not
    have, which is the same failure with the databases swapped round.
    """
    store = Store(tmp_path / "fresh.db")
    try:
        store.add_batch("B_1", name="the migration", opened_by="alice")
        store.upsert_task("T_1", title="work", state="TASK_STATE_SUBMITTED",
                          owner=None, room="general", created_by="alice",
                          join_open_batch=True)
        assert store.tasks()[0]["batch"] == "B_1"
    finally:
        store.close()


def test_migrating_a_board_twice_changes_nothing(pre_batch_db):
    """The ALTER runs unconditionally on a table that has no column and never
    again — a second run that tried would raise and refuse to open the hub."""
    first = Store(pre_batch_db)
    try:
        first.add_batch("B_1", name="the migration", opened_by="jarvis")
    finally:
        first.close()

    second = Store(pre_batch_db)
    try:
        assert second.open_batch()["id"] == "B_1"
        assert len(second.tasks()) == 2
    finally:
        second.close()


def test_an_upgraded_board_still_ends_up_in_wal(pre_batch_db):
    """The batch migration is DDL in the same implicit transaction as the rest.

    `journal_mode` cannot change inside one and fails silently rather than
    raising, so a new step added carelessly here would quietly leave every
    upgraded session on the rollback journal.
    """
    store = Store(pre_batch_db)
    try:
        mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        store.close()
    assert mode == "wal"


def test_an_old_session_still_ends_up_in_wal(old_db):
    """The pragma used to run after the migration, and quietly lose.

    The back-fills in ``_migrate`` are DML, so sqlite3 has an implicit
    transaction open by the time the pragma runs, and ``journal_mode`` cannot
    change inside one.  It fails silently rather than raising, so a migrated
    session kept the rollback journal for the rest of its life while a fresh
    one got WAL — losing the concurrency that lets a hub and a `collab
    sessions` in another terminal touch the same file.
    """
    store = Store(old_db)
    try:
        mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        store.close()
    assert mode == "wal", "a migrated session must get WAL like a fresh one"
