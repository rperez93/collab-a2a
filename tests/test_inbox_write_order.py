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
