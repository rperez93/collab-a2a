"""One command an agent can loop on to prove it is still collaborating.

Arming a watcher once and forgetting it is the failure the whole listening
section exists to prevent, and nothing tells you when it stops. No single thing
answers it either: the daemon can be live while nothing reads the feed, the feed
can be read while nobody acts on it, and an agent can act on everything while
never saying what it is doing.

So each is asked separately, each answer carries the command that fixes it, and
the whole thing is SILENT when there is nothing to do — a loop that reports
success fills the context it exists to protect, and then the agent stops reading
it, which is the same as not running it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time

import pytest

from collab import activity, cli
from collab.client import daemon as d
from collab.config import SessionProfile


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    """A throwaway global config, never the machine's own.

    `collab check` reads it: the standing reminder's interval lives there, and
    the reminder check now reports the route that will carry it whenever
    somebody has configured one. Without this, whether these tests pass depends
    on what the person running them happens to have in their own config — the
    isolation `test_wake.py` already takes for the same reason.
    """
    from collab import config as cfg

    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    yield
    cfg._CACHE.clear()


@pytest.fixture()
def session(tmp_path, monkeypatch):
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
    return profile


def _status(profile, **changes):
    body = json.loads((profile.dir / "status.json").read_text())
    body.update(changes)
    (profile.dir / "status.json").write_text(json.dumps(body))


def _run(**flags):
    fields = {"json": False, "verbose": False, "session": None, **flags}
    args = argparse.Namespace(**fields)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.cmd_check(args)
    return code, out.getvalue()


def _published(profile, doing):
    """What the roster says about us — the daemon's snapshot on disk."""
    (profile.dir / "snapshot.json").write_text(json.dumps({"participants": [
        {"id": profile.participant_id, "name": profile.name,
         "connected": True, "activity": doing}]}))


def _healthy(profile):
    """Listening, nothing waiting, and having said what it is doing — where
    «said» means the roster knows, not merely that we wrote it down."""
    doing = activity.sanitise({"state": "working", "what": "the client side"})
    activity.write_local(profile, doing)
    _published(profile, doing)
    return d.watching(profile)


# --- silence is the whole point ---------------------------------------------

def test_it_says_nothing_at_all_when_there_is_nothing_to_do(session):
    with _healthy(session):
        code, out = _run()

    assert code == 0
    assert out == "", "a loop that reports success fills the context it protects"


def test_but_verbose_shows_the_working_for_a_person(session):
    with _healthy(session):
        code, out = _run(verbose=True)

    assert code == 0
    assert "listener" in out and "watching" in out and "activity" in out


# --- and what it says when something is wrong -------------------------------

def test_nothing_reading_is_a_failure_with_an_exit_code(session):
    activity.write_local(session, activity.sanitise({"state": "working", "what": "x"}))

    code, out = _run()

    assert code == 1, "a hook or a timer can carry a non-zero exit"
    assert "nothing is reading this session" in out
    assert "listen --follow" in out, "and what to do about it"


def test_undrained_messages_are_reported_when_nothing_is_streaming(session):
    """Only then does unread mean «arrived, and you have not taken it»."""
    _status(session, unread_messages=3)
    activity.write_local(session, activity.sanitise({"state": "idle"}))
    _published(session, activity.sanitise({"state": "idle"}))
    d.polled(session)

    code, out = _run()

    assert "3 undrained" in out
    assert "recv" in out and "DO what they ask" in out


def test_an_armed_watcher_is_never_nagged_about_unread(session):
    """THE BUG THIS FEATURE SHIPPED WITH. Nothing but `recv` marks a row read —
    `listen --follow` streams the log and never touches the table — so an agent
    doing exactly what the skills prescribe accumulated unread for ever, was
    scolded on every iteration of the loop, and was told to run `recv`, which
    would have made it act twice on messages it had already handled."""
    _status(session, unread=12, unread_messages=12)

    with _healthy(session):
        code, out = _run()

    assert (code, out) == (0, ""), "the prescribed setup must be silent"


