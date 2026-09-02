"""Two edges of the stamp where a true reading and a false one shared a label.

A stamp a few seconds in the FUTURE is the freshest report there is, off a hub
whose clock runs slightly ahead of this machine's — and it read «age unknown»,
the same words as a hub that never stamped at all. A report that sanitises to
NOTHING — all nested junk — was stamped anyway, so a participant who told us
nothing usable grew a stats dict of one key, flipped the «nobody has shared any
usage yet» banner, and half an hour later showed a bare «31m ago — old» on the
roster: «this agent's data is stale», where the truth is «this agent never told
us anything».
"""

from __future__ import annotations

import time

import pytest

from collab import stats as statmod
from collab.client import tui


# --- clock skew ------------------------------------------------------------------

@pytest.mark.parametrize("ahead", [0.5, 2, 4.9])
def test_a_stamp_slightly_in_the_future_is_now_not_unknown(ahead):
    now = time.time()
    text = statmod.reported_age({"reported_at": now + ahead}, now=now)
    assert text == "0s ago", text


def test_a_stamp_well_in_the_future_is_still_unknown():
    """Beyond skew, it is a clock that disagrees with ours, not a fresh report."""
    now = time.time()
    assert statmod.reported_age({"reported_at": now + 120}, now=now) == "age unknown"


def test_a_bool_stamp_renders_as_unknown():
    """`True` is 1.0 to `float()`; it is not a moment in 1970 either."""
    assert statmod.reported_age({"reported_at": True}) == "age unknown"
    assert statmod.reported_age({"reported_at": False}) == "age unknown"


# --- a report of nothing -----------------------------------------------------------

def test_a_report_with_nothing_usable_is_not_stamped(client, session, host_headers):
    from tests.test_stats import _headers, _join, _person
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "x", "stats": {"junk": {"a": 1}, "more": [1, 2]}})
    stats = _person(client, host_headers, "bob").get("stats") or {}
    assert "reported_at" not in stats, stats
    assert not stats, "a participant who reported nothing grew a stats dict"


def test_the_roster_shows_nothing_for_a_stamp_alone():
    """Belt and braces below the hub: a dict of one key is not figures."""
    line = tui.stat_line({"name": "bob", "stats": {"reported_at": time.time() - 3600}})
    assert line == "", line
