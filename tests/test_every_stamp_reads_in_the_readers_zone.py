"""Two renderers of one stamp, one zone — and no stamp that can raise.

`collab config timezone` pins the zone the transcript and the viewer read
stamps in. The `collab stats` row's «reported 14:05» took its clock from the
same conversion but judged «today» and built its date with a bare
`.astimezone()` — the machine's zone — so a reader who had pinned a zone saw
the transcript honour it and the stats row silently ignore it. Same stamp, two
answers, an hour either side of midnight.

And `local_datetime` converts with `.astimezone(zone)`, which raises
`OverflowError` for a stamp at the edge of the calendar. No wire stamp gets
near it; a hostile one can, and this runs on the draw path of a curses program.
"""

from __future__ import annotations

import datetime as _dt
import time

import pytest

from collab import config, protocol
from collab.client import tui


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def test_the_stats_row_dates_a_stamp_in_the_configured_zone():
    """23:30 UTC on the 2nd is the 3rd in Kiritimati — and «today» there."""
    config.set_timezone("Pacific/Kiritimati")            # UTC+14
    stamp = "2026-09-02T23:30:00Z"
    local = protocol.local_datetime(stamp)
    assert local.date() == _dt.date(2026, 9, 3)
    # Judged against the configured zone's today, the date is dropped.
    out = protocol.local_day_clock(stamp, today=_dt.date(2026, 9, 3))
    assert out == local.strftime("%H:%M"), out
    # And against another day it is spelled in that zone's calendar, not UTC's.
    out = protocol.local_day_clock(stamp, today=_dt.date(2026, 9, 4))
    assert out.startswith("3 sep "), out


def test_the_stats_row_and_the_transcript_agree_on_the_day():
    config.set_timezone("America/Bogota")                 # UTC-5
    stamp = "2026-09-03T02:30:00Z"                        # still the 2nd there
    day_in_transcript = protocol.local_datetime(stamp).date()
    out = protocol.local_day_clock(stamp, today=_dt.date(2026, 9, 3))
    assert out.startswith(f"{day_in_transcript.day} sep "), out


def test_local_day_clock_asks_local_today_not_the_machine(monkeypatch):
    """With no `today` given, the comparison must be in the reading zone.

    A 2030 stamp, so that the machine's own today can never coincide with the
    day under test: the first version of this used a stamp from the day it was
    written and passed against `datetime.now().date()` by luck of the calendar.
    """
    config.set_timezone("Pacific/Kiritimati")
    stamp = "2029-12-31T23:30:00Z"                        # 1 Jan 2030 there
    monkeypatch.setattr(protocol, "local_today", lambda: _dt.date(2030, 1, 1))
    assert protocol.local_day_clock(stamp) == protocol.local_clock(stamp)


@pytest.mark.parametrize("stamp", [
    "9999-12-31T23:59:59Z", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59+14:00",
])
@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Europe/Madrid",
                                  "America/Bogota"])
def test_a_stamp_at_the_edge_of_the_calendar_never_raises(stamp, zone):
    config.set_timezone(zone)
    for render in (protocol.local_datetime, protocol.local_clock,
                   protocol.local_day_clock):
        render(stamp)                                    # must not raise
    assert isinstance(protocol.local_clock(stamp), str)


def test_one_month_table():
    """Two copies drift; the viewer reads the protocol's."""
    assert tui.MONTHS is protocol.MONTHS
