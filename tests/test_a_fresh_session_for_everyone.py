"""Asking a room of peers to start again, and letting the room decide.

An operator with a new set of tasks wants the whole swarm to start clean. No
agent can be told to by another: a session somebody is mid-task in is not
anybody else's to discard, and there is no participant here whose say-so counts
for more. So it is a PROPOSAL, and the only thing that carries it is agreement.

THERE IS NO COORDINATOR, which is the design and not an omission. Every daemon
sees the same feed, keeps its own copy of the proposal and the votes, and
reaches its own verdict. That is the only arrangement with no single point to
fail — and it is why two daemons can legitimately disagree about who was
connected when the proposal arrived, which is accepted and written down rather
than papered over.

Everything here is matched by PARTICIPANT ID and never by name. A name is a
display string anybody may take; a vote counted under one would let a joiner
agree on somebody else's behalf by renaming themselves, and the thing being
agreed to destroys work.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import time
from pathlib import Path

import pytest

from collab import activity, cli, config as cfg
from collab.client import daemon as d
from collab.client.inbox import Inbox
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope, now_iso


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    yield
    cfg._CACHE.clear()


class _Daemon:
    """Enough of a daemon to see a proposal through, and a record of what it did."""

    _note_fresh_session = d.Daemon._note_fresh_session
    _note_a_proposal = d.Daemon._note_a_proposal
    _note_a_vote = d.Daemon._note_a_vote
    _proposal_is_over = d.Daemon._proposal_is_over
    _consensus_reached = d.Daemon._consensus_reached
    _settle_any_proposal = d.Daemon._settle_any_proposal
    _start_a_fresh_session = d.Daemon._start_a_fresh_session
    _say_in_the_room = d.Daemon._say_in_the_room

    def __init__(self, profile, me, others, *, pane=True):
        self.profile = profile
        self.paths = d.DaemonPaths(profile.dir)
        self.snapshot = {"participants":
                         [{"id": who, "name": who, "connected": True}
                          for who in [me, *others]]}
        self._proposal = None
        self._proposed_at = {}
        self._settled = set()
        self._acted_at = {"compact": 0.0, "new": 0.0}
        self._under_since = {"compact": 0.0, "new": 0.0}
        self._pane = pane
        self.applied: list[str] = []
        self.said: list[str] = []
        self.published: list[dict] = []
        self.reminded: list[str] = []
        self.waker = _Waker(self.reminded)

    async def _post_chat(self, payload):
        self.said.append(payload.get("text", ""))

    async def _publish_activity(self, said):
        self.published.append(said)


class _Waker:
    def __init__(self, into):
        self._into = into

    def offer_reminder(self, text):
        self._into.append(text)
        return True


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="bob",
                           host_name="alice", token="t", home=str(home),
                           participant_id="p_bob")
    saved.save()
    return saved


@pytest.fixture
def swarm(profile, monkeypatch):
    """A daemon for bob, in a room with alice and carol, pane willing."""
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_carol"])

    def apply(root, action, **_kw):
        if not agent._pane:
            return 1, "no tmux pane is armed"
        agent.applied.append(action)
        return 0, "typed /clear into %3"

    monkeypatch.setattr("collab.compaction.apply", apply)
    return agent


def _proposal(by="p_alice", pid="fs_1", reason="a new set of tasks", seq=1):
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT, sender=by.replace("p_", ""),
                    sender_id=by,
                    text=f"proposes that everyone starts a fresh session: {reason}",
                    body={"fresh_session": {"proposal": pid, "by": by,
                                            "reason": reason}})


def _vote(by, pid="fs_1", vote="agree", seq=2, name=None):
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT,
                    sender=name or by.replace("p_", ""), sender_id=by,
                    text=f"{vote}s",
                    body={"fresh_session_vote": {"proposal": pid, "vote": vote,
                                                 "reason": ""}})


def _settle(agent, now=None):
    import collab.client.daemon as mod

    real = mod.time.time
    if now is not None:
        mod.time.time = lambda: now                   # noqa: B010
    try:
        asyncio.run(agent._settle_any_proposal())
    finally:
        mod.time.time = real


# --- a proposal arrives ------------------------------------------------------------

def test_a_proposal_is_recorded_with_who_was_asked(swarm):
    """Who was here when it arrived, recorded then rather than counted later:
    consensus is about the room that was asked, and somebody who joined after
    the question was put was not asked."""
    swarm._note_fresh_session(_proposal())

    assert swarm._proposal["id"] == "fs_1"
    assert swarm._proposal["by"] == "p_alice"
    assert swarm._proposal["asked"] == {"p_bob", "p_carol"}, \
        "the proposer is not asked to agree with themselves"


def test_a_proposal_settles_nothing_on_its_own(swarm):
    swarm._note_fresh_session(_proposal())
    _settle(swarm)
    assert swarm.applied == []


def test_a_message_that_is_not_a_proposal_is_ignored(swarm):
    swarm._note_fresh_session(Envelope(seq=1, ts=now_iso(), kind=KIND_CHAT,
                                       sender="alice", sender_id="p_alice",
                                       text="fresh_session"))
    assert swarm._proposal is None


# --- agreement -----------------------------------------------------------------------

def test_everybody_agreeing_starts_this_agents_fresh_session(swarm):
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", seq=3))

    _settle(swarm)

    assert swarm.applied == ["clear"]


def test_it_says_so_before_it_does_it(swarm):
    """The roster, because an agent about to lose its context is not working on
    anything; and the room, because the others are waiting to see it happen."""
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", seq=3))

    _settle(swarm)

    assert swarm.published and swarm.published[0]["state"] == activity.IDLE
    assert any("starting a fresh session as asked" in line
               for line in swarm.said)


def test_one_short_of_everybody_is_not_everybody(swarm):
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))

    _settle(swarm)

    assert swarm.applied == []


def test_the_proposer_acts_too(profile, monkeypatch):
    """Their own vote is implied; asking somebody to agree with themselves is a
    round trip that answers nothing."""
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_carol"])
    monkeypatch.setattr("collab.compaction.apply",
                        lambda root, action, **_kw: (agent.applied.append(action),
                                                     (0, "typed"))[1])
    agent._note_fresh_session(_proposal(by="p_bob"))
    agent._note_fresh_session(_vote("p_alice"))
    agent._note_fresh_session(_vote("p_carol", seq=3))

    _settle(agent)

    assert agent.applied == ["clear"]


def test_agreeing_is_the_agent_saying_it_is_at_a_boundary(swarm):
    """The idle guard does not apply to an agreed proposal. Overriding a guard
    the agent lifted itself would be the program second-guessing the room."""
    cfg.setting("new_when").write("idle")
    activity.write_local(swarm.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", seq=3))

    _settle(swarm)

    assert swarm.applied == ["clear"]


# --- declining -----------------------------------------------------------------------

def test_one_decline_ends_it_under_all(swarm):
    """The outcome is already decided the moment somebody says no, and leaving
    the question open would keep asking a room that has answered."""
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", vote="decline", seq=3))

    _settle(swarm)

    assert swarm.applied == []
    assert swarm._proposal is None, "and it is over rather than still standing"


def test_a_settled_proposal_is_not_reopened_by_a_late_agreement(swarm):
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_carol", vote="decline", seq=3))
    _settle(swarm)

    swarm._note_fresh_session(_vote("p_carol", vote="agree", seq=4))
    _settle(swarm)

    assert swarm.applied == []


# --- the arithmetic of a majority ------------------------------------------------------

def test_majority_needs_more_than_half_of_those_asked(profile, monkeypatch):
    cfg.setting("new_consensus").write("majority")
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_carol", "p_dave"])
    monkeypatch.setattr("collab.compaction.apply",
                        lambda root, action, **_kw: (agent.applied.append(action),
                                                     (0, "typed"))[1])
    agent._note_fresh_session(_proposal())          # asked: bob, carol, dave
    agent._note_fresh_session(_vote("p_bob"))
    _settle(agent)
    assert agent.applied == [], "one of three is not more than half"

    agent._note_fresh_session(_vote("p_carol", seq=3))
    _settle(agent)

    assert agent.applied == ["clear"], "two of three is"


def test_majority_ends_when_enough_have_declined(profile, monkeypatch):
    cfg.setting("new_consensus").write("majority")
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_carol"])
    monkeypatch.setattr("collab.compaction.apply",
                        lambda root, action, **_kw: (agent.applied.append(action),
                                                     (0, "typed"))[1])
    agent._note_fresh_session(_proposal())          # asked: bob, carol
    agent._note_fresh_session(_vote("p_bob", vote="decline"))

    _settle(agent)

    assert agent.applied == []
    assert agent._proposal is None


def test_one_decline_does_not_end_a_majority_that_can_still_be_reached(
        profile, monkeypatch):
    cfg.setting("new_consensus").write("majority")
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_c", "p_d", "p_e"])
    monkeypatch.setattr("collab.compaction.apply",
                        lambda root, action, **_kw: (agent.applied.append(action),
                                                     (0, "typed"))[1])
    agent._note_fresh_session(_proposal())          # asked: bob, c, d, e
    agent._note_fresh_session(_vote("p_c", vote="decline"))

    _settle(agent)

    assert agent._proposal is not None, "three could still agree"


# --- expiry ----------------------------------------------------------------------------

def test_a_proposal_nobody_answered_expires(swarm):
    """Long enough for an agent to finish a piece of work and answer, short
    enough that it does not fire at a room that has moved on."""
    swarm._note_fresh_session(_proposal())
    minutes = cfg.new_consensus_minutes()

    _settle(swarm, now=time.time() + minutes * 60 + 1)

    assert swarm.applied == []
    assert swarm._proposal is None


def test_an_expired_proposal_does_not_fire_on_a_late_agreement(swarm):
    swarm._note_fresh_session(_proposal())
    _settle(swarm, now=time.time() + cfg.new_consensus_minutes() * 60 + 1)

    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", seq=3))
    _settle(swarm)

    assert swarm.applied == []


# --- one at a time, and not too often --------------------------------------------------

def test_a_second_proposal_is_ignored_while_one_is_open(swarm):
    """Two open proposals is two answers to «are we starting again» and an
    agent asked to agree to both."""
    swarm._note_fresh_session(_proposal(pid="fs_1"))
    swarm._note_fresh_session(_proposal(by="p_carol", pid="fs_2", seq=5))

    assert swarm._proposal["id"] == "fs_1"


def test_the_same_proposer_may_not_ask_again_at_once(swarm):
    """A proposal is put in front of every agent in the room as something to
    stop and answer. One every turn is one interruption per turn for
    everybody."""
    swarm._note_fresh_session(_proposal(pid="fs_1"))
    swarm._note_fresh_session(_vote("p_bob", vote="decline"))
    _settle(swarm)

    swarm._note_fresh_session(_proposal(pid="fs_2", seq=6))

    assert swarm._proposal is None


def test_a_different_proposer_is_not_held_by_somebody_elses_cooldown(swarm):
    swarm._note_fresh_session(_proposal(by="p_alice", pid="fs_1"))
    swarm._note_fresh_session(_vote("p_bob", vote="decline"))
    _settle(swarm)

    swarm._note_fresh_session(_proposal(by="p_carol", pid="fs_2", seq=6))

    assert swarm._proposal is not None and swarm._proposal["id"] == "fs_2"


# --- identity ---------------------------------------------------------------------------

def test_a_vote_under_a_borrowed_name_does_not_count(swarm):
    """Matched by id and never by name. A joiner who renamed themselves `carol`
    would otherwise agree on the real carol's behalf, and the thing being
    agreed to destroys work."""
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_impostor", seq=3, name="carol"))

    _settle(swarm)

    assert swarm.applied == [], "carol has not answered"


def test_a_vote_with_no_id_at_all_is_dropped(swarm):
    swarm._note_fresh_session(_proposal())
    nameless = _vote("p_carol")
    nameless.sender_id = ""
    swarm._note_fresh_session(nameless)

    assert swarm._proposal["votes"] == {}


def test_a_vote_on_another_proposal_is_not_this_one(swarm):
    swarm._note_fresh_session(_proposal(pid="fs_1"))
    swarm._note_fresh_session(_vote("p_bob", pid="fs_other"))

    assert swarm._proposal["votes"] == {}


# --- an agent with no pane to type into ---------------------------------------------------

def test_an_agent_with_no_pane_is_told_what_to_do(profile, monkeypatch):
    """An ordinary state of affairs rather than a fault: a Codex thread, a
    headless recipe, a tool with no wake armed. The agent is told in the one
    way it can still be reached, and the words are an instruction because that
    is what it is."""
    agent = _Daemon(profile, "p_bob", ["p_alice", "p_carol"], pane=False)
    monkeypatch.setattr("collab.compaction.apply",
                        lambda root, action, **_kw: (1, "no tmux pane is armed"))
    agent._note_fresh_session(_proposal())
    agent._note_fresh_session(_vote("p_bob"))
    agent._note_fresh_session(_vote("p_carol", seq=3))

    _settle(agent)

    assert agent.reminded, "it has to reach the agent somehow"
    said = agent.reminded[0]
    assert "collab new" in said and "restart your session" in said


def test_an_agent_with_the_switch_off_says_so_rather_than_going_quiet(swarm):
    """A room that agreed to start fresh and has one agent that did not is a
    room where nobody can tell which."""
    cfg.setting("new").write(False)
    swarm._note_fresh_session(_proposal())
    swarm._note_fresh_session(_vote("p_bob"))
    swarm._note_fresh_session(_vote("p_carol", seq=3))

    _settle(swarm)

    assert swarm.applied == []
    assert any("config new on" in line for line in swarm.said)


# --- the settings that decide it -----------------------------------------------------------

def test_all_is_the_default_because_the_act_destroys_work():
    assert cfg.new_consensus() == "all"
    assert cfg.setting("new_consensus").default == "all"


def test_the_window_is_ten_minutes_by_default():
    assert cfg.new_consensus_minutes() == 10


@pytest.mark.parametrize("typed", ["0", "500", "-3"])
def test_a_window_outside_the_range_is_refused(typed):
    with pytest.raises(ValueError):
        cfg.setting("new_consensus_minutes").parse(typed)


def test_a_window_in_a_hand_edited_file_is_clamped(tmp_path):
    (tmp_path / "global-config.json").write_text('{"new_consensus_minutes": 9999}')
    cfg._CACHE.clear()
    assert cfg.new_consensus_minutes() == cfg.MAX_CONSENSUS_MINUTES


# --- the commands that put one on the wire ------------------------------------------

def _cli(profile, monkeypatch, sent, **flags):
    """`collab new` with the swarm flags, and a hub that records what was sent."""
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send(self, env):
            sent.append(env)

    monkeypatch.setattr(cli, "_client", lambda p: _Client())
    monkeypatch.setattr(cli, "_current_stats", lambda p: None)
    fields = {"session": None, "agent": None, "all": False, "agree": None,
              "decline": None, "reason": None, "status": False, "json": False}
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_new(argparse.Namespace(**{**fields, **flags}))
    return code, out.getvalue()


def _inbox_holds(profile, *envelopes):
    """Put messages in this agent's own inbox, as the daemon would have."""
    box = Inbox(Path(profile.dir))
    try:
        for env in envelopes:
            box.record(env)
    finally:
        box.close()


