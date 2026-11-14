"""Claude Code reports quota resets as epoch seconds; show them as a duration."""
import time

from collab.stats import _short_reset, quota_summary


def test_epoch_resets_read_as_a_duration():
    assert _short_reset(str(int(time.time()) + 3 * 3600 + 60)) == "3h"
    assert _short_reset(str(int(time.time()) - 5)) == "due"
    assert "(→3h)" in quota_summary({"quotas": {"5h": {"used_pct": 10,
                                                       "resets_at": str(int(time.time()) + 3 * 3600 + 60)}}},
                                    with_resets=True)


def test_hostile_reset_values_never_crash_a_roster_view():
    for value in ("²", "9" * 30, "99999999999999"):
        _short_reset(value)
        quota_summary({"quotas": {"5h": {"used_pct": 10, "resets_at": value}}}, with_resets=True)
