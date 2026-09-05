"""A status that outlived its work, and the three places that now notice.

`collab working "the parser"` is true when it is said and stays true-looking
for ever. An agent that finished at eleven and never said `idle` reads at four
o'clock exactly as it read at eleven — and the roster answers «who is free»,
so the cost of that falls on a colleague, who passes it over for the afternoon
while doing exactly what the roster told them.

`is_stale` already refused to draw such a statement as a present tense on
somebody ELSE's roster. What was missing was everything on the agent's own
side: nothing put its own claim in front of it, nothing asked about it, and
nothing ever retired it.

So, three measures, and the interesting part is what tells them apart. The
agent's own screens carry its statement WITH ITS AGE, which is the figure that
makes it checkable. The reminder gains a sentence when the statement is old and
the agent's usage figures HAVE moved — the case where the two facts contradict
each other. And the daemon retires the statement when it is old and the figures
have NOT moved, which is the case where there is no contradiction to point out
because nobody is there.

Never to `idle`, always to `quiet`. `idle` is a thing an agent says about
itself and means «free for work»; this is a thing the daemon observed and means
«nobody knows». Inferring the first from the second hands work to an agent that
is not there, which is the same failure the other way round.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from collab import activity as act, config as cfg
from collab.client import daemon as d, statusbar as sb
from collab.config import SessionProfile

MINUTE = 60.0


# --- the agent's own statement, with its age ------------------------------------

def _working(what="the token refresh", ago=0.0, now=None):
    at = (now or time.time()) - ago
    return {"state": act.WORKING, "what": what, "since": at, "updated_at": at}


def test_the_segment_says_what_and_how_long_ago_it_was_said():
    """The age is the point. «working: the parser» stays true-looking for ever;
    how long the statement has stood is the figure that makes it checkable."""
    now = time.time()
    said = sb.activity_segment(_working(ago=50 * MINUTE, now=now), now=now)
    assert "working" in said and "the token refresh" in said
    assert "50m ago" in said


def test_a_narrow_pane_keeps_the_age_and_gives_up_the_objective():
    """Six characters buy the only part a reader cannot infer."""
    now = time.time()
    said = sb.activity_segment(_working(ago=50 * MINUTE, now=now), now=now,
                               narrow=True)
    assert said == "working · 50m ago"


def test_a_long_objective_is_cut_at_a_word():
    now = time.time()
    said = sb.activity_segment(
        _working("rewriting the token refresh across every client", now=now),
        now=now)
    assert "…" in said and "rewritin…" not in said


def test_nothing_is_drawn_when_nothing_was_ever_declared():
    """An agent whose user never ran `collab working` would otherwise carry an
    empty label for the life of the session."""
    assert sb.activity_segment(None) == ""
    assert sb.activity_segment({}) == ""
    assert sb.activity_segment({"state": "nonsense"}) == ""


def test_a_decayed_status_says_what_it_replaced_rather_than_its_own_age():
    """Its own age is «a moment», which says nothing. What a reader needs is
    the last thing the agent actually said."""
    now = time.time()
    said = sb.activity_segment(
        {"state": act.QUIET, "what": "working on the parser", "decayed": True,
         "until": now - 3600, "since": now, "updated_at": now}, now=now)
    assert said.startswith("quiet since ")


def test_it_is_on_both_of_the_agents_own_surfaces_by_default():
    """The roster carries everybody's line except the reader's, and the status
    line is the one place the agent's own claim is in front of it."""
    assert "activity" in cfg.STATUSLINE_SEGMENTS
    assert "activity" in cfg.WATCH_ROSTER_SEGMENTS


def test_the_readers_own_figure_is_the_one_exception_on_that_row(cfg_file):
    """`stats` and `command` stay refused: four participants would read four
    numbers off one row and each take theirs for everybody's. This one says
    about the reader exactly what the rows above say about everybody else."""
    with pytest.raises(ValueError):
        cfg.setting("watch_status_roster_segments").write(["batch", "stats"])
    cfg.setting("watch_status_roster_segments").write(["batch", "activity"])
    assert cfg.watch_roster_settings()["segments"] == ("batch", "activity")


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "own-config.json"
    monkeypatch.setenv("COLLAB_CONFIG", str(path))
    cfg._CACHE.clear()
    yield path
    cfg._CACHE.clear()


# --- the quiet state -------------------------------------------------------------

def test_quiet_is_not_idle():
    """`idle` means «free for work», and it is a thing an agent says about
    itself. Handing work out on an inferred one is the failure this avoids."""
    assert act.QUIET in act.STATES
    assert act.QUIET != act.IDLE


def test_a_decayed_statement_carries_what_it_replaced(cfg_file):
    now = time.time()
    said = act.sanitise({"state": act.QUIET, "decayed": True, "until": now - 100,
                         "what": "the parser"})
    assert said["state"] == act.QUIET
    assert said["decayed"] is True and said["until"] == now - 100
    assert said["what"] == "the parser"


def test_the_roster_line_for_a_quiet_agent_says_what_it_last_claimed(cfg_file):
    now = time.time()
    line = act.describe({"state": act.QUIET, "what": "working on the parser",
                         "until": now - 3600, "since": now, "updated_at": now})
    assert line.startswith("quiet · said working on the parser until ")


# --- the daemon: watching whether the figures actually moved ---------------------

