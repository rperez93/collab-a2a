"""The message count on the roster row, at the exact edge of being a memory.

That count is the one figure on screen claiming to be the same for everybody:
the hub's own `COUNT(*)` over its log, taken once and copied out on the
snapshot. Which means it has the failure every shared figure in this project
has had — `write_status` keeps writing every three seconds after the hub has
gone quiet, so the number freezes while looking live, and a frozen count is
indistinguishable from a quiet room.

`batch.is_stale` is what stops that, and the tests below are about its EDGES
rather than its middle. The middle is already covered where the batch bar is
tested; what was never written down is what happens at the threshold itself,
with no stamp at all, and with a stamp from the future — three inputs that each
have a plausible-looking wrong answer, and each of which draws a number nobody
can vouch for.

And the same fact is now said where somebody can act on it. The viewer marks
the count with its age, which the person watching can see and do nothing about;
`collab check` names the process that stopped refreshing it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time

import pytest

from collab import batch as batch_progress, cli
from collab.client import statusbar as sb
from collab.config import SessionProfile

STALE = batch_progress.STALE_AFTER


def _segment(figures, *, now, narrow=False):
    return sb.messages_segment(figures, now=now, narrow=narrow)


# --- the edges of the window --------------------------------------------------

def test_just_inside_the_window_the_count_is_the_count():
    now = time.time()
    figures = {"total": 128, "fetched_at": now - (STALE - 1)}
    assert _segment(figures, now=now) == "128 messages"


def test_at_the_threshold_itself_it_is_already_a_memory():
    """The window is «how long a figure may be trusted», so at the end of it
    the figure may no longer be true. Where the two readings of the boundary
    differ by one instant, the one that draws a possibly-wrong shared number is
    not the one to keep."""
    now = time.time()
    figures = {"total": 128, "fetched_at": now - STALE}
    assert _segment(figures, now=now) == f"messages ? {int(STALE)}s old"
    assert batch_progress.is_stale(figures, now=now)


def test_past_the_threshold_it_says_how_old_it_is():
    now = time.time()
    figures = {"total": 128, "fetched_at": now - 3600}
    assert _segment(figures, now=now) == "messages ? 1h old"
    assert "128" not in _segment(figures, now=now), "no remembered number"


def test_an_unstamped_count_is_a_memory_of_unknown_age():
    """A payload that reached us by some path which forgot to stamp it has an
    age nobody can vouch for, and unknown age is not evidence of youth. There
    is no age to print, so the segment says unknown and stops."""
    now = time.time()
    assert _segment({"total": 128}, now=now) == "messages ?"
    assert _segment({"total": 128, "fetched_at": None}, now=now) == "messages ?"


def test_a_stamp_from_the_future_is_not_a_fresh_one():
    """NTP correcting, a VM resuming, a container syncing: the stamp lands
    ahead of now and the subtraction goes negative. An age that cannot be
    computed is an age that cannot be vouched for, so the count is marked
    rather than drawn — the same answer `batch.is_stale` gives the bar, and for
    the same reason. Two figures on one row disagreeing about what a backward
    clock means is worse than either answer.
    """
    now = time.time()
    figures = {"total": 128, "fetched_at": now + 3600}
    assert batch_progress.is_stale(figures, now=now)
    assert _segment(figures, now=now) == "messages ?"
    assert "128" not in _segment(figures, now=now)


def test_the_narrow_form_marks_staleness_too():
    """A pane too narrow for the word still may not draw a memory plainly."""
    now = time.time()
    old = {"total": 128, "fetched_at": now - 300}
    assert _segment(old, now=now, narrow=True).startswith("msgs ?")
    fresh = {"total": 128, "fetched_at": now - 1}
    assert _segment(fresh, now=now, narrow=True) == "128 msgs"


# --- and where somebody can act on it -----------------------------------------

@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A live daemon with a session, as `test_check` builds one."""
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home),
                             participant_id="p_bob")
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread": 0}))
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: ["w"])
    return profile


def _status(profile, **changes):
    body = json.loads((profile.dir / "status.json").read_text())
    body.update(changes)
    (profile.dir / "status.json").write_text(json.dumps(body))


def _checks(profile):
    return {r["check"]: r for r in cli._checks(profile)}


def test_a_frozen_count_is_reported_by_the_process_responsible(session):
    """The viewer can only say «this is 4m old». The command can say which
    process stopped refreshing it, which is the half somebody can act on."""
    _status(session, messages={"total": 128, "fetched_at": time.time() - 300})
    found = _checks(session)["count"]
    assert found["verdict"] == cli.CHECK_WARN
    assert "5m old" in found["detail"]
    assert "has not refreshed the snapshot" in found["detail"]
    assert found["fix"], "a warning without its fix is a scolding"


def test_a_current_count_says_nothing_at_all(session):
    """This loop is silent when there is nothing to do; a line per run is how
    a check stops being read."""
    _status(session, messages={"total": 128, "fetched_at": time.time()})
    assert "count" not in _checks(session)


def test_a_daemon_with_no_snapshot_yet_is_starting_up_not_stuck(session):
    """A count that has never arrived is not a count that stopped."""
    assert "count" not in _checks(session)
    _status(session, messages=None)
    assert "count" not in _checks(session)


def test_a_daemon_that_is_not_live_is_reported_once_and_not_twice(session, monkeypatch):
    """The listener check has already said the daemon is down. Saying it again
    in different words sends somebody looking for a second fault."""
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    _status(session, messages={"total": 128, "fetched_at": time.time() - 300})
    found = _checks(session)
    assert found["listener"]["verdict"] == cli.CHECK_FAIL
    assert "count" not in found


def test_an_unstamped_count_is_reported_as_unstamped_and_not_as_an_age(session):
    """«0s old» about a figure with no stamp would be a reading invented to
    fill the sentence."""
    _status(session, messages={"total": 128})
    found = _checks(session)["count"]
    assert "not stamped" in found["detail"]
    assert " old —" not in found["detail"]


def _run(**flags):
    args = argparse.Namespace(**{"json": False, "verbose": False,
                                 "session": None, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.cmd_check(args)
    return code, out.getvalue()


def test_the_warning_reaches_the_printed_output_and_does_not_fail_the_run(session):
    """A stale count is worth saying and is not a reason to exit non-zero: the
    hub may simply be slow, and a hook keyed on failure should not fire for it.
    """
    _status(session, messages={"total": 128, "fetched_at": time.time() - 300})
    code, out = _run()
    assert "message count" in out
    assert code == 0
