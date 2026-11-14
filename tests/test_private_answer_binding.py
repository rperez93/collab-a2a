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


@pytest.mark.asyncio
async def test_an_answer_for_bob_and_a_reply_to_carol_in_one_turn_are_both_delivered(tmp_path, monkeypatch):
    """jarvis's acceptance: an unrelated reply never holds anyone's answer."""
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    inbox = Inbox(tmp_path)
    inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", to="alice", text="May I merge #148?"))
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    assert store.commit_turn(expected_cursor=0, cursor=1, summary="", replies=[],
                             escalations=[{"seq": 1, "reason": "decision", "question": "May bob merge #148?"}],
                             decision_sources={1: {"sender_id": "p_b", "sender": "bob", "room": "",
                                                   "broadcast": False}})
    store.answer(store.pending()[0]["id"], "Tell bob yes, after the merge gate passes.", verbatim=False)
    inbox.record(Envelope(seq=2, kind="chat", sender="carol", sender_id="p_c", to="alice", text="Any news on R17?"))
    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": [
            {"text": "Yes, after the gate.", "to": "bob", "room": ""},
            {"text": "R17 numbers are posted in general.", "to": "carol", "room": ""}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content).get("text"))
        return httpx.Response(200, json={"seq": 9})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                                             url="http://hub", token="t"),
                                     inbox=inbox, _http=client,
                                     snapshot={"participants": [{"name": "alice", "id": "p_a"},
                                                                {"name": "bob", "id": "p_b"},
                                                                {"name": "carol", "id": "p_c"}]})
            await Conversation(daemon).turn()
    finally:
        inbox.close()
    with store._db() as db:
        held = db.execute("SELECT COUNT(*) FROM records WHERE kind='quarantine'").fetchone()[0]
    assert sorted(sent) == ["R17 numbers are posted in general.", "Yes, after the gate."] and held == 0
    assert not store.snapshot()["answers"]


async def _mixed_turn(tmp_path, monkeypatch, replies, *, askers=("bob",)):
    """One turn carrying a human answer for each asker (private questions), a
    DM from carol, and the given model replies. Returns (sent texts, held count,
    answers still queued)."""
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    ids = {"bob": "p_b", "dave": "p_d"}
    inbox = Inbox(tmp_path)
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    for seq, name in enumerate(askers, start=1):
        inbox.record(Envelope(seq=seq, kind="chat", sender=name, sender_id=ids[name], to="alice",
                              text=f"May I merge #{147 + seq}?"))
        assert store.commit_turn(expected_cursor=seq - 1, cursor=seq, summary="", replies=[],
                                 escalations=[{"seq": seq, "reason": "decision",
                                               "question": f"May {name} merge #{147 + seq}?"}],
                                 decision_sources={seq: {"sender_id": ids[name], "sender": name,
                                                         "room": "", "broadcast": False}})
    for row, name in zip(store.pending(), askers):
        store.answer(row["id"], f"Tell {name} yes, after the merge gate passes.", verbatim=False)
    inbox.record(Envelope(seq=len(askers) + 1, kind="chat", sender="carol", sender_id="p_c", to="alice",
                          text="Any news on R17?"))

    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": replies}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []

    async def transport(request):
        sent.append(json.loads(request.content).get("text"))
        return httpx.Response(200, json={"seq": 9})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                                             url="http://hub", token="t"),
                                     inbox=inbox, _http=client,
                                     snapshot={"participants": [{"name": "alice", "id": "p_a"},
                                                                {"name": "bob", "id": "p_b"},
                                                                {"name": "carol", "id": "p_c"},
                                                                {"name": "dave", "id": "p_d"}]})
            await Conversation(daemon).turn()
    finally:
        inbox.close()
    with store._db() as db:
        held = db.execute("SELECT COUNT(*) FROM records WHERE kind='quarantine'").fetchone()[0]
    return sorted(sent), held, len(store.snapshot()["answers"])


@pytest.mark.asyncio
async def test_an_answer_whose_only_carrier_went_to_someone_else_is_held_not_consumed(
        tmp_path, monkeypatch):
    """#162 review, case M3: the model's only reply carrying bob's answer went
    to carol. That reply is withheld — and the answer must not be consumed with
    nothing delivered: it is held for the main agent's local review."""
    sent, held, left = await _mixed_turn(tmp_path, monkeypatch, [
        {"text": "Tell bob yes, after the merge gate passes.", "to": "carol", "room": ""}])
    assert sent == [] and held == 1


@pytest.mark.asyncio
async def test_a_reply_to_carol_repeating_bobs_answer_is_withheld_and_bob_still_answered(
        tmp_path, monkeypatch):
    """#162 review, case M2 (mutants X2, X4): the answer text reaches only its
    asker; carol's copy is withheld, bob's delivery stands, nothing is held."""
    sent, held, left = await _mixed_turn(tmp_path, monkeypatch, [
        {"text": "Yes, after the gate.", "to": "bob", "room": ""},
        {"text": "FYI: Tell bob yes, after the merge gate passes.", "to": "carol", "room": ""}])
    assert sent == ["Yes, after the gate."] and held == 0 and left == 0


@pytest.mark.asyncio
async def test_a_wrong_room_for_one_asker_holds_only_that_askers_answer(tmp_path, monkeypatch):
    """#162 review, case M6 (mutant X1): dave's reply in a room his private
    question did not allow holds dave's answer only; bob's is kept, not lost."""
    sent, held, left = await _mixed_turn(tmp_path, monkeypatch, [
        {"text": "Yes, after the gate.", "to": "bob", "room": ""},
        {"text": "Yes, after the gate, dave.", "to": "dave", "room": "general"}],
        askers=("bob", "dave"))
    assert held == 1 and left == 1