def test_proposing_puts_a_proposal_on_the_wire(profile, monkeypatch):
    sent = []
    code, out = _cli(profile, monkeypatch, sent, all=True,
                     reason="moving to the billing work")

    assert code == 0 and len(sent) == 1
    body = sent[0].body["fresh_session"]
    assert body["by"] == profile.participant_id, "by id, never by name"
    assert body["reason"] == "moving to the billing work"
    assert body["proposal"] and body["proposal"] in out
    assert "proposes that everyone starts a fresh session" in sent[0].text


def test_a_second_proposal_is_refused_while_one_is_open(profile, monkeypatch):
    """Two open proposals is two answers to «are we starting again» and an
    agent asked to agree to both."""
    _inbox_holds(profile, _proposal(pid="fs_open"))
    sent = []

    code, out = _cli(profile, monkeypatch, sent, all=True)

    assert code == 1 and sent == []
    assert "already open" in out and "fs_open" in out


def test_agreeing_puts_a_vote_on_the_wire(profile, monkeypatch):
    _inbox_holds(profile, _proposal(pid="fs_open"))
    sent = []

    code, _out = _cli(profile, monkeypatch, sent, agree="fs_open")

    assert code == 0
    vote = sent[0].body["fresh_session_vote"]
    assert vote == {"proposal": "fs_open", "vote": "agree", "reason": ""}


