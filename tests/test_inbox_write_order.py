"""The log is written before the cursor that would resume past it.

`record` wrote the database, committed, and then appended to the JSONL. Resume
asks the database for `last_seq`, so an event committed there is one the hub
will never be asked for again — and a process that died in that window left an
agent tailing the line stream, which is the arrangement every skill here
prescribes, permanently missing a message that `collab recv` could still see,
with nothing anywhere saying the two views had diverged.

The window is two lines wide and the odds are small. What matters is which way
it fails: a message nobody ever sees, in a tool whose whole purpose is that
messages are seen.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from collab.client.inbox import BUSY_TIMEOUT_MS, Inbox
from collab.protocol import KIND_CHAT, Envelope, now_iso


def _env(seq: int) -> Envelope:
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT, sender="alice",
                    body={"text": f"number {seq}"})


@pytest.fixture()
def inbox(tmp_path):
    box = Inbox(tmp_path)
    yield box
    box.close()


class _WatchesTheCommit:
    """Notes what the log held at the moment the database became durable."""

    def __init__(self, real, jsonl):
        self._real, self._jsonl = real, jsonl
        self.log_at_commit: str | None = None

    def commit(self):
        self.log_at_commit = self._jsonl.read_text() if self._jsonl.exists() else ""
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _DiesAtTheCommit:
    def __init__(self, real):
        self._real = real

    def commit(self):
        raise KeyboardInterrupt("killed between the two writes")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_log_has_the_event_before_the_database_commits(inbox, tmp_path):
    """The commit is the point of no return, because it moves `last_seq`."""
    watcher = _WatchesTheCommit(inbox._db, tmp_path / "inbox.jsonl")
    inbox._db = watcher

    assert inbox.record(_env(1)) is True
    assert watcher.log_at_commit is not None, "it never committed"
    assert '"seq": 1' in watcher.log_at_commit, \
        "the cursor would have moved past an event the log did not have"


def test_a_crash_at_the_commit_costs_a_repeat_and_not_a_message(inbox, tmp_path):
    """Written the other way round the same crash lost the message for good.

    Written this way it costs a duplicate line, because resume re-delivers an
    event the log already has — the direction `wake` argues for out loud: an
    agent that was briefly broken should be told twice rather than not at all.
    """
    real = inbox._db
    inbox._db = _DiesAtTheCommit(real)
    with pytest.raises(KeyboardInterrupt):
        inbox.record(_env(1))
    inbox._db = real
    real.rollback()

    assert inbox.last_seq() == 0, "resume will ask for it again"
    assert '"seq": 1' in (tmp_path / "inbox.jsonl").read_text(), \
        "the message reached nobody"


def test_an_event_we_already_have_appends_nothing(inbox, tmp_path):
    """The daemon decides from this return whether to broadcast and whether to
    note a wake, so a replay after a reconnect must do neither twice."""
    assert inbox.record(_env(1)) is True
    assert inbox.record(_env(1)) is False

    lines = (tmp_path / "inbox.jsonl").read_text().splitlines()
    assert len(lines) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_a_log_that_cannot_be_written_takes_the_row_with_it(inbox, tmp_path):
    """The append can FAIL, not merely be interrupted — and that escaped.

    A read-only state directory or a full disk raises inside the append, and
    the write transaction was left open. Everything after it then read through
    the transaction rather than the database: `last_seq` answered with a seq
    that had not been committed, so the resume skipped that event, and the next
    successful record committed the orphaned row alongside its own. The seq was
    in the database, absent from the log, and unreachable for ever — the same
    silent divergence, one step further along.
    """
    inbox.record(_env(1))
    log = tmp_path / "inbox.jsonl"
    log.chmod(0o400)
    try:
        with pytest.raises(OSError):
            inbox.record(_env(50))

        assert inbox.last_seq() == 1, "the cursor moved past an event nobody has"
        assert [e.seq for e in inbox.all_events()] == [1]
    finally:
        log.chmod(0o600)

    # And the next event must not carry the orphan in with it.
    assert inbox.record(_env(51)) is True
    assert [e.seq for e in inbox.all_events()] == [1, 51]
    assert [json.loads(line)["seq"] for line in log.read_text().splitlines()] == [1, 51]


class _RollbackAlsoFails:
    """The second failure: the log cannot be written AND the rollback refuses.

    Only once, so the recovery afterwards is the real connection's behaviour
    rather than this stand-in's.
    """

    def __init__(self, real):
        self._real, self.armed = real, True

    def rollback(self):
        if self.armed:
            self.armed = False
            raise sqlite3.OperationalError("rollback refused too")
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _both_fail(inbox, log, seq):
    """Break the log AND the rollback, and let `record` run into both."""
    log.chmod(0o400)
    inbox._db = _RollbackAlsoFails(inbox._db)
    try:
        with pytest.raises(OSError) as caught:
            inbox.record(_env(seq))
    finally:
        log.chmod(0o600)
    return caught


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_a_rollback_that_fails_too_does_not_leave_the_row_behind(inbox, tmp_path):
    """Both at once, which the first guard did not survive.

    A rollback that raises leaves the transaction open, so `last_seq` reads
    through it, the next `record` finds its own uncommitted row and answers
    that we already have it, and the next write that succeeds commits the
    orphan behind it. Measured before this: database [1, 50, 51] and log
    [1, 51] — seq 50 committed with no log line and unreachable by resume,
    which is the divergence the ordering exists to prevent arriving through
    the handler written for it.
    """
    inbox.record(_env(1))
    caught = _both_fail(inbox, tmp_path / "inbox.jsonl", 50)

    assert not isinstance(caught.value, sqlite3.Error), \
        "the rollback's complaint replaced the failure a person can act on"
    assert inbox.last_seq() == 1, "it read through a transaction nobody closed"
    assert [e.seq for e in inbox.all_events()] == [1]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_a_moment_of_bad_luck_does_not_deafen_the_daemon_for_good(inbox, tmp_path):
    """The Inbox is built once and never rebuilt, so a connection left closed
    would stop this process recording anything for the rest of its life over
    one bad moment. A fault that persists is loud through the append anyway."""
    inbox.record(_env(1))
    log = tmp_path / "inbox.jsonl"
    _both_fail(inbox, log, 50)

    assert inbox.record(_env(50)) is True, "it never recorded again"
    assert [e.seq for e in inbox.all_events()] == [1, 50]
    assert [json.loads(x)["seq"] for x in log.read_text().splitlines()] == [1, 50]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_the_replacement_connection_is_set_up_like_the_first_one(inbox, tmp_path):
    """The one way to get the recovery wrong.

    A hand-rolled reconnection comes back without `journal_mode=WAL` and
    without the busy timeout — which is the contention fix from earlier on this
    branch, reintroduced on the path nobody would think to look at. Both
    connections come out of `_connect` so that the two cannot drift; this is
    what says they have not.
    """
    was = (inbox._db.execute("PRAGMA journal_mode").fetchone()[0].lower(),
           inbox._db.execute("PRAGMA busy_timeout").fetchone()[0])
    assert was == ("wal", BUSY_TIMEOUT_MS)

    inbox.record(_env(1))
    _both_fail(inbox, tmp_path / "inbox.jsonl", 50)

    now = (inbox._db.execute("PRAGMA journal_mode").fetchone()[0].lower(),
           inbox._db.execute("PRAGMA busy_timeout").fetchone()[0])
    assert now == was, "the recovery path came back configured differently"
    assert inbox._db.row_factory is sqlite3.Row
