"""`file_outcome` is fed by the hub and drawn by every renderer.

The body of a `received` event is a remote party's choice of values, and the
words this function makes go into the transcript, the watch pane, the viewer and
`collab file get` — none of which wrap the call. `int(body.get("remaining") or
0)` raised on `"lots"`, so a hub that sent junk here did not garble one line, it
took down whichever renderer was reading it.

A count that is not a count says nothing about how many remain; it does not
raise, and it does not invent a number.
"""

from __future__ import annotations

import pytest

from collab.protocol import file_outcome


@pytest.mark.parametrize("remaining", [
    "lots", "", "3", 3.5, True, False, -1, [2], {"n": 2}, None, 10**30,
])
def test_junk_remaining_never_raises(remaining):
    out = file_outcome({"remaining": remaining, "awaiting": ["bob"]})
    assert isinstance(out, str) and out


@pytest.mark.parametrize("awaiting", [
    "carol", 7, None, {"a": 1}, [None, 3, {"x": 1}], "a\x1b[31mb",
])
def test_junk_awaiting_never_raises(awaiting):
    out = file_outcome({"remaining": 2, "awaiting": awaiting})
    assert isinstance(out, str) and "2 still to collect" in out
    assert "\x1b" not in out, "a control character reached the row"


def test_a_name_with_an_escape_in_it_is_scrubbed():
    """A LIST holding the hostile name — a bare string is nobody and never
    reaches the scrub, which is how the first version of this test passed
    with the scrub deleted."""
    out = file_outcome({"remaining": 1, "awaiting": ["a\x1b[31mb", "carol"]})
    assert "\x1b" not in out, out
    assert "carol" in out


def test_a_real_count_still_reads_as_one():
    out = file_outcome({"remaining": 2, "awaiting": ["carol", "dave"]})
    assert out == "2 still to collect (carol, dave)"


def test_a_bool_is_not_a_count_of_anything():
    """`True` is an int to `int()`; it is not one person still to collect."""
    out = file_outcome({"remaining": True, "awaiting": ["bob"]})
    assert "1 still" not in out


def test_an_old_event_without_the_keys_still_reads_as_a_deletion():
    assert file_outcome({}) == "deleted from the host"
    assert file_outcome({"deleted": True}) == "deleted from the host"
