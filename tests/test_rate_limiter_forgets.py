"""The /join limiter remembers callers for as long as it is limiting them, and no longer.

It was a `defaultdict(deque)`: `allow()` trimmed the TIMESTAMPS out of a
caller's deque once they aged past the window, but the caller's KEY stayed for
the life of the hub. A tunnelled hub's /join is reachable from the whole
internet, and every scanner that probed it once left a permanent entry —
measured at 14.9 MiB after 20,000 distinct addresses, growing linearly with no
ceiling, in a process that is meant to run for hours.
"""

from __future__ import annotations

import types

import pytest

from collab.server import auth
from collab.server.auth import RateLimiter


@pytest.fixture()
def clock(monkeypatch):
    """A clock the test moves by hand, seen by the limiter as `time.time()`."""
    now = [1_000.0]
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(time=lambda: now[0]))
    return now


def test_callers_who_stopped_calling_are_forgotten(clock):
    limiter = RateLimiter(limit=10, window=60.0)
    for i in range(5_000):
        assert limiter.allow(f"10.0.{i // 256}.{i % 256}")
    assert len(limiter._hits) == 5_000, "within the window every caller is on record"

    clock[0] += 61.0
    limiter.allow("the-next-one")

    # Everyone whose last attempt is older than the window is gone — not just
    # the key that happened to call. A key that stops calling never gets
    # another `allow()` of its own to be trimmed by.
    assert len(limiter._hits) <= 2


def test_a_denied_attempt_leaves_no_entry_behind(clock):
    limiter = RateLimiter(limit=1, window=60.0)
    assert limiter.allow("attacker")
    assert not limiter.allow("attacker")
    clock[0] += 61.0
    limiter.allow("somebody-else")
    assert "attacker" not in limiter._hits


def test_the_sweep_does_not_reset_a_caller_still_inside_the_window(clock):
    """Forgetting the idle must not free the busy.

    A sweep that dropped every key would hand an attacker a fresh count every
    minute. Only attempts older than the window are forgotten, so a caller who
    hit the limit twelve seconds ago is still at the limit after the sweep.
    """
    limiter = RateLimiter(limit=10, window=60.0)
    clock[0] += 50.0
    for _ in range(10):
        assert limiter.allow("attacker")
    assert not limiter.allow("attacker")

    clock[0] += 12.0                      # 62 s since construction: a sweep is due
    assert limiter.allow("bystander")     # and this call runs it
    assert not limiter.allow("attacker"), "the sweep reset an active caller"

    clock[0] += 50.0                      # 112 s: the attacker's attempts have aged out
    assert limiter.allow("attacker")
