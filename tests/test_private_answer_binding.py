"""T_9e266ef03035: an answer to a private question that carried a room is deliverable by DM."""
import json

import httpx
import pytest

from collab import worker
from collab.client.inbox import Inbox
from collab.client.worker_service import Conversation
from collab.protocol import Envelope
from types import SimpleNamespace


async def _answer_turn(tmp_path, monkeypatch, *, broadcast, reply):
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    inbox = Inbox(tmp_path)
    # bob's question: a DM to alice that still carries room 'general' (private), or a room broadcast.
    inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", room="general",
                          to=None if broadcast else "alice", text="May I merge #148?"))
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    assert store.commit_turn(expected_cursor=0, cursor=1, summary="", replies=[],
                             escalations=[{"seq": 1, "reason": "decision", "question": "May bob merge #148?"}],
                             decision_sources={1: {"sender_id": "p_b", "sender": "bob", "room": "general",
                                                   "broadcast": broadcast}})
    store.answer(store.pending()[0]["id"], "Tell bob yes, after the gate.", verbatim=False)
    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": [reply]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"seq": 9})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                                             url="http://hub", token="t"),
                                     inbox=inbox, _http=client,
                                     snapshot={"participants": [{"name": "alice", "id": "p_a"},
                                                                {"name": "bob", "id": "p_b"}]})
            await Conversation(daemon).turn()
    finally:
        inbox.close()
    with store._db() as db:
        quarantined = db.execute("SELECT COUNT(*) FROM records WHERE kind='quarantine'").fetchone()[0]
    return store, sent, quarantined


@pytest.mark.asyncio
async def test_a_private_question_with_a_room_is_answered_by_dm(tmp_path, monkeypatch):
    store, sent, quarantined = await _answer_turn(
        tmp_path, monkeypatch, broadcast=False, reply={"text": "Yes, after the gate.", "to": "bob", "room": ""})
    assert [m.get("text") for m in sent] == ["Yes, after the gate."] and quarantined == 0
    assert not store.snapshot()["answers"], "consumed"


@pytest.mark.asyncio
async def test_a_private_question_is_never_answered_in_the_room(tmp_path, monkeypatch):
    store, sent, quarantined = await _answer_turn(
        tmp_path, monkeypatch, broadcast=False,
        reply={"text": "Yes, after the gate.", "to": "bob", "room": "general"})
    assert sent == [], "a private question's answer never goes to the room"


@pytest.mark.asyncio
@pytest.mark.parametrize("room", ["", "general"])
async def test_a_public_question_may_be_answered_in_its_room_or_by_dm(tmp_path, monkeypatch, room):
    store, sent, quarantined = await _answer_turn(
        tmp_path, monkeypatch, broadcast=True, reply={"text": "Yes, after the gate.", "to": "bob", "room": room})
    assert [m.get("text") for m in sent] == ["Yes, after the gate."] and quarantined == 0
