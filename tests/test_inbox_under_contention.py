"""The inbox is written from inside the daemon's feed loop, and it blocks.

`record` is a synchronous sqlite call made directly inside the `async for` over
the SSE stream, so anything it waits for stops the whole daemon: the heartbeat,
the bridge and the feed together. That made two ordinary sqlite facts into
faults worth naming — a reader locking the writer out, and two writers colliding
on one primary key.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from collab.client.inbox import Inbox
from collab.protocol import KIND_CHAT, Envelope, now_iso


def _env(seq: int) -> Envelope:
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT, sender="alice",
                    body={"text": f"number {seq}"})


@pytest.fixture()
def inbox(tmp_path):
    box = Inbox(tmp_path)
    yield box
    box.close()


def test_the_inbox_is_kept_in_wal_mode(inbox, tmp_path):
    mode = inbox._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_a_write_waits_for_the_database_only_briefly(inbox):
    """Five seconds — sqlite's default through Python — is long enough to take
    a local lock past READ_TIMEOUT and come out of it as a reconnect."""
    waited = inbox._db.execute("PRAGMA busy_timeout").fetchone()[0]
    assert 0 < waited <= 2000


def test_a_reader_does_not_stop_the_daemon_writing(inbox, tmp_path):
    """`collab recv`, the viewer and the status line all read this file while
    the daemon writes it. Under the rollback journal a reader holding its
    snapshot locked the daemon out of its own inbox, and the daemon does not
    merely wait — it stops streaming while it waits."""
    inbox.record(_env(1))

    reader = sqlite3.connect(tmp_path / "inbox.db")
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM inbox").fetchall()   # snapshot held open
    try:
        started = time.monotonic()
        assert inbox.record(_env(2)) is True
        assert time.monotonic() - started < 0.5, "the reader held the daemon up"
    finally:
        reader.close()
