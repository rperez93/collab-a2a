"""Reading what a session recorded must not require ending the session.

Before this the only reader of the diagnostic record was `collab issue draft`,
which assembles a markdown file to post somewhere — the wrong shape entirely for
«what has it been doing for the last ten minutes», and the wrong shape for an
agent that wants to look at its own daemon without writing a bug report.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest

from collab import cli
from collab.client.daemon_files import DaemonPaths


@pytest.fixture(autouse=True)
def _its_own_hang_log(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "cfg" / "config.json"))


def _args(profile, **over):
    base = {"lines": 40, "follow": False, "session": None}
    base.update(over)
    return argparse.Namespace(**base)


def _record(profile, event, when=None):
    from collab import diagnostics as diag

    root = DaemonPaths(profile.dir).root
    path = diag.path_for(root, when or time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": when or time.time(), "proc": "daemon",
                             "event": event}) + "\n")


def test_it_reads_the_record_and_changes_nothing(profile, monkeypatch, capsys):
    """The whole promise: it is a read, and a live session is untouched."""
    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: True)
    _record(profile, "feed_dropped")
    _record(profile, "reconnected")
    root = DaemonPaths(profile.dir).root
    (root / "daemon.log").write_text("first line\nsecond line\n")
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    assert cli.cmd_logs(_args(profile)) == 0

    out = capsys.readouterr().out
    assert "feed_dropped" in out and "reconnected" in out
    assert "second line" in out
    after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert after == before, "reading the logs must not write to the session"


def test_lines_zero_means_none_of_them_rather_than_all_of_them(profile,
                                                               monkeypatch,
                                                               capsys):
    """`rows[-0:]` is `rows[0:]`, which is the whole file.

    The obvious spelling answers `--lines 0` with everything there is — the
    opposite of the request, and a day of diagnostics printed at somebody who
    asked for a summary.
    """
    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: True)
    for n in range(30):
        _record(profile, f"event_number_{n}")

    cli.cmd_logs(_args(profile, lines=0))

    out = capsys.readouterr().out
    # The COUNTS still name every event — zero lines is not zero information —
    # so the thing to assert absent is a rendered RECORD, which is the only
    # place the writing process's name appears.
    assert "daemon" not in out
    assert "30 record(s) on file" in out
    assert "event_number_0" in out, "the tally is still worth showing"


def test_a_negative_count_is_not_an_off_by_the_whole_file(profile, monkeypatch,
                                                          capsys):
    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: True)
    for n in range(5):
        _record(profile, f"event_number_{n}")

    cli.cmd_logs(_args(profile, lines=-3))

    out = capsys.readouterr().out
    assert "daemon" not in out, "a negative count must not print the whole file"


def test_it_says_the_record_is_off_rather_than_showing_nothing(profile,
                                                               monkeypatch,
                                                               capsys):
    """Silence and «there is nothing wrong» are different answers."""
    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: False)

    cli.cmd_logs(_args(profile))

    out = capsys.readouterr().out
    assert "off" in out
    assert "collab config diagnostics on" in out
    # AND THAT IT NEEDS NO RESTART, which is the fact somebody acting on this
    # line has to have: the setting is read live on every record written.
    assert "without a restart" in out


def test_a_hung_status_line_is_shown_even_with_the_record_off(profile,
                                                             monkeypatch,
                                                             capsys):
    """The hang log is written unconditionally, so it is read unconditionally.

    A hang is exactly the case where nobody turned diagnostics on beforehand,
    which is why it is not gated on them at either end.
    """
    from collab.statusline import watchdog

    monkeypatch.setattr(cli, "_optional_profile", lambda a: profile)
    monkeypatch.setattr("collab.config.diagnostics_enabled", lambda: False)
    import os

    path = watchdog.log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(
        "\n--- 2026-09-06T12:00:00+0000 pid 1 limit 5s cwd / argv --plain\n"
        "Timeout (0:00:05)!\n"
        "  File \"render.py\", line 1 in the_line_it_stopped_on\n")

    cli.cmd_logs(_args(profile))

    out = capsys.readouterr().out
    assert "hung and were stopped" in out
    assert "the_line_it_stopped_on" in out


def test_the_last_of_them_is_the_last_of_them():
    assert cli._last([1, 2, 3, 4], 2) == [3, 4]
    assert cli._last([1, 2, 3, 4], 0) == []
    assert cli._last([1, 2, 3, 4], -1) == []
    assert cli._last([1, 2], 99) == [1, 2]


def test_a_hang_is_shown_with_no_session_at_all(monkeypatch, capsys):
    """The person this command was written for may have no session open.

    `troubleshooting.md` sends somebody here when their status line has wedged,
    and requiring a profile first answered exactly them with «no active collab
    session» rather than the traceback they came for.
    """
    import os

    from collab.statusline import watchdog

    monkeypatch.setattr(cli, "_optional_profile", lambda a: None)
    path = watchdog.log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(
        "\n--- 2026-09-06T12:00:00+0000 pid 1 limit 5s cwd / argv --plain\n"
        "Timeout (0:00:05)!\n"
        "  File \"render.py\", line 1 in the_line_it_stopped_on\n")

    assert cli.cmd_logs(_args(None)) == 0

    out = capsys.readouterr().out
    assert "the_line_it_stopped_on" in out
    assert "no session in this checkout" in out
