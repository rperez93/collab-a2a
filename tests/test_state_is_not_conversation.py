"""An agent saying what it is working on is not an agent saying something.

`collab working` and `collab idle` publish KIND_ACTIVITY, and the hub sends it
to everyone exactly as it sends a message. It is not one: it is that agent's
STATE, and the answer to «what is bob doing» is whatever the last one said, not
the list of every one he has ever sent.

The conversation pane had no case for the kind, so it fell through to
`_wrap(env.text or str(env.body), width)` and drew it in a bubble. That lands
two different ways, and the quieter one is the worse:

* `collab working "the token refresh"` — the hub copies `what` into the
  envelope's `text`, so the pane drew a perfectly ordinary bubble reading «the
  token refresh» over bob's name, indistinguishable from bob saying it.
* `collab idle` with no note — `text` is empty, so it fell through to
  `str(env.body)` and drew a raw Python dict.

This project asks agents to publish their activity constantly, so the effect
grew with how well an agent behaved.
"""

from __future__ import annotations

import json
import time

import pytest

from collab.client import tui as T
from collab.client import watch as W
from collab.client.inbox import Inbox
from collab.config import SessionProfile
from collab.protocol import KIND_ACTIVITY, KIND_CHAT, Envelope, now_iso


def _chat(seq, text="something said out loud"):
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT, sender="bob",
                    text=text)


def _state(seq, what="the token refresh", state="working"):
    """Shaped exactly as `server.app` publishes it: `text` is a copy of `what`."""
    now = time.time()
    body = {"state": state, "what": what, "files": ["src/api/auth.py"],
            "since": now, "updated_at": now}
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_ACTIVITY, sender="bob",
                    text=what, body=body)


def _idle(seq):
    """`collab idle` with no note — the one that drew the raw dict."""
    return _state(seq, what="", state="idle")


@pytest.fixture()
def session(tmp_path):
    home = tmp_path / "collab"
    directory = home / "sessions" / "s"
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(json.dumps({"participants": []}))
    return SessionProfile(session_id="s", url="u", name="me", host_name="host",
                          token="t", home=str(home))


def _record(session, events):
    inbox = Inbox(session.dir)
    for env in events:
        inbox.record(env)
    inbox.close()


def _model(session, limit=T.WINDOW):
    model = T.Model(profile=session)
    model.load_initial(limit=limit)
    return model


def _drawn(model):
    return "\n".join(row.text for row in
                     T.conversation_rows(model.events, 70, "me", set()))


# --- the pane ----------------------------------------------------------------

def test_a_state_update_is_not_a_bubble_in_the_conversation(session):
    _record(session, [_chat(1), _state(2), _chat(3, "and another")])
    assert [e.kind for e in _model(session).events] == [KIND_CHAT, KIND_CHAT]


def test_it_does_not_read_as_something_the_agent_said(session):
    """The hub copies `what` into `text`, so this drew an ordinary bubble.

    «the token refresh», over bob's name, in the same shape as every sentence
    bob has ever typed — and the reader has no way to tell that bob did not say
    it.
    """
    _record(session, [_chat(1), _state(2, "the token refresh")])
    assert "the token refresh" not in _drawn(_model(session))


def test_an_idle_with_no_note_does_not_dump_its_body_on_screen(session):
    """The other half: no `what`, so `text` is empty and `str(body)` won.

    It drew `{'state': 'idle', 'what': '', 'files': [...], …}`.
    """
    _record(session, [_chat(1), _idle(2)])
    drawn = _drawn(_model(session))
    assert "{" not in drawn and "'state'" not in drawn


def test_a_session_of_nothing_but_state_reads_as_an_empty_conversation(session):
    """Not as a wall of them. Two agents working and not talking is quiet."""
    _record(session, [_state(n) for n in range(1, 21)])
    assert _model(session).events == []


# --- and the counts have to agree with the pane ------------------------------

def test_the_count_below_does_not_promise_messages_that_never_appear(session):
    """«3 new below», press End, nothing moves.

    The count came from the log and the pane from the log MINUS these, so a
    busy agent publishing its activity inflated the one without touching the
    other. A number that cannot be made to come true is worse than no number.
    """
    _record(session, [_chat(1)])
    model = _model(session)
    _record(session, [_state(2), _state(3), _state(4)])

    assert model.pending() == 0


def test_a_real_message_after_them_is_still_counted(session):
    """The guard must not have been bought by counting nothing at all."""
    _record(session, [_chat(1)])
    model = _model(session)
    _record(session, [_state(2), _chat(3, "over here")])

    assert model.pending() == 1


def test_reaching_back_returns_a_full_page_of_conversation(session):
    """Filtering after the fetch rather than inside it returns a short page.

    Ten asked for, ten conversation events back — not ten rows of which half
    are dropped on the way out.
    """
    events, seq = [], 1
    for _ in range(30):
        events.append(_chat(seq))
        seq += 1
        events.append(_state(seq))
        seq += 1
    _record(session, events)

    model = _model(session, limit=5)
    before = len(model.events)
    model.load_older(count=10)
    assert len(model.events) - before == 10


def test_the_live_tail_reads_them_off_the_log_and_drops_them(session):
    """The one path that meets a raw line rather than a query.

    `poll_events` only reads `inbox.jsonl` when `pending()` is zero — when a
    real message is waiting it reloads the end through the filtered queries
    instead. So the file-reading branch is reached exactly when NOTHING but
    state has arrived, which is also the only moment it can do damage.
    """
    _record(session, [_chat(1)])
    model = _model(session)
    _record(session, [_state(2), _state(3)])

    assert model.pending() == 0, "this test is not exercising the tail branch"
    assert model.poll_events(follow=True) == 0
    assert all(e.kind == KIND_CHAT for e in model.events)


def test_a_message_arriving_behind_them_still_lands(session):
    """The mixed case, which reloads the end through the queries instead."""
    _record(session, [_chat(1)])
    model = _model(session)
    _record(session, [_state(2), _chat(3, "still here")])

    model.poll_events(follow=True)
    assert all(e.kind == KIND_CHAT for e in model.events)
    assert model.events[-1].text == "still here"


# --- but the agent-facing stream still carries them --------------------------

def test_the_event_stream_still_has_them(session):
    """`collab listen` is an agent's feed and wants every transition."""
    _record(session, [_chat(1), _state(2)])
    inbox = Inbox(session.dir)
    try:
        kinds = [e.kind for e in inbox.all_events(limit=50)]
    finally:
        inbox.close()
    assert KIND_ACTIVITY in kinds


def test_the_plain_transcript_renders_them_as_state_not_as_a_dict():
    """`collab watch --no-follow` has a case for the kind, and keeps it."""
    line = W.format_event(_state(2))
    assert "working on the token refresh" in line
    assert "{" not in line and "'state'" not in line
