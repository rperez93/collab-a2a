"""Two structures in the daemon that grew for as long as the session ran.

Found by driving the heartbeat under `tracemalloc` for the compressed
equivalent of a long day. Neither is large per entry, and that is exactly why
neither would ever be noticed: a session that runs for an afternoon with six
agents in it never shows either of them, and a session that runs for a week
with agents joining under new names shows both and shows them as «the daemon
is using more memory than it did».

* `_answered_sync` records when each asker was last answered, so that a loop
  asking on every turn is answered once per cooldown. Nothing ever removed a
  key, and a key older than the cooldown decides nothing. 4,000 distinct
  askers left 4,000 entries, of which at most a handful could still matter.
* `_arrived` and `_sync_asks` are filled by the feed and emptied by the
  heartbeat. Two writers, one reader, and nothing bounding the gap: a
  heartbeat wedged on a disk it cannot write leaves the feed appending
  envelopes of up to `learnings.MAX_BODY` each, from the room, for as long as
  the room keeps sending them.

Both are pinned by counting rather than by measuring memory, in the style of
`test_colour_tables_forget_the_departed.py`: the question is whether a
structure forgets, and a byte count answers that only indirectly and only on
the machine it ran on.
"""

from __future__ import annotations

import time

import pytest

from collab import learnings
from collab.client import daemon as D
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope, now_iso


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "checkout" / ".collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save()
    return D.Daemon(profile)


def _learning(i, sender=None):
    return Envelope(seq=i, ts=now_iso(), kind=KIND_CHAT,
                    sender=sender or f"agent{i}",
                    text=f"{learnings.PREFIX} a thing",
                    body={learnings.MARKER: {"slug": f"s{i}", "title": "T",
                                             "body": "B", "repo": "host/a/b"}})


def _asks(i, sender=None):
    return Envelope(seq=i, ts=now_iso(), kind=KIND_CHAT,
                    sender=sender or f"agent{i}", text=learnings.SYNC_TEXT,
                    body={learnings.SYNC_MARKER: {"want": 3}})


# --- the queues the feed fills and the heartbeat empties -----------------------------

def test_a_heartbeat_that_never_runs_does_not_grow_the_daemon(daemon):
    """The feed is the writer and a timer is the reader. What is pinned is the
    gap between them, because everything in it came from the room."""
    for i in range(D.MAX_QUEUED_LEARNINGS * 5):
        daemon._note_any_learning(_learning(i))

    assert len(daemon._arrived) == D.MAX_QUEUED_LEARNINGS


def test_the_requests_queue_is_bounded_the_same_way(daemon):
    for i in range(D.MAX_QUEUED_LEARNINGS * 3):
        daemon._note_any_learning(_asks(i))

    assert len(daemon._sync_asks) == D.MAX_QUEUED_LEARNINGS


def test_it_is_the_oldest_that_go(daemon):
    """A learning is a fact rather than a line of conversation, so the ones to
    lose under pressure are the ones that have waited longest without being
    filed. Losing one costs a fact somebody can send again."""
    for i in range(D.MAX_QUEUED_LEARNINGS + 3):
        daemon._note_any_learning(_learning(i))

    kept = [e.seq for e in daemon._arrived]
    assert kept[0] == 3 and kept[-1] == D.MAX_QUEUED_LEARNINGS + 2


def test_what_was_dropped_is_counted_and_said(daemon):
    """Silently discarding somebody's fact is the one thing worse than
    discarding it. `collab check` reads these figures."""
    assert "dropped" not in daemon._learning_figures()

    for i in range(D.MAX_QUEUED_LEARNINGS + 7):
        daemon._note_any_learning(_learning(i))

    assert daemon._learning_figures()["dropped"] == 7


def test_an_ordinary_session_never_meets_the_cap(daemon):
    """A real sync answers with twenty at most, so the bound must be far
    enough above ordinary use to be invisible to it."""
    for i in range(20):
        daemon._note_any_learning(_learning(i))

    assert len(daemon._arrived) == 20
    assert "dropped" not in daemon._learning_figures()


# --- the cooldown map ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_cooldown_map_forgets_whoever_it_has_released(daemon):
    """An entry older than the cooldown decides nothing. It was kept anyway,
    and a session meets a name for every join."""
    old = time.time() - learnings.SYNC_COOLDOWN - 1
    daemon._answered_sync = {f"gone{i}": old for i in range(500)}
    daemon._answered_sync["recent"] = time.time()

    await daemon._answer_sync_requests()

    assert list(daemon._answered_sync) == ["recent"]


@pytest.mark.asyncio
async def test_forgetting_does_not_answer_the_same_asker_twice(daemon,
                                                               monkeypatch):
    """The pruning must not cost the map its job. Somebody inside the cooldown
    is still refused."""
    sent: list[str] = []
    monkeypatch.setattr(daemon, "_send_learning",
                        lambda one, to="": sent.append(to))
    monkeypatch.setattr(daemon, "_bundle",
                        lambda: ("host/a/b", daemon.profile.dir))
    daemon._http = object()

    daemon._sync_asks = [_asks(1, sender="carol")]
    await daemon._answer_sync_requests()
    daemon._sync_asks = [_asks(2, sender="carol")]
    await daemon._answer_sync_requests()

    assert daemon._answered_sync["carol"] > 0
    assert sent == [], "no learnings here to send, and asked twice all the same"


@pytest.mark.asyncio
async def test_the_map_does_not_grow_across_a_long_session(daemon):
    """Two thousand askers, each an hour apart. What is left is what the
    cooldown still covers."""
    now = time.time()
    for i in range(2000):
        daemon._answered_sync[f"agent{i}"] = now - (2000 - i) * 3600
        await daemon._answer_sync_requests()

    assert len(daemon._answered_sync) <= 1
