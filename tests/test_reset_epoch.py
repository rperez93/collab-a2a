"""Quota reset values: one parsing policy, and no peer value can crash a view (#123)."""
import time
from datetime import datetime, timezone

import pytest

from collab.stats import MAX_RESET_EPOCH, _short_reset, collect_quotas, parse_reset, quota_summary


def test_epoch_seconds_read_as_a_duration():
    soon = int(time.time()) + 3 * 3600 + 60
    assert _short_reset(str(soon)) == "3h" and _short_reset(soon) == "3h"
    assert _short_reset(float(soon) + 0.5) == "3h", "finite decimals are seconds too"
    assert _short_reset(str(int(time.time()) - 5)) == "due"


def test_epoch_boundaries():
    assert parse_reset(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert parse_reset(MAX_RESET_EPOCH - 1) is not None
    for out_of_range in (-1, MAX_RESET_EPOCH, "-5", str(MAX_RESET_EPOCH)):
        assert parse_reset(out_of_range) is None, out_of_range
    # Milliseconds are never inferred from digit count.
    assert parse_reset(str(int(time.time() * 1000))) is None
    assert parse_reset(int(time.time() * 1000)) is None


def test_equivalent_instants_normalise_to_one_utc_time():
    instants = ["2030-01-01T00:00:00Z", "2030-01-01T05:30:00+05:30", "2029-12-31T19:00:00-0500",
                "2030-01-01T00:00:00.000000Z", "2030-01-01T00:00Z", 1893456000, "1893456000", 1893456000.0]
    parsed = {parse_reset(v) for v in instants}
    assert parsed == {datetime(2030, 1, 1, tzinfo=timezone.utc)}, parsed


def test_iso_fractions_are_kept_to_microseconds_and_truncated_beyond():
    when = parse_reset("2030-01-01T00:00:00.123456789Z")
    assert when.microsecond == 123456
    assert parse_reset("2030-01-01T00:00:00.5+01:00") == datetime(2029, 12, 31, 23, 0, 0, 500000, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [
    "2030-01-01T00:00:00",            # naive
    "2030-13-01T00:00:00Z",           # invalid month
    "tomorrow", "", "²", "9" * 30, "1e9", "0x10", " ", None, True, [], {},
    float("nan"), float("inf"), float("-inf"), "NaN", "Infinity",
])
def test_unknown_resets_render_as_unknown_never_raw(value):
    assert parse_reset(value) is None
    assert _short_reset(value) == "unknown"


def test_non_finite_used_pct_is_unknown_but_the_reset_survives():
    for bad in (float("nan"), "NaN", float("inf"), "+Infinity", "-Infinity"):
        windows = collect_quotas({"quotas": {"5h": {"used_pct": bad, "resets_at": "2030-01-01T00:00:00Z"},
                                             "7d": {"used_pct": 12, "resets_at": "1893456000"}}})
        assert "used_pct" not in windows["five_hour"] and windows["five_hour"]["resets_at"]
        assert windows["seven_day"]["used_pct"] == 12


HOSTILE = ["²", "9" * 30, "99999999999999", "2030-01-01T00:00:00", float("nan"), -1, "\x1b[2J", "1" * 200]


def _stats(reset):
    return {"quotas": {"5h": {"used_pct": 10, "resets_at": reset}}, "observed_at": time.time()}


def test_no_hostile_reset_crashes_the_roster_stats_or_dashboard():
    from collab.client import participant_metrics, statusbar, tui
    for reset in HOSTILE:
        line = quota_summary(_stats(reset), with_resets=True)
        assert "(→unknown)" in line and "\x1b" not in line
        person = {"name": "bob", "stats": _stats(reset)}
        participant_metrics.metric_lines(person, ["quota"], detailed=True)
        assert "(→unknown)" in tui.quota_text(_stats(reset))
        statusbar_bits = quota_summary(_stats(reset))
        assert "%" in statusbar_bits



@pytest.mark.parametrize("bad", [float("nan"), "nan", float("inf"), "-inf", 1e308, 100.5, -3, "lots", True, None])
def test_an_unusable_used_pct_is_skipped_never_drawn(bad):
    """«quota 5h nan%» and «999%» reached the roster when quotas bypassed collect_quotas."""
    stats = {"quotas": {"5h": {"used_pct": bad, "resets_at": "2030-01-01T00:00:00Z"},
                        "7d": {"used_pct": 12}}}
    line = quota_summary(stats, with_resets=True)
    assert line == "quota 7d 12%", line
    assert quota_summary({"quotas": {"5h": {"used_pct": bad}}}) == ""
    assert quota_summary({"quota_used_pct": bad}) == ""
    assert quota_summary({"quota_used_pct": 100}) == "quota 100%"
