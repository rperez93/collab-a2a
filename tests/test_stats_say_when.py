"""A quota reading is a fact about a moment, and the row has to say which.

`collab stats` printed «5h 91 %» with nothing about when that was reported, so
a reading from three hours ago read exactly like one from just now — and the two
call for opposite decisions about who takes the next task. The hub now stamps
each participant's usage with the moment it arrived; every place that prints
the figures prints the age beside them, and past a threshold says plainly that
the figures are old.
"""

from __future__ import annotations

import time

from collab import cli, stats as statmod
from collab.client import tui


def _person(age_seconds, **stats):
    figures = {"model": "x", "quota_five_hour": 91.0, **stats}
    if age_seconds is not None:
        figures["reported_at"] = time.time() - age_seconds
    return {"name": "bob", "connected": True, "stats": figures}


# --- the words -------------------------------------------------------------------

def test_a_fresh_report_is_dated_in_seconds_or_minutes():
    assert statmod.reported_age(_person(5)["stats"]) == "5s ago"
    assert statmod.reported_age(_person(240)["stats"]) == "4m ago"


def test_an_old_report_is_called_old_not_merely_dated():
    """Past the threshold, the age alone is not enough — «3h ago» beside a
    quota figure still reads as a quota figure. The word does the work."""
    text = statmod.reported_age(_person(3 * 3600)["stats"])
    assert "3h ago" in text and "old" in text


def test_no_stamp_means_unknown_not_fresh():
    """A report from a hub that predates the stamp is of unknown age. Saying
    nothing would read as current; it is the older figure, not the newer."""
    text = statmod.reported_age(_person(None)["stats"])
    assert text and "unknown" in text


def test_junk_stamps_never_raise():
    for junk in ("lots", None, [], {}, True, -5, 1e400, "1e400"):
        statmod.reported_age({"reported_at": junk})   # must not raise


# --- collab stats ------------------------------------------------------------------

def test_the_stats_row_carries_the_age():
    bits = cli._stat_bits(_person(120))
    assert any("2m ago" in b for b in bits), bits


def test_the_stats_row_says_old_past_the_threshold():
    bits = cli._stat_bits(_person(statmod.STATS_STALE_AFTER + 60))
    assert any("old" in b for b in bits), bits


def test_the_stats_row_says_unknown_for_an_unstamped_report():
    bits = cli._stat_bits(_person(None))
    assert any("unknown" in b for b in bits), bits


# --- the roster ----------------------------------------------------------------------

def test_the_roster_line_dates_a_stale_report():
    line = tui.stat_line(_person(statmod.STATS_STALE_AFTER + 60))
    assert "old" in line, line


def test_the_roster_line_leaves_a_fresh_report_alone():
    """The roster is narrow; a fresh figure needs no annotation there. The
    full `collab stats` output carries the age for every row."""
    line = tui.stat_line(_person(30))
    assert "ago" not in line and "old" not in line, line


def test_the_roster_line_marks_an_unstamped_report():
    line = tui.stat_line(_person(None))
    assert "unknown" in line, line
