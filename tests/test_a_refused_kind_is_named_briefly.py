"""A refused kind is named in the refusal — but not at any length.

Review of the kind check fed it a ten-megabyte kind and a kind full of control
bytes. The `!r` already neutralises the bytes; the length went straight into
the response and the host's own log. Refusal never publishes, so no other
participant sees it — it is the host's log that pays, and a refusal is not a
place to pay for the attacker's imagination.
"""
from __future__ import annotations

from collab.protocol import client_kind_refusal

#: The most a refusal will repeat back. Generous for any real kind (the longest
#: is eight characters) and small against a log line.
CAP = 80


def test_a_kind_of_ordinary_length_is_named_in_full():
    reason = client_kind_refusal("system")
    assert reason is not None
    assert "'system'" in reason


def test_a_ten_megabyte_kind_is_not_repeated_back():
    huge = "x" * (10 * 1024 * 1024)
    reason = client_kind_refusal(huge)
    assert reason is not None
    assert len(reason) <= CAP + len("kind  refused: chat is the only kind a client may send") + 8, \
        f"the refusal is {len(reason)} bytes long"


def test_a_clipped_kind_still_says_it_was_clipped():
    long = "k" * 500
    reason = client_kind_refusal(long)
    assert reason is not None
    assert "…" in reason or "..." in reason, "a clipped kind should look clipped"


def test_control_bytes_stay_neutralised_after_the_cap():
    nasty = "\x1b[31m" + "A" * 300 + "\n\r\x00"
    reason = client_kind_refusal(nasty)
    assert reason is not None
    assert "\x1b" not in reason and "\n" not in reason and "\x00" not in reason