def test_declining_carries_the_reason(profile, monkeypatch):
    """Under the default rule one decline ends it, so the others need to know
    whether to wait or to go without you."""
    _inbox_holds(profile, _proposal(pid="fs_open"))
    sent = []

    code, _out = _cli(profile, monkeypatch, sent, decline="fs_open",
                      reason="mid-migration")

    assert code == 0
    vote = sent[0].body["fresh_session_vote"]
    assert vote["vote"] == "decline" and vote["reason"] == "mid-migration"
    assert "mid-migration" in sent[0].text


def test_answering_a_proposal_nobody_made_is_refused(profile, monkeypatch):
    sent = []
    code, out = _cli(profile, monkeypatch, sent, agree="fs_nothing")

    assert code == 1 and sent == []
    assert "no open proposal" in out


def test_status_says_who_has_not_answered(profile, monkeypatch):
    """The ones who have not answered are the point of the listing: a proposal
    waits on them."""
    (Path(profile.dir) / "snapshot.json").write_text(json.dumps({"participants": [
        {"id": "p_alice", "name": "alice", "connected": True},
        {"id": "p_bob", "name": "bob", "connected": True},
        {"id": "p_carol", "name": "carol", "connected": True}]}))
    _inbox_holds(profile, _proposal(by="p_alice", pid="fs_open"),
                 _vote("p_bob", pid="fs_open", seq=2))
    sent = []

    _code, out = _cli(profile, monkeypatch, sent, status=True)

    assert "fs_open" in out
    assert "proposed it" in out, "alice"
    assert "agree" in out, "bob"
    assert "has not answered" in out, "carol"


def test_status_with_nothing_open_says_so(profile, monkeypatch):
    sent = []
    _code, out = _cli(profile, monkeypatch, sent, status=True)
    assert "no fresh-session proposal is open" in out


def test_an_expired_proposal_is_not_open(profile, monkeypatch):
    """Read off the feed with the same window every daemon uses, so a command
    and a daemon cannot disagree about what is open."""
    old = _proposal(pid="fs_old")
    old.ts = "2020-01-01T00:00:00+00:00"
    _inbox_holds(profile, old)
    sent = []

    _code, out = _cli(profile, monkeypatch, sent, status=True)

    assert "no fresh-session proposal is open" in out
