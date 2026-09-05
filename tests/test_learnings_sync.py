"""The daemon half: the spool is drained, and only one repository ever leaves.

The store holds what this agent has learnt about EVERY repository it has worked
on. The people in the room are working on one of them. That gap is the whole
security surface of this feature, and it is closed in one place: the responder
derives the key from its own checkout, every time, and never reads one out of
the request. A field nobody reads cannot become a field somebody reads by
accident, which is why a sync request carrying `"repo": "B"` is not refused
here so much as unnoticed.

The other half is that no agent ever waits. The commands write a spool file and
return; this is where the file becomes a bundle write, an index update and a
publish, on the heartbeat and off the event loop. A spool file is deleted after
the work succeeded and not before, so a crash between the write and the publish
loses a few seconds rather than the learning.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from collab import config as cfg, learnings as L
from collab.client import daemon as d
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope


class _Http:
    """An http client that records what would have been posted."""

    def __init__(self, fails: bool = False):
        self.posts: list[dict] = []
        self.fails = fails

    async def post(self, url, headers=None, json=None, timeout=None):
        if self.fails:
            raise RuntimeError("the hub is not answering")
        self.posts.append(json or {})
        return None


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A daemon whose session lives in a checkout with a known remote."""
    cfg._CACHE.clear()
    home = tmp_path / "checkout" / ".collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="alice",
                             host_name="alice", token="t", home=str(home),
                             participant_id="p_a")
    profile.save()
    monkeypatch.setattr(L, "_git", lambda *a, **k: "git@host:owner/A.git")

    # Built without `__init__`, which would open a bridge and take the lock.
    # The cost is that this list has to be kept level with the constructor's
    # learnings half by hand — `_to_publish` sat here for a while after the
    # daemon stopped having one — so anything added there is added here.
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = profile
    daemon.paths = d.DaemonPaths(profile.dir)
    daemon._arrived = []
    daemon._sync_asks = []
    daemon._sync_wanted = 0
    daemon._answered_sync = {}
    daemon._learning_error = ""
    daemon._learnings_dropped = 0
    daemon._http = _Http()
    return daemon


def _bundle_of(daemon):
    key, bundle = daemon._bundle()
    bundle.mkdir(parents=True, exist_ok=True)
    return key, bundle


def _one(slug="a-thing", **over):
    fields = {"title": "A thing", "description": "One line.",
              "body": "Because of this.", "tags": ["infra"], "by": "bob",
              "at": "2026-09-05T16:04:00Z", "repo": "host/owner/A"}
    fields.update(over)
    return L.Learning(slug=slug, **fields)


def _work(daemon):
    asyncio.run(daemon._do_the_learning_work())


# --- the key is the daemon's own, always -----------------------------------------

def test_the_daemon_works_out_of_its_own_checkouts_key(agent):
    key, _bundle = agent._bundle()
    assert key == "host/owner/A"


def test_a_learning_that_claims_another_repository_is_filed_under_ours(agent):
    """A sender can say anything. Believing it would file knowledge under a
    repository nobody in the room is working on."""
    key, bundle = _bundle_of(agent)
    agent._arrived.append(Envelope(
        kind=KIND_CHAT, sender="bob",
        body={L.MARKER: {**L.to_wire(_one(repo="somebody/else"))}}))
    _work(agent)
    assert L.load(bundle, "a-thing").repo == key


# --- the spool becomes the work ---------------------------------------------------

def test_recording_one_writes_the_bundle_and_publishes_it(agent):
    key, bundle = _bundle_of(agent)
    L.spool(agent.profile.dir, "add", learning={
        "title": "The eu-west key", "body": "Because of this.",
        "tags": ["infra"], "by": "alice", "repo": key})
    _work(agent)

    assert L.slugs(bundle) == {"the-eu-west-key"}
    written = L.load(bundle, "the-eu-west-key")
    assert written.by == "alice" and written.at, "it is stamped when it is written"
    assert written.peer_uses == 0, "our own learning has no peer count"
    assert (bundle / L.INDEX).exists() and (bundle / L.LOG).exists()
    posted = agent._http.posts[-1]
    assert posted["text"].startswith(L.PREFIX)
    assert posted["body"][L.MARKER]["slug"] == "the-eu-west-key"
    assert not L.pending(agent.profile.dir), "the spool file outlived the work"


def test_the_spool_file_survives_a_failure_and_is_retried(agent):
    """A crash between the write and the publish loses a few seconds. Deleting
    the file first would lose exactly the ones written while something was
    wrong."""
    _bundle_of(agent)
    agent._http = _Http(fails=True)
    L.spool(agent.profile.dir, "add", learning={"title": "A thing", "by": "alice"})
    _work(agent)
    assert len(L.pending(agent.profile.dir)) == 1
    assert "the hub is not answering" in agent._learning_error

    agent._http = _Http()
    _work(agent)
    assert not L.pending(agent.profile.dir)
    assert agent._learning_error == ""
    assert agent._http.posts


def test_the_counters_are_kept_by_the_daemon_and_not_the_command(agent):
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(repo=key))
    L.spool(agent.profile.dir, "read", slug="a-thing")
    L.spool(agent.profile.dir, "used", slug="a-thing")
    _work(agent)
    back = L.load(bundle, "a-thing")
    assert back.reads == 1 and back.uses == 1


def test_a_half_written_spool_file_is_dropped_rather_than_retried_for_ever(agent):
    _bundle_of(agent)
    junk = L.spool_dir(agent.profile.dir)
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "1-broken.json").write_text("{not json")
    _work(agent)
    assert not L.pending(agent.profile.dir)


