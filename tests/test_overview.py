"""`collab` with no arguments has to print the list of commands.

It is the first thing anybody runs, agent or person — every skill promises it
«lists every command» — and it ended in a traceback halfway through, because
one row of the table carried three fields where the loop unpacks two:

    ("kill", "lock", "end a session (its history is kept)")

A missed comma between two entries. Nothing tested the listing, so nothing
noticed, and it shipped that way: an agent orienting itself with the command
the instructions name first saw a Python stack trace.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from collab import cli


def _overview(monkeypatch):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda cls: None))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.print_overview()
    return out.getvalue()


def test_it_prints_without_raising(monkeypatch):
    assert "collab" in _overview(monkeypatch)


def test_every_group_and_row_reaches_the_page(monkeypatch):
    """Halfway through is not printed: the traceback took the rest with it."""
    text = _overview(monkeypatch)
    for title, entries in cli.COMMAND_GROUPS:
        assert title in text, f"group missing: {title}"
        for entry in entries:
            assert entry[0] in text, f"command missing: {entry[0]}"


def test_the_table_itself_is_well_formed():
    """The guard in the printer is a seatbelt; the data is the fix."""
    for _title, entries in cli.COMMAND_GROUPS:
        for entry in entries:
            assert len(entry) == 2, f"{entry[0]!r} has {len(entry)} fields, not 2"


def test_a_malformed_row_still_prints_the_rest(monkeypatch):
    """Whatever gets edited in here later, the listing must survive it."""
    broken = [("Group", [("one", "two", "the blurb"), ("after", "still printed")])]
    monkeypatch.setattr(cli, "COMMAND_GROUPS", broken)

    text = _overview(monkeypatch)
    assert "after" in text and "still printed" in text


def test_the_local_join_is_named_first(monkeypatch):
    """An agent with no link needs to meet `join` before it meets a URL.

    Both agents on one machine is the ordinary case; asking the user for a
    link is the one step in that flow that needs a person.
    """
    text = _overview(monkeypatch)
    bare = text.index("\n    join ")
    with_url = text.index("join <url>")
    assert bare < with_url
    assert "no link needed" in text
