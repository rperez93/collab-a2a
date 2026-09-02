"""A file sent to a room stays until everyone in the room has it.

Acking used to delete the blob on the FIRST confirmation, whoever it came
from — right for a file addressed to one person, wrong for a file shared with a
room: the first agent to collect it took it away from everybody else. A room
file now waits for every participant who was in the session when it was sent,
or for a 30-minute clock, whichever comes first.
"""

from __future__ import annotations

import io
import sqlite3
import time
import types

import pytest

from collab.protocol import FILE_TTL_SECONDS, ROOM_FILE_TTL_SECONDS
from collab.server import store as store_module
from collab.server.app import create_app
from collab.server.store import Store, token_hash

EXT = "/ext/collab/v1"


def _join(client, session, name):
    r = client.post(f"{EXT}/join",
                    json={"invite": session["invite"], "name": name, "hello": {}})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _upload(client, headers, content=b"payload", name="build.tar.gz", **params):
    r = client.post(f"{EXT}/files", headers=headers,
                    files={"file": (name, io.BytesIO(content), "application/octet-stream")},
                    params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _blob(session, file_id):
    return store_module.Path(session["store"].path).parent / "files" / file_id


def _ack(client, headers, file_id):
    r = client.post(f"{EXT}/files/{file_id}/ack", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _received(client, headers):
    events = client.get(f"{EXT}/history", headers=headers).json()["events"]
    return [e for e in events
            if e["kind"] == "file" and e["body"].get("action") == "received"]


def _advance(monkeypatch, seconds):
    """Move the store's clock forward without touching anybody else's."""
    now = time.time()
    monkeypatch.setattr(store_module, "time",
                        types.SimpleNamespace(time=lambda: now + seconds))


# --- the defect ------------------------------------------------------------------

def test_the_first_collector_does_not_take_the_file_from_the_others(
        client, session, host_headers):
    """Three in the room; the host sends; bob collects. carol must still get it."""
    bob = _join(client, session, "bob")
    carol = _join(client, session, "carol")
    record = _upload(client, host_headers)

    first = _ack(client, bob, record["id"])
    assert first["deleted"] is False
    assert first["state"] == "available"
    assert first["remaining"] == 1 and first["collected"] == 1
    assert first["awaiting"] == ["carol"]
    assert _blob(session, record["id"]).exists(), "bob's ack deleted carol's copy"

    down = client.get(f"{EXT}/files/{record['id']}/content", headers=carol)
    assert down.status_code == 200 and down.content == b"payload"

    last = _ack(client, carol, record["id"])
    assert last["deleted"] is True
    assert last["state"] == "collected"
    assert last["remaining"] == 0 and last["collected"] == 2
    assert not _blob(session, record["id"]).exists()
    assert client.get(f"{EXT}/files", headers=bob).json()["files"] == []


def test_each_ack_says_who_collected_and_how_many_remain(client, session, host_headers):
    """The feed has to carry it, or the roster and the transcript cannot show it."""
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers)

    _ack(client, bob, record["id"])
    (event,) = _received(client, host_headers)
    assert event["from"] == "bob"
    body = event["body"]
    assert body["by"] == "bob"
    assert body["collected"] == 1 and body["remaining"] == 1
    assert body["deleted"] is False
    assert body["room"] == "general"


def test_collecting_twice_is_idempotent(client, session, host_headers):
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers)

    _ack(client, bob, record["id"])
    again = _ack(client, bob, record["id"])
    assert again["collected"] == 1 and again["remaining"] == 1
    assert _blob(session, record["id"]).exists()
    assert len(_received(client, host_headers)) == 1, "no second announcement"


def test_the_room_list_shows_who_still_has_to_collect(client, session, host_headers):
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers)
    _ack(client, bob, record["id"])

    (listed,) = client.get(f"{EXT}/files", headers=host_headers).json()["files"]
    assert listed["id"] == record["id"]
    assert listed["collected"] == 1 and listed["remaining"] == 1
    assert listed["awaiting"] == ["carol"]


# --- who counts -----------------------------------------------------------------

def test_the_audience_is_who_was_there_when_it_was_sent(client, session, host_headers):
    """Someone who joins afterwards is welcome to it but does not keep it alive."""
    bob = _join(client, session, "bob")
    record = _upload(client, host_headers)
    dave = _join(client, session, "dave")

    assert client.get(f"{EXT}/files/{record['id']}/content",
                      headers=dave).status_code == 200, "a late joiner may still read it"
    late = _ack(client, dave, record["id"])
    assert late["deleted"] is False, "dave was never awaited, so his ack settles nothing"
    assert late["awaiting"] == ["bob"]

    done = _ack(client, bob, record["id"])
    assert done["deleted"] is True


def test_someone_removed_from_the_session_does_not_block_deletion(
        client, session, host_headers):
    """A kicked participant is never coming back for the file."""
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers)

    _ack(client, bob, record["id"])
    assert client.post(f"{EXT}/revoke", headers=host_headers,
                       json={"name": "carol"}).status_code == 200

    # The sweep runs on the paths that touch the directory anyway.
    files = client.get(f"{EXT}/files", headers=host_headers).json()["files"]
    assert files == []
    assert not _blob(session, record["id"]).exists()