def test_what_is_waiting_reaches_the_status_file(agent):
    """The command returned at once saying the daemon would do it, so the agent
    never finds out that it did not. Something has to say so."""
    _bundle_of(agent)
    agent._http = _Http(fails=True)
    L.spool(agent.profile.dir, "add", learning={"title": "A thing", "by": "alice"})
    _work(agent)
    figures = agent._learning_figures()
    assert figures["pending"] == 1
    assert "not answering" in figures["last_error"]


# --- the sync, and what may leave the machine -------------------------------------

def _ask(sender="bob", **body):
    return Envelope(kind=KIND_CHAT, sender=sender,
                    body={L.SYNC_MARKER: {"want": 20, **body}})


def test_only_the_session_s_own_repository_ever_leaves(agent):
    """The store holds two repositories and the session is in one of them. A
    request naming the other must not reach it — and it must not reach it
    because the request is never read, rather than because it was refused."""
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(slug="ours", title="Ours", repo=key))
    other = L.bundle_dir("host/owner/B")
    other.mkdir(parents=True, exist_ok=True)
    L.save(other, _one(slug="theirs", title="Theirs", repo="host/owner/B"))

    agent._sync_asks.append(_ask(repo="host/owner/B"))
    _work(agent)

    sent = [p["body"][L.MARKER]["slug"] for p in agent._http.posts
            if L.MARKER in p.get("body", {})]
    assert sent == ["ours"], sent
    assert all(p.get("to") == "bob" for p in agent._http.posts)


def test_an_answer_goes_directly_and_not_to_the_room(agent):
    """A sync is a burst of twenty messages. The room does not want them."""
    key, bundle = _bundle_of(agent)
    for n in range(3):
        L.save(bundle, _one(slug=f"one-{n}", repo=key))
    agent._sync_asks.append(_ask())
    _work(agent)
    assert len(agent._http.posts) == 3
    assert {p["to"] for p in agent._http.posts} == {"bob"}


def test_an_answer_carries_the_senders_own_counts(agent):
    """So a receiver can order an index it has never read."""
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(repo=key, uses=4, reads=9))
    agent._sync_asks.append(_ask())
    _work(agent)
    body = agent._http.posts[0]["body"][L.MARKER]
    assert body["uses"] == 4 and body["reads"] == 9


def test_the_most_used_go_first_and_the_ask_is_bounded(agent):
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(slug="quiet", repo=key))
    L.save(bundle, _one(slug="loud", repo=key, uses=5))
    agent._sync_asks.append(_ask(want=1))
    _work(agent)
    sent = [p["body"][L.MARKER]["slug"] for p in agent._http.posts]
    assert sent == ["loud"]


def test_the_same_asker_is_not_answered_twice_in_five_minutes(agent, monkeypatch):
    """An agent that asked twice by accident, or a loop that asks every turn,
    would otherwise be answered every time by every other agent here."""
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(repo=key))
    agent._sync_asks.append(_ask())
    _work(agent)
    assert len(agent._http.posts) == 1

    agent._sync_asks.append(_ask())
    _work(agent)
    assert len(agent._http.posts) == 1, "it answered the same asker again"

    agent._answered_sync["bob"] -= L.SYNC_COOLDOWN + 1
    agent._sync_asks.append(_ask())
    _work(agent)
    assert len(agent._http.posts) == 2, "and never answers again after that"


def test_a_different_asker_is_answered(agent):
    key, bundle = _bundle_of(agent)
    L.save(bundle, _one(repo=key))
    agent._sync_asks.append(_ask("bob"))
    agent._sync_asks.append(_ask("carol"))
    _work(agent)
    assert {p["to"] for p in agent._http.posts} == {"bob", "carol"}


def test_our_own_request_is_published_to_the_room(agent):
    _bundle_of(agent)
    L.spool(agent.profile.dir, "sync", want=7)
    _work(agent)
    asked = [p for p in agent._http.posts if L.SYNC_MARKER in p.get("body", {})]
    assert asked and asked[0]["body"][L.SYNC_MARKER]["want"] == 7
    assert "to" not in asked[0], "a request goes to the room, not to one person"


def test_we_do_not_answer_our_own_request(agent):
    """The feed carries back what this daemon published, and answering it would
    be a burst of direct messages to ourselves."""
    _bundle_of(agent)
    env = Envelope(kind=KIND_CHAT, sender="alice",
                   body={L.SYNC_MARKER: {"want": 5}})
    agent._note_any_learning(env)
    assert agent._sync_asks == []


def test_the_feed_queues_rather_than_writing(agent):
    """A bundle write in the middle of the stream would hold the feed for a
    disk, and a burst of forty answers would hold it forty times."""
    _bundle_of(agent)
    env = Envelope(kind=KIND_CHAT, sender="bob",
                   body={L.MARKER: L.to_wire(_one())})
    agent._note_any_learning(env)
    assert agent._arrived == [env]
    assert not L.slugs(L.bundle_dir("host/owner/A")), "the feed wrote a file"


def test_nothing_happens_at_all_when_the_store_is_off(agent):
    cfg.set_learnings_dir("")
    L.spool(agent.profile.dir, "add", learning={"title": "A thing"})
    agent._arrived.append(Envelope(kind=KIND_CHAT, sender="bob",
                                   body={L.MARKER: L.to_wire(_one())}))
    _work(agent)
    assert agent._http.posts == []
    assert L.pending(agent.profile.dir), "the spool was thrown away"
