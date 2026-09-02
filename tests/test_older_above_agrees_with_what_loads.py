"""The sixth read path, and two guards that were counting the wrong thing.

`has_before` is asked whether anything older exists, and the answer labels the
pane «older above (keep scrolling, or g)». Every other read path was taught to
skip the kinds the pane does not draw; this one was not, so it answered from a
wider set than `load_older` fetches from. A daemon publishes its state before
anybody speaks, which puts undrawable rows at the very bottom of the inbox —
exactly where this question gets asked. The pane offered older messages that
were not there, and went on offering them however many times you scrolled.

The two paging guards were written to count EVENTS rather than kinds, so
`load_older` and `load_newer` could each lose their `exclude` and the whole
suite stayed green. A guard that cannot tell a message from a state update is
not guarding the thing this change is about.
"""

from __future__ import annotations

import json
import time

import pytest

from collab.client import tui as T
from collab.client.inbox import Inbox
from collab.config import SessionProfile
from collab.protocol import KIND_ACTIVITY, KIND_CHAT, Envelope, now_iso


def _chat(seq: int) -> Envelope:
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT, sender="bob",
                    text=f"message {seq}")


def _state(seq: int, what: str = "the token refresh") -> Envelope:
    """Shaped as `server.app` publishes it: `text` is a copy of `what`."""
    now = time.time()
    return Envelope(seq=seq, ts=now_iso(), kind=KIND_ACTIVITY, sender="bob",
                    text=what,
                    body={"state": "working", "what": what, "since": now,
                          "updated_at": now})


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


# STATE FIRST, THEN SPEECH. The oldest rows in a real inbox are routinely the
# ones the pane will not draw, because an agent says what it is doing before it
# says anything to anybody.
def _state_then_speech(session, messages: int = 4):
    _record(session, [_state(1), _state(2)]
            + [_chat(seq) for seq in range(3, 3 + messages)])


# --- the sixth read path ---------------------------------------------------------

def test_no_older_above_when_everything_older_is_undrawable(session):
    _state_then_speech(session)
    box = Inbox(session.dir)
    assert box.before(3, limit=1, exclude=T.NOT_CONVERSATION) == [], \
        "fixture is wrong: something drawable is older than seq 3"
    assert box.has_before(3, exclude=T.NOT_CONVERSATION) is False, \
        "offered older messages that load_older cannot produce"


def test_something_older_is_still_reported(session):
    _state_then_speech(session)
    assert Inbox(session.dir).has_before(6, exclude=T.NOT_CONVERSATION) is True


def test_the_unfiltered_answer_is_unchanged(session):
    """Callers that draw every kind still get the wider answer."""
    _state_then_speech(session)
    assert Inbox(session.dir).has_before(3) is True


def test_the_pane_does_not_promise_a_page_it_cannot_load(session):
    """The end-to-end shape of the defect: the header said there was more
    above, and reaching back returned nothing, every time it was asked."""
    _state_then_speech(session)
    model = T.Model(profile=session)
    model.load_initial(limit=2)
    for _ in range(5):
        if model.more_above():
            assert model.load_older() > 0, \
                "said older messages were above and then loaded none"


# --- the two guards, at the layer that was actually unguarded ---------------------

def test_reaching_back_pages_only_conversation(session):
    """Drives `Model.load_older`, not the inbox beneath it — the guard that
    existed counted events, so dropping the exclude here changed nothing it
    could see."""
    _state_then_speech(session, messages=8)
    model = T.Model(profile=session)
    model.load_initial(limit=2)
    model.load_older(count=50)
    assert [e.kind for e in model.events] == [KIND_CHAT] * len(model.events), \
        "a state update was paged into the conversation from above"


def test_reading_forward_pages_only_conversation(session):
    """The same hole on the other side, driven through `Model.load_newer`.

    The window is cut back by hand rather than by a small `load_start`, because
    a start that already holds everything makes `load_newer` a no-op and the
    assertion then passes without the code under test ever running — which is
    how the original guard came to survive its own mutation.
    """
    _record(session, [_chat(1), _chat(2)]
            + [_state(seq) for seq in range(3, 7)]
            + [_chat(7), _chat(8)])
    model = T.Model(profile=session)
    model.load_start()
    model.events = model.events[:2]
    model._older = None
    assert model.pending() > 0, "nothing left below: load_newer would not run"
    assert model.load_newer(count=50) > 0, "load_newer paged nothing"
    assert [e.kind for e in model.events] == [KIND_CHAT] * len(model.events), \
        "a state update was paged into the conversation from below"