def test_a_file_shared_with_an_empty_room_waits_for_its_clock(
        client, session, host_headers, monkeypatch):
    """Nobody was there to be counted, so nothing but the TTL can settle it —
    the sender's own ack is not a collection."""
    record = _upload(client, host_headers)
    mine = _ack(client, host_headers, record["id"])
    assert mine["deleted"] is False and mine["remaining"] == 0
    assert _blob(session, record["id"]).exists()


# --- the clock ------------------------------------------------------------------

def test_a_room_file_is_swept_after_thirty_minutes(client, session, host_headers,
                                                    monkeypatch):
    """Two in the room, one collects, the other never does: 31 minutes later the
    host's disk is clear."""
    assert ROOM_FILE_TTL_SECONDS == 30 * 60
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers)
    _ack(client, bob, record["id"])

    _advance(monkeypatch, ROOM_FILE_TTL_SECONDS + 60)
    assert client.get(f"{EXT}/files", headers=host_headers).json()["files"] == []
    assert not _blob(session, record["id"]).exists()
    assert session["store"].get_file(record["id"])["state"] == "expired"


def test_a_direct_file_keeps_its_day(client, session, host_headers, monkeypatch):
    """The room clock must not shorten a file addressed to one person."""
    assert FILE_TTL_SECONDS == 24 * 3600
    bob = _join(client, session, "bob")
    record = _upload(client, host_headers, to="bob")

    _advance(monkeypatch, ROOM_FILE_TTL_SECONDS + 60)
    assert [f["id"] for f in client.get(f"{EXT}/files", headers=bob).json()["files"]] \
        == [record["id"]]

    _advance(monkeypatch, FILE_TTL_SECONDS + 60)
    assert client.get(f"{EXT}/files", headers=bob).json()["files"] == []


# --- a direct file is unchanged -------------------------------------------------

def test_a_direct_file_still_goes_on_the_recipients_single_ack(
        client, session, host_headers):
    bob = _join(client, session, "bob")
    _join(client, session, "carol")
    record = _upload(client, host_headers, to="bob")

    done = _ack(client, bob, record["id"])
    assert done["deleted"] is True and done["state"] == "collected"
    assert not _blob(session, record["id"]).exists()


# --- an older session -----------------------------------------------------------

#: `files` as it was before collections were recorded: one ack per file. The
#: people are in the current shape; only the file side is old.
PRE_COLLECTIONS_FILES = """
CREATE TABLE participants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    is_host    INTEGER NOT NULL DEFAULT 0,
    joined_at  REAL NOT NULL,
    last_seen  REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE participant_names (
    name           TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    claimed_at     REAL NOT NULL
);
CREATE TABLE files (
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
"""


@pytest.fixture()
def old_hub(tmp_path):
    """A session with a room file still waiting, written before collections."""
    path = tmp_path / "hub.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_COLLECTIONS_FILES)
    now = time.time()
    for pid, name, host, token in (("p_alice", "alice", 1, "aaa"),
                                   ("p_bob", "bob", 0, "bbb")):
        con.execute(
            "INSERT INTO participants (id, name, token_hash, is_host, joined_at,"
            " last_seen) VALUES (?,?,?,?,?,?)",
            (pid, name, token_hash(token), host, now, now))
        con.execute(
            "INSERT INTO participant_names (name, participant_id, claimed_at)"
            " VALUES (?,?,?)", (name, pid, now))
    con.execute(
        "INSERT INTO files (id,name,size,sha256,sender,recipient,room,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("f_old", "notes.txt", 5, "0" * 64, "alice", None, "general", now))
    con.execute(
        "INSERT INTO files (id,name,size,sha256,sender,recipient,room,created_at,"
        "acked_at,acked_by,state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("f_done", "old.bin", 5, "1" * 64, "alice", "bob", None, now - 60,
         now - 30, "bob", "collected"))
    con.commit()
    con.close()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "f_old").write_bytes(b"hello")
    return path


def test_an_older_session_opens_and_still_serves_its_room_file(old_hub):
    store = Store(old_hub)
    try:
        app = create_app(store=store, session_id="s_old", host_name="alice",
                         public_url="http://testserver")
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            bob = {"Authorization": "Bearer bbb"}
            listed = client.get(f"{EXT}/files", headers=bob).json()["files"]
            assert [f["id"] for f in listed] == ["f_old"]
            got = client.get(f"{EXT}/files/f_old/content", headers=bob)
            assert got.status_code == 200 and got.content == b"hello"
            # Nobody was recorded as awaiting it, so the clock decides its end.
            acked = _ack(client, bob, "f_old")
            assert acked["deleted"] is False

        # The old single-ack record was folded into the new table too.
        assert store.file_progress("f_done")["collected"] == 1
        assert store.collectors("f_done") == ["p_bob"]
    finally:
        store.close()


def test_migrating_an_older_session_twice_changes_nothing(old_hub):
    first = Store(old_hub)
    try:
        assert first.file_progress("f_done")["collected"] == 1
    finally:
        first.close()
    second = Store(old_hub)
    try:
        assert second.file_progress("f_done")["collected"] == 1
        assert second.get_file("f_old")["state"] == "available"
    finally:
        second.close()