def test_somebody_arriving_is_not_a_message_you_ignored(session):
    """`unread` counts every kind — joins, renames, file notices. One agent
    walking in produced «1 unread — nobody has acted on them», with nothing
    whatever to act on."""
    _status(session, unread=1, unread_messages=0)
    activity.write_local(session, activity.sanitise({"state": "idle"}))
    _published(session, activity.sanitise({"state": "idle"}))
    d.polled(session)

    code, out = _run()

    assert code == 0
    assert "undrained" not in out, "an arrival is not something to answer"


def test_saying_nothing_about_your_work_is_worth_a_line(session):
    with d.watching(session):
        code, out = _run()

    assert "you have not said what you are doing" in out
    assert "working" in out


def test_an_activity_that_never_reached_the_roster_is_not_a_pass(session):
    """The local file is what we MEANT to publish; the question is what the
    others can see. They differ exactly when publishing failed."""
    activity.write_local(session, activity.sanitise(
        {"state": "working", "what": "the client side"}))

    with d.watching(session):
        code, out = _run()

    assert "not on the roster" in out
    assert code == 0, "it is a warning: the listener republishes on reconnect"


def test_a_wedged_listener_fails_rather_than_warns(session):
    """Its pid is alive and its heartbeat is a day old: a hung process, which
    does not retry itself out of it. As a warning this exited 0, so a hook
    keyed on failure never fired for the one state that needs a human."""
    _status(session, heartbeat=time.time() - 86400)

    with _healthy(session):
        code, out = _run()

    assert code == 1
    assert "not beating" in out
    assert "daemon stop" in out


def test_the_session_flag_is_obeyed(session, monkeypatch, tmp_path):
    """It is offered on this command, so reading the current session while
    being told to read another checks the wrong one and says nothing of it."""
    import argparse
    import contextlib as ctx
    import io as _io

    out = _io.StringIO()
    with ctx.redirect_stdout(out):
        code = cli.cmd_check(argparse.Namespace(json=True, verbose=False,
                                                session="s_somebody_else"))

    assert code == 1
    assert "s_somebody_else" in out.getvalue()


def test_a_warning_alone_is_not_a_failure(session):
    """`fail` is «this session is not working»; `warn` is «you are not holding
    up your end». A loop that treats an unanswered message as a crash stops
    being run."""
    _status(session, unread_messages=2)
    activity.write_local(session, activity.sanitise({"state": "idle"}))
    _published(session, activity.sanitise({"state": "idle"}))
    d.polled(session)

    code, out = _run()

    assert code == 0
    assert "2 undrained" in out, "it still says so"


def test_a_dead_listener_is_a_failure(session, monkeypatch):
    monkeypatch.setattr(cli, "is_running", lambda p: None)

    code, out = _run()

    assert code == 1
    assert "listener is not running" in out
    assert "daemon start" in out


def test_polling_passes_but_says_what_it_is(session):
    d.polled(session)
    activity.write_local(session, activity.sanitise({"state": "idle"}))

    code, out = _run()

    assert code == 0, "polling is the documented fallback, not a fault"
    assert "polling" in out


def test_no_session_at_all_fails_with_the_way_out(session, monkeypatch, capsys):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: None))

    assert cli.cmd_check(argparse.Namespace(json=False, verbose=False,
                                            session=None)) == 1
    assert "join" in capsys.readouterr().out


# --- for a hook rather than a reader ----------------------------------------

def test_json_is_always_printed_and_carries_the_verdicts(session):
    with _healthy(session):
        code, out = _run(json=True)

    payload = json.loads(out)
    assert payload["ok"] is True and payload["verdict"] == "ok"
    assert {c["check"] for c in payload["checks"]} == {
        "listener", "watching", "acting", "activity"}


