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


# --- the clock beside the age -------------------------------------------------------

def _local_clock_of(epoch):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M")


def test_the_stats_row_says_the_time_as_well_as_the_age():
    """«4m ago» tells one reader how fresh it is; a room of people comparing
    notes need the moment itself. Both, in one bit: `reported 14:05 · 4m ago`."""
    when = time.time() - 240
    bits = cli._stat_bits(_person(240))
    last = bits[-1]
    assert _local_clock_of(when) in last, bits
    assert "4m ago" in last, bits


def test_a_stamp_from_another_day_carries_its_date():
    """The clock alone reads as today. Yesterday's 14:05 is not today's."""
    import datetime as _dt
    when = time.time() - 26 * 3600
    d = _dt.datetime.fromtimestamp(when)
    bits = cli._stat_bits(_person(26 * 3600))
    last = bits[-1]
    assert str(d.day) in last and statmod.MONTHS[d.month - 1] in last, bits
    assert _local_clock_of(when) in last, bits
    assert "old" in last, bits


def test_a_stamp_from_today_carries_no_date():
    bits = cli._stat_bits(_person(30))
    assert not any(m in bits[-1] for m in statmod.MONTHS), bits


def test_a_stamp_from_the_future_has_no_clock_either():
    """`reported_age` calls a stamp more than CLOCK_SKEW ahead unknown — a
    clock that disagrees with ours, not a report — and the row read «reported
    19:12 · age unknown»: a moment printed for a report whose age it could
    not tell. Whatever the age calls unknown has no moment to print."""
    ahead = time.time() + statmod.CLOCK_SKEW + 60
    figures = {"model": "x", "reported_at": ahead}
    assert statmod.reported_age(figures) == "age unknown"
    assert statmod.reported_when(figures) == ""
    assert cli._stat_bits({"stats": figures})[-1] == "age unknown"


def test_epoch_zero_is_not_a_report():
    """A Thursday in 1970 is what an unset field reads as, not a moment
    anybody reported at."""
    figures = {"model": "x", "reported_at": 0}
    assert statmod.reported_age(figures) == "age unknown"
    assert statmod.reported_when(figures) == ""
    assert cli._stat_bits({"stats": figures})[-1] == "age unknown"


def test_junk_or_missing_stamps_show_no_clock():
    """No stamp, no time — the same words as before, and nothing invented."""
    assert cli._stat_bits(_person(None))[-1] == "age unknown"
    for junk in ("lots", True, -5, "1e400"):
        person = _person(None)
        person["stats"]["reported_at"] = junk
        assert cli._stat_bits(person)[-1] == "age unknown", junk
        assert statmod.reported_when(person["stats"]) == ""
