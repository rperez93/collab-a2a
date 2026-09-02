"""The viewer's line for a collected file says what the ack did — in the
protocol's words, not its own.

A room file is not gone when one person has it: the `received` event says how
many are still to collect, and `protocol.file_outcome` is the one wording for
it. `watch` already reads from there. The viewer spelt «deleted from the host»
out by hand, so the moment the count arrived the transcript and the pane would
have disagreed about whether the host still held the file.
"""

from __future__ import annotations

import pytest

from collab import protocol
from collab.client import tui
from collab.protocol import KIND_FILE, Envelope


def _received(**body) -> Envelope:
    return Envelope(kind=KIND_FILE, sender="bob",
                    body={"action": "received", "name": "build.tar.gz", **body})


def _line(env: Envelope) -> str:
    return " ".join(tui._body_lines(env, 200))


def test_the_viewer_takes_the_wording_from_the_protocol(monkeypatch):
    """Whatever the protocol says, the viewer says — so the two cannot drift."""
    monkeypatch.setattr(tui, "file_outcome",
                        lambda body: f"<what happened to {body['name']}>")
    assert _line(_received()) == \
        "collected build.tar.gz (<what happened to build.tar.gz>)"


def test_an_event_from_before_the_count_reads_as_the_deletion_it_was():
    """A hub that predates the count sends none of the keys; it only ever
    deleted, and the line still says so."""
    assert _line(_received()) == "collected build.tar.gz (deleted from the host)"


@pytest.mark.skipif(not hasattr(protocol, "file_outcome"),
                    reason="protocol.file_outcome lands with the files-and-local branch")
def test_a_room_file_still_awaited_says_who_is_yet_to_collect():
    """The new shape, once both branches are in: the same words `watch` prints."""
    line = _line(_received(by="bob", room="general", collected=["bob"],
                           remaining=2, awaiting=["carol", "dave"], deleted=False))
    assert line == "collected build.tar.gz (2 still to collect (carol, dave))"
