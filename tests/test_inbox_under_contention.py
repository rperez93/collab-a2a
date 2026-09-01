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


class _RaceOnSelect:
    """A connection that lets a rival in between the look and the leap.

    `record` asks whether it has the seq and then inserts it; the two are not
    one step, and two daemons on one directory —which is what a lost start-up
    race leaves— both answered no.
    """

    def __init__(self, real, rival, env):
        self._real, self._rival, self._env = real, rival, env

    def execute(self, sql, *args):
        result = self._real.execute(sql, *args)
        if sql.startswith("SELECT 1 FROM inbox") and self._rival is not None:
            rival, self._rival = self._rival, None
            rival.record(self._env)
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_an_event_stored_twice_over_is_not_a_dropped_feed(inbox, tmp_path):
    """The IntegrityError went uncaught into `_connect_forever`'s general
    handler, where it was logged as a dropped feed and counted as a failure —
    and after eight of those the hint tells a guest the hub is unreachable and
    to go and ask a human for a fresh link, over a race that never left this
    machine.
    """
    rival = Inbox(tmp_path)
    try:
        inbox._db = _RaceOnSelect(inbox._db, rival, _env(7))

        assert inbox.record(_env(7)) is False, "we already have this event"
        assert inbox.last_seq() == 7
    finally:
        rival.close()

    lines = (tmp_path / "inbox.jsonl").read_text().splitlines()
    assert len(lines) == 1, "one event, written once"


def test_the_inbox_still_works_after_a_collision(inbox, tmp_path):
    """The rollback must leave the connection usable: a daemon that survived
    the race and then could not write again would be no better off."""
    rival = Inbox(tmp_path)
    try:
        inbox._db = _RaceOnSelect(inbox._db, rival, _env(7))
        inbox.record(_env(7))
    finally:
        rival.close()

    assert inbox.record(_env(8)) is True
    assert [e.seq for e in inbox.all_events()] == [7, 8]