def test_json_says_which_one_failed(session):
    code, out = _run(json=True)

    payload = json.loads(out)
    assert payload["ok"] is False
    broken = [c for c in payload["checks"] if c["verdict"] == "fail"]
    assert [c["check"] for c in broken] == ["watching"]
    assert broken[0]["fix"], "every failure carries its own fix"


# --- the figures the room splits work by ------------------------------------
#
# Every route that produces usage figures is silent when it fails: the status
# line only exists if somebody installed it, the pull command only runs if
# somebody configured one, and reporting by hand only happens while an agent
# remembers. So a roster fills with agents whose usage is blank, the feature
# quietly stops working, and this loop — which asks about everything else —
# never asked about this at all. It applies to Claude Code exactly as much as
# to anything else: it is not woken, but it is not exempt from being seen.

def test_an_agent_that_never_reported_is_not_nagged(session, monkeypatch):
    """No figures ever means no reporting was set up — a decision somebody
    made or did not make at install time, not a fault. Warning about it every
    few turns is exactly the noise that gets this loop ignored."""
    monkeypatch.setattr(cli, "share_stats_enabled", lambda: True)
    results = {r["check"]: r for r in cli._checks(session)}
    assert "stats" not in results


def test_figures_from_hours_ago_are_not_current(session, monkeypatch):
    from collab import stats as st

    monkeypatch.setattr(cli, "share_stats_enabled", lambda: True)
    monkeypatch.setattr(cli, "stats_source", lambda: ("mycmd", 120))
    st.write_stats(session, {"quota_used_pct": 40})
    old = time.time() - cli.STATS_ARE_HISTORY - 60
    os.utime(session.dir / st.STATS_FILE, (old, old))
    results = {r["check"]: r for r in cli._checks(session)}
    assert results["stats"]["verdict"] == cli.CHECK_WARN
    assert "stale" in results["stats"]["detail"]


def test_fresh_figures_pass_quietly(session, monkeypatch):
    from collab import stats as st

    monkeypatch.setattr(cli, "share_stats_enabled", lambda: True)
    st.write_stats(session, {"quota_used_pct": 40})
    results = {r["check"]: r for r in cli._checks(session)}
    assert results["stats"]["verdict"] == cli.CHECK_OK


def test_it_says_nothing_when_sharing_is_off(session, monkeypatch):
    """Off is a decision somebody made, not a fault to report every few turns."""
    monkeypatch.setattr(cli, "share_stats_enabled", lambda: False)
    results = {r["check"]: r for r in cli._checks(session)}
    assert "stats" not in results


# --- what the host is told to say -------------------------------------------

def test_the_host_is_offered_an_opening_message(session, capsys):
    """The first message decides whether the next hour is collaboration or two
    monologues, and it is always the same message."""
    cli._opening_message(session)

    out = capsys.readouterr().out
    assert "listen --follow" in out, "arm a watcher"
    assert "working" in out, "and say what you are on"
    assert "check" in out, "and keep checking"
    # Asked for in the same breath as the watcher, because the guests that
    # cannot hold one are precisely the guests who will not raise it themselves.
    assert "wake" in out, "and be woken if you cannot watch"


def test_the_monitor_hint_names_the_loop(session, capsys):
    cli._monitor_hint(session, {"bridge_port": 45855})

    out = capsys.readouterr().out
    assert "on a loop" in out
    assert "SILENT" in out, "so an agent knows silence is the good case"


# --- the one way it could be silent while nothing was listening -------------

def test_a_reused_pid_does_not_resurrect_a_dead_watcher(session):
    """A watcher killed with SIGKILL never runs its `finally`, so its file
    outlives it — and once the kernel reuses that pid for anything at all,
    `kill(pid, 0)` says yes and a session with nothing reading it looks
    perfectly healthy. The worst failure this feature could have."""
    import os

    directory = d.watchers_dir(session)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / str(os.getpid())).write_text("123456789")   # not our start time

    assert d.watchers(session) == []
    assert not (directory / str(os.getpid())).exists(), "and it is swept up"

    activity.write_local(session, activity.sanitise({"state": "idle"}))
    _published(session, activity.sanitise({"state": "idle"}))
    code, out = _run()

    assert code == 1, "nothing is reading, and it says so"
    assert "nothing is reading this session" in out


