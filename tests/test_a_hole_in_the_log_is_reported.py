"""A message that never arrived is invisible from every other surface.

The hub numbers every event and the daemon resumes with `Last-Event-ID`, so a
reconnect is meant to leave a gap-free log. When it does not, nothing says so:
`last_seq` reports how far the log reaches, the unread count reports what has
not been looked at, and a conversation with message 41 missing reads perfectly
well. The only symptom is an agent answering a question nobody can see it was
asked.

`Inbox.gaps` has always been able to detect it. It was removed by a dead-code
sweep — its own docstring claimed «used by the tests» and no test used it — and
the right answer was neither to delete it nor to leave it sitting there, but to
give it the readers it should have had: `collab check` and `collab logs`.
"""

from __future__ import annotations

import argparse

import pytest

from collab import cli
from collab.client.inbox import Inbox
from collab.protocol import Envelope


def _log(where, seqs):
    box = Inbox(where)
    for seq in seqs:
        box.record(Envelope(kind="chat", sender="a", text=f"m{seq}",
                            seq=seq, ts=float(seq)))
    return box


def test_it_finds_the_numbers_that_are_not_there(tmp_path):
    box = _log(tmp_path, [1, 2, 5, 6])
    try:
        assert box.gaps() == [3, 4]
    finally:
        box.close()


def test_a_complete_log_reports_nothing(tmp_path):
    """Empty is the healthy answer, and it must not be mistaken for a fault."""
    box = _log(tmp_path, [7, 8, 9])
    try:
        assert box.gaps() == []
    finally:
        box.close()


def test_an_empty_log_is_not_one_enormous_gap(tmp_path):
    box = Inbox(tmp_path)
    try:
        assert box.gaps() == []
    finally:
        box.close()


def test_a_log_that_starts_late_is_not_a_gap(tmp_path):
    """Joining a session in progress backfills from where you arrived.

    The first sequence this agent holds is wherever the room had got to, and
    counting from 1 would report every message said before it joined as lost.
    """
    box = _log(tmp_path, [400, 401, 402])
    try:
        assert box.gaps() == []
    finally:
        box.close()


def test_check_warns_about_it_and_does_not_fail(profile, monkeypatch):
    """The session works; what is wrong is the record of it.

    A fail would say «this session is not working», which is untrue and is how
    a check loop earns being ignored.
    """
    box = _log(profile.dir, [1, 2, 4])
    box.close()
    monkeypatch.setattr(cli, "_stats_health", lambda p: None)

    found = [row for row in cli._checks(profile) if row["check"] == "messages"]

    assert len(found) == 1, [r["check"] for r in cli._checks(profile)]
    assert found[0]["verdict"] == cli.CHECK_WARN
    assert "3" in found[0]["detail"]
    assert found[0]["fix"], "a warning without a next step is noise"


def test_check_is_silent_when_the_log_is_whole(profile, monkeypatch):
    box = _log(profile.dir, [1, 2, 3])
    box.close()
    monkeypatch.setattr(cli, "_stats_health", lambda p: None)

    assert not [r for r in cli._checks(profile) if r["check"] == "messages"]


def test_logs_reports_it(profile, monkeypatch, capsys):
    box = _log(profile.dir, [1, 2, 4, 5])
    box.close()
    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: False)
    monkeypatch.setenv("COLLAB_CONFIG", str(profile.dir / "cfg" / "config.json"))

    cli.cmd_logs(argparse.Namespace(lines=40, follow=False, session=None))

    out = capsys.readouterr().out
    assert "never arrived" in out
    assert "3" in out


def test_an_unreadable_inbox_does_not_take_the_report_down(profile, monkeypatch):
    """The hang log and the diagnostics are often what somebody came for.

    A database that is busy, locked, or written by a newer collab is not a
    reason to refuse the rest of the report.
    """
    monkeypatch.setattr("collab.client.inbox.Inbox",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    assert cli._missing_events(profile) == []


def test_reading_the_gaps_writes_nothing_at_all(tmp_path):
    """The regression its own sibling test caught.

    An ordinary `Inbox(...)` creates the directory, sets the journal mode, runs
    the schema and the migrations and commits — four writes before a row is
    read. `collab logs` promises every file it opens is opened read-only, and
    counting gaps broke that promise the first time it was wired up.
    """
    box = _log(tmp_path, [1, 2, 4])
    box.close()

    # THE RECORDS, NOT SQLITE'S SIDECARS. `-wal` and `-shm` are shared memory
    # that any reader of a WAL database brings into existence, this one and
    # `collab recv` and the viewer alike; in a live session the daemon is
    # holding them open already. What must not move is what the daemon WROTE.
    def records():
        return {p.name: (p.stat().st_mtime_ns, p.read_bytes())
                for p in sorted(tmp_path.rglob("*"))
                if p.is_file() and not p.name.endswith(("-wal", "-shm"))}

    before = records()
    ro = Inbox(tmp_path, readonly=True)
    try:
        assert ro.gaps() == [3]
    finally:
        ro.close()

    assert records() == before


def test_a_read_only_inbox_refuses_to_create_one(tmp_path):
    """No database is the right answer for a session that received nothing.

    Creating an empty one beside it would be a write, and would also invent a
    file the daemon then has to reconcile with.
    """
    with pytest.raises(Exception):
        Inbox(tmp_path / "never-used", readonly=True)
    assert not (tmp_path / "never-used").exists()


def test_a_read_only_inbox_cannot_write(tmp_path):
    """The guarantee, asserted rather than assumed."""
    box = _log(tmp_path, [1, 2])
    box.close()
    ro = Inbox(tmp_path, readonly=True)
    try:
        with pytest.raises(Exception):
            ro.record(Envelope(kind="chat", sender="a", text="x", seq=3, ts=3.0))
    finally:
        ro.close()