@pytest.fixture
def agent(tmp_path, monkeypatch, cfg_file):
    home = tmp_path / "checkout" / ".collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="alice",
                             host_name="alice", token="t", home=str(home),
                             participant_id="p_a")
    profile.save()
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = profile
    daemon.paths = d.DaemonPaths(profile.dir)
    daemon._figures_mark = ""
    daemon._figures_moved_at = 0.0
    daemon._http = None
    daemon.published: list = []

    async def publish(said):
        daemon.published.append(said)
        act.write_local(profile, said)

    daemon._publish_activity = publish
    daemon.waker = type("W", (), {"reminder": staticmethod(
        lambda: {"text": "the shipped reminder"})})()
    return daemon


def _figures(agent, **over):
    from collab.stats import write_stats

    write_stats(agent.profile, {"tokens_in": 100, "cost_usd": 1.0, **over})


def _at(agent, when, fn):
    """Run one heartbeat step with the clock where the test wants it."""
    real = d.time.time
    d.time.time = lambda: when
    try:
        return fn()
    finally:
        d.time.time = real


def test_a_file_rewritten_with_the_same_numbers_is_not_a_movement(agent):
    """A status line rewrites the figures on every prompt whether or not
    anything changed, so the file's own timestamp says «this agent exists» and
    nothing else. What the two measures need is «this agent is working»."""
    _figures(agent)
    _at(agent, 1000.0, agent._watch_the_figures)
    assert agent._figures_moved_at == 1000.0

    _figures(agent)                                     # same numbers again
    _at(agent, 2000.0, agent._watch_the_figures)
    assert agent._figures_moved_at == 1000.0, "a rewrite counted as work"

    _figures(agent, tokens_in=500)
    _at(agent, 3000.0, agent._watch_the_figures)
    assert agent._figures_moved_at == 3000.0


def test_the_first_reading_is_not_a_movement(agent):
    """Counted as one, every restart would look like a busy agent for an
    interval — and would suppress the very nudge it is there to give."""
    _figures(agent)
    _at(agent, 1000.0, agent._watch_the_figures)
    assert agent._figures_mark and agent._figures_moved_at == 1000.0


# --- the reminder's sentence -------------------------------------------------------

def test_a_busy_agent_with_an_old_status_is_told_about_it(agent):
    """The case where the two facts contradict each other: the agent is
    demonstrably working, and the roster says it has been doing one thing since
    an hour ago."""
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 5000.0                    # spent since it spoke

    text = _at(agent, 5000.0, agent._reminder_text)
    assert "the shipped reminder" in text
    assert "Your status has said" in text
    assert "the token refresh" in text
    assert "collab activity" in text


def test_an_agent_that_has_spent_nothing_is_not_nudged(agent):
    """There is no contradiction to point out. It is not working, its old
    statement is the last true thing it said, and the decay answers that."""
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 900.0                     # before it spoke

    assert _at(agent, 5000.0, agent._reminder_text) == "the shipped reminder"


def test_a_fresh_status_is_not_nudged(agent):
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=4900.0))
    agent._figures_moved_at = 5000.0
    assert _at(agent, 5000.0, agent._reminder_text) == "the shipped reminder"


def test_an_idle_agent_is_never_nudged(agent):
    """An `idle` nobody renewed misleads nobody: the roster reads «free», which
    is what it was told and what a departed agent is."""
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, {"state": act.IDLE, "since": 1000.0,
                                    "updated_at": 1000.0})
    agent._figures_moved_at = 5000.0
    assert _at(agent, 5000.0, agent._reminder_text) == "the shipped reminder"


def test_zero_turns_the_whole_thing_off(agent):
    cfg.set_activity_stale_after(0)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 5000.0
    assert _at(agent, 99_000.0, agent._reminder_text) == "the shipped reminder"
    _at(agent, 99_000.0, lambda: asyncio.run(agent._maybe_decay_activity()))
    assert agent.published == []


# --- the decay -----------------------------------------------------------------------

def _decay(agent, when):
    return _at(agent, when, lambda: asyncio.run(agent._maybe_decay_activity()))


def test_a_statement_with_nothing_behind_it_decays_to_quiet(agent):
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 1000.0

    _decay(agent, 1000.0 + 29 * MINUTE)
    assert agent.published == [], "it decayed inside its own window"

    _decay(agent, 1000.0 + 31 * MINUTE)
    assert len(agent.published) == 1
    said = agent.published[0]
    assert said["state"] == act.QUIET
    assert said["decayed"] is True
    assert said["until"] == 1000.0, "it keeps the moment it stopped being known"
    assert "the token refresh" in said["what"]


def test_a_busy_agent_is_never_decayed(agent):
    """It is told in the reminder instead. Only an agent saying nothing AND
    spending nothing has its last word retired."""
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 1000.0 + 29 * MINUTE      # still spending
    _decay(agent, 1000.0 + 31 * MINUTE)
    assert agent.published == []


def test_an_idle_agent_is_never_decayed(agent):
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, {"state": act.IDLE, "since": 1000.0,
                                    "updated_at": 1000.0})
    agent._figures_moved_at = 1000.0
    _decay(agent, 99_000.0)
    assert agent.published == []


def test_it_decays_once_and_not_on_every_beat(agent):
    """`quiet` is not `working`, so the next pass finds nothing to retire."""
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 1000.0
    for tick in range(5):
        _decay(agent, 1000.0 + (31 + tick) * MINUTE)
    assert len(agent.published) == 1


def test_the_agent_speaking_again_replaces_it_normally(agent):
    cfg.set_activity_stale_after(30)
    act.write_local(agent.profile, _working(ago=0, now=1000.0))
    agent._figures_moved_at = 1000.0
    _decay(agent, 1000.0 + 31 * MINUTE)
    assert act.read_local(agent.profile)["state"] == act.QUIET

    act.write_local(agent.profile, _working("something new", now=99_000.0))
    assert act.read_local(agent.profile)["state"] == act.WORKING
