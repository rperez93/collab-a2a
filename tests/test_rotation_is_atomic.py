"""Rotation is one act, and a retired invite is retired at once.

`Store._lock` is a `threading.Lock` on one connection, so it orders nothing
between PROCESSES — and rotation is now a separate process from the hub, which
is the whole change. Two gaps followed from that:

* `consume_invite` reads the row, then updates it. Between the two a rotation
  can delete it; the UPDATE matches nothing, the function returns success
  anyway, and a link the host has just retired lets somebody in.
* `clear_invites` and `add_invite` are two statements. Between them the table
  holds no invite at all, and two rotations racing can leave two live rows —
  which is exactly the «exactly one way in» the feature is sold on.

Neither is reachable by ordinary timing today. Both are reachable, and this is
a credential: the guarantee has to hold because it is written down, not because
the window happens to be narrow.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from collab.server.session import create_session, rotate_invite
from collab.server.store import Store, token_hash


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A real session, so rotation runs against the config it was built for."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    return create_session("alice", 9000)


def test_a_join_cannot_spend_an_invite_that_vanished_under_it(tmp_path):
    """The row is read, then gone, then spent — and the spend decides.

    The cross-process version of this is now prevented outright: `BEGIN
    IMMEDIATE` takes the write lock before the read, so a rotation on another
    connection waits rather than landing in the middle. This drives the same
    disappearance on the transaction's OWN connection, which is the one way in
    that still exists, and holds the guard that catches it: the answer comes
    from whether the UPDATE touched a row, never from the SELECT before it.
    """
    path = tmp_path / "hub.db"
    store = Store(path)
    store.add_invite("first", ttl_seconds=3600, max_uses=0)

    class VanishesMidJoin:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kw):
            if sql.strip().upper().startswith("UPDATE INVITES"):
                self._real.execute("DELETE FROM invites")
            return self._real.execute(sql, *args, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real = store._db
    store._db = VanishesMidJoin(real)                # type: ignore[assignment]
    try:
        ok, reason = store.consume_invite("first")
    finally:
        store._db = real                             # type: ignore[assignment]
        store.close()

    assert ok is False, "an invite retired mid-join was still accepted"
    assert reason, "a refusal with no reason is not a refusal"


def test_a_rotation_waits_for_a_join_rather_than_cutting_into_it(tmp_path):
    """And the two together never admit anybody on a retired link."""
    path = tmp_path / "hub.db"
    Store(path).close()
    minted: list[str] = []
    admitted: list[str] = []

    def rotate(n: int) -> None:
        store = Store(path)
        try:
            code = f"code-{n}"
            store.replace_invite(code, ttl_seconds=3600, max_uses=0)
            minted.append(code)
        except sqlite3.OperationalError:
            pass                                      # a loser waiting out the lock
        finally:
            store.close()

    def join(n: int) -> None:
        store = Store(path)
        try:
            for code in list(minted):
                if store.consume_invite(code)[0]:
                    admitted.append(code)
        except sqlite3.OperationalError:
            pass
        finally:
            store.close()

    Store(path).replace_invite("code-0", ttl_seconds=3600, max_uses=0)
    minted.append("code-0")
    threads = ([threading.Thread(target=rotate, args=(n,)) for n in range(1, 6)]
               + [threading.Thread(target=join, args=(n,)) for n in range(5)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    con = sqlite3.connect(path)
    live = [r[0] for r in con.execute("SELECT code_hash FROM invites")]
    con.close()
    # ONE WAY IN, whatever the interleaving was. Not two: that is the failure a
    # clear-then-add can leave behind. Not none: that would lock out the host
    # as well. What `admitted` holds is not asserted on — a join that succeeded
    # while its code was live is legitimate even though a later rotation has
    # since replaced it, and reading the list at the end says nothing about
    # what was true when each one ran.
    assert len(live) == 1, f"{len(live)} live invites after five rotations"


def test_eight_rotations_at_once_leave_exactly_one_way_in(session):
    """Not two, and not none."""
    def rotate() -> None:
        try:
            rotate_invite(session)
        except Exception:                             # noqa: BLE001
            pass                                      # a loser is fine; two rows are not

    threads = [threading.Thread(target=rotate) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    con = sqlite3.connect(session.db_path)
    rows = con.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
    con.close()
    assert rows == 1, f"{rows} invites live after eight rotations"


def test_a_rotation_that_fails_halfway_leaves_the_old_link_working(tmp_path):
    """All of it or none of it — the deterministic half of «one act».

    Clearing and then adding as two statements commits the delete before the
    insert is even attempted, so anything that goes wrong in between leaves a
    session with no way into it at all and a host holding a link that no longer
    works. In one transaction the failure rolls the delete back with it.
    """
    path = tmp_path / "hub.db"
    store = Store(path)
    store.add_invite("old", ttl_seconds=3600, max_uses=0)

    class FailsAtTheInsert:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kw):
            if sql.strip().upper().startswith("INSERT INTO INVITES"):
                raise sqlite3.OperationalError("disk full, say")
            return self._real.execute(sql, *args, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real = store._db
    store._db = FailsAtTheInsert(real)               # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.replace_invite("new", ttl_seconds=3600, max_uses=0)
    finally:
        store._db = real                             # type: ignore[assignment]

    try:
        assert store.consume_invite("old")[0] is True, \
            "a failed rotation took the old link with it"
        assert store.consume_invite("new")[0] is False
    finally:
        store.close()


def test_rotate_invite_itself_is_all_or_nothing(session, monkeypatch):
    """The same property, through the function a host actually runs.

    The test above holds the store; this holds `rotate_invite`, so a rotation
    reassembled out of a clear and an add is caught here rather than passing
    because the store still offers both.
    """
    from collab.server import session as session_mod

    old = session.invite
    real_store = session_mod.Store

    class FailsAtTheInsert:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kw):
            if sql.strip().upper().startswith("INSERT INTO INVITES"):
                raise sqlite3.OperationalError("disk full, say")
            return self._real.execute(sql, *args, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def store_that_fails(path):
        store = real_store(path)
        store._db = FailsAtTheInsert(store._db)      # type: ignore[assignment]
        return store

    monkeypatch.setattr(session_mod, "Store", store_that_fails)
    with pytest.raises(sqlite3.OperationalError):
        rotate_invite(session)
    monkeypatch.setattr(session_mod, "Store", real_store)

    store = real_store(session.db_path)
    try:
        assert store.consume_invite(old)[0] is True, \
            "a rotation that failed halfway left the session with no way in"
    finally:
        store.close()


def test_the_old_invite_dies_and_the_new_one_lives(session):
    old = session.invite
    rotate_invite(session)
    store = Store(session.db_path)
    try:
        assert store.consume_invite(old)[0] is False
        assert store.consume_invite(session.invite)[0] is True
    finally:
        store.close()


def test_the_row_is_still_hashed_and_still_one(session):
    """The fix changes when it is written, not what."""
    rotate_invite(session)
    con = sqlite3.connect(session.db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM invites").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0]["code_hash"] == token_hash(session.invite)
    assert rows[0]["code_hash"] != session.invite, "stored in the clear"