def test_a_watcher_records_which_process_it_is(session):
    import os

    with d.watching(session):
        stamp = (d.watchers_dir(session) / str(os.getpid())).read_text().strip()

    assert stamp == d._started_at(os.getpid()) != ""


# --- a fact that arrived and was never filed --------------------------------

def test_a_dropped_learning_is_named_with_its_count(session):
    """The queue the feed fills and the heartbeat empties is bounded, so a
    drop is a real event rather than a theoretical one — and it is silent by
    construction. The sender was told the room had its fact and this end never
    filed it, so nothing but this says otherwise."""
    _status(session, learnings={"pending": 0, "last_error": None, "dropped": 7})
    with _healthy(session):
        code, out = _run()

    assert "7 learnings or sync answers dropped" in out
    assert "learn sync" in out, "and what to do once the flood has passed"
    assert code == 0, "a warning, not a failure: the session still works"


def test_one_dropped_learning_reads_as_one(session):
    _status(session, learnings={"dropped": 1})
    with _healthy(session):
        _code, out = _run()

    assert "1 learning or sync answer dropped" in out


def test_nothing_dropped_says_nothing(session):
    """Silence is the whole point of this command, and a zero here would be a
    line every run for a thing that has not happened."""
    _status(session, learnings={"pending": 0, "last_error": None})
    with _healthy(session):
        code, out = _run()

    assert code == 0 and out == ""


def test_dropped_and_waiting_are_two_separate_answers(session):
    """One is our own writing not going out, the other is somebody else's not
    coming in. A session can have either without the other, so they are not
    folded into one line."""
    _status(session, learnings={"pending": 3, "dropped": 2,
                                "last_error": "read-only file system"})
    with _healthy(session):
        _code, out = _run()

    assert "3 learnings waiting to be published" in out
    assert "2 learnings or sync answers dropped" in out


def test_the_count_reaches_the_json_as_its_own_check(session):
    """An agent reads this command as JSON, so the row has to be there and not
    only in the printed form."""
    _status(session, learnings={"dropped": 4})
    with _healthy(session):
        _code, out = _run(json=True)

    rows = [r for r in json.loads(out)["checks"] if r["check"] == "learnings"]
    assert len(rows) == 1
    assert rows[0]["verdict"] == cli.CHECK_WARN
    assert "4 learnings" in rows[0]["detail"]


# --- and the same three figures on the status page --------------------------

def _status_page(as_json=False):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(argparse.Namespace(json=as_json))
    return out.getvalue()


def test_the_status_page_carries_the_learnings_figures(session):
    """`collab check` is the loop and this is the page somebody reads. The
    daemon kept three figures about what became of the facts and showed a
    person none of them."""
    _status(session, learnings={"pending": 2, "dropped": 5,
                                "last_error": "read-only file system"})
    line = [ln for ln in _status_page().splitlines()
            if ln.strip().startswith("learnings")]

    assert len(line) == 1, _status_page()
    assert "2 waiting to publish" in line[0]
    assert "5 dropped" in line[0]
    assert "read-only file system" in line[0]


def test_the_status_page_says_nothing_when_there_is_nothing_to_say(session):
    """One line and only when there is one to print. A row of zeroes on every
    run is a row people learn to skip."""
    _status(session, learnings={"pending": 0, "last_error": None})

    assert "learnings" not in _status_page()


def test_the_figures_reach_the_status_json(session):
    """An agent reads this as JSON, and a figure that prints and is not in the
    payload is a figure only a human can act on."""
    _status(session, learnings={"dropped": 3})

    assert json.loads(_status_page(as_json=True))["learnings"] == {"dropped": 3}
