"""T_c144c7ad2773: the worker knows what its main agent said, and asks a question once."""
import json
import sys

import httpx
import pytest

from collab import worker, worker_runtime
from collab.protocol import Envelope
from test_worker_service import conversation, configure, incoming  # noqa: F401  (fixtures)


def store_on(tmp_path):
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    return store


def ask(store, *, seq, question, subject="", source_id="p_b", private=False):
    """Escalate as the daemon does: provenance from #146's decision_sources, never the model."""
    snap = store.snapshot()
    escalation = {"reason": "decision", "seq": seq, "question": question, "subject": subject}
    sources = {seq: {"sender_id": source_id, "sender": "bob", "room": "" if private else "general",
                     "broadcast": not private}} if source_id else None
    assert store.commit_turn(expected_cursor=snap["cursor"], cursor=max(seq, snap["cursor"]), summary="",
                             replies=[], escalations=[escalation], decision_sources=sources)


# --- own-sent ledger -----------------------------------------------------------------

def test_the_main_agents_sends_survive_a_restart_with_their_status(tmp_path):
    store = store_on(tmp_path)
    delivered = store.send("Rebased #141: 087570a", to="jarvis", to_id="p_j")
    failed = store.send("for the room", to="", room="general")
    store.acknowledge(delivered)
    store.reject(failed, reason="404")
    again = worker.Store(tmp_path)                       # restart: a new handle reads the same state
    ledger = {e["id"]: e for e in again.snapshot()["main_sent"]}
    assert ledger[delivered]["status"] == "delivered" and ledger[delivered]["private"] is True
    assert ledger[failed]["status"] == "failed", "a failed send must not read as delivered"


def test_repeated_delivery_is_idempotent_and_the_ledger_is_bounded(tmp_path):
    store = store_on(tmp_path)
    rid = store.send("once", to="bob", to_id="p_b")
    store.acknowledge(rid)
    store.acknowledge(rid)                               # a retried acknowledgement
    assert [e["id"] for e in store.snapshot()["main_sent"]].count(rid) == 1
    for i in range(worker.MAIN_SENT_KEEP + 5):
        store.send("x" * 900 + str(i), to="bob", to_id="p_b")
    ledger = store.snapshot()["main_sent"]
    assert len(ledger) <= worker.MAIN_SENT_KEEP
    assert len(json.dumps(ledger).encode()) <= worker.MAIN_SENT_BYTES


def test_an_ordinary_send_never_closes_a_pending_decision(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="May bob merge #141?", subject="PR#141@087570a")
    store.send("Yes, merge #141.", to="bob", to_id="p_b")             # said to bob, not an answer to the decision
    assert len(store.pending()) == 1


# --- exact-subject dedupe ------------------------------------------------------------

def test_a_new_question_on_the_same_subject_is_its_own_decision_linked_to_the_first(tmp_path):
    """jarvis's probe: 'May I take T_c144?' then 'T_c144 tests fail on Windows: ship or wait?'."""
    store = store_on(tmp_path)
    ask(store, seq=0, question="May I take T_c144?", subject="T_c144c7ad2773")
    ask(store, seq=0, question="T_c144 tests fail on Windows: ship or wait?", subject="T_c144c7ad2773")
    first, second = store.pending()
    assert second["related"] == [first["id"]] and "follow_ups" not in first, "linked, never merged"
    from collab import worker_notices
    store.configure({"agent": "claude", "scope": "Coordinate"})
    notice = worker_notices.claim(tmp_path)
    assert "fail on Windows" in notice["text"] and f"related to {first['id']}" in notice["text"]


def test_only_an_exact_duplicate_from_the_same_asker_is_dropped(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="Rebase #141 now?")
    ask(store, seq=0, question="  rebase   #141 NOW?  ")          # same words: one decision
    assert len(store.pending()) == 1
    ask(store, seq=0, question="Rebase #141 now?", source_id="p_c")  # someone else asking
    ask(store, seq=0, question="Rebase #141 now?", private=True)     # same asker, other audience
    assert len(store.pending()) == 3


def test_d4_no_subject_never_links_or_merges(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="First question?")
    ask(store, seq=0, question="Second question?")
    assert [("related" in p) for p in store.pending()] == [False, False]


def test_d3_a_decision_awaiting_the_human_is_never_joined(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="Ship 2.2 today?")
    store.await_human(store.pending()[0]["id"], "asking my human", to="bob")
    ask(store, seq=0, question="Ship 2.2 today?")                   # word-for-word, but the first is with the human
    assert len(store.pending()) == 2


# --- the service: payload, provenance, and no model calls ----------------------------

def test_the_provider_schema_and_validator_accept_a_subject_or_none():
    assert "subject" in worker_runtime.OUTPUT_SCHEMA["properties"]["escalations"]["items"]["required"]
    for escalation in ({"reason": "decision", "question": "q", "seq": 0},
                       {"reason": "decision", "question": "q", "seq": 0, "subject": "T_c144c7ad2773"}):
        out = worker_runtime.validate_result({"summary": "", "replies": [], "escalations": [escalation]})
        assert "subject" in out["escalations"][0]


@pytest.mark.asyncio
async def test_main_sent_reaches_the_turn_and_alone_starts_no_turn(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    calls = []
    async def run(*args, **kwargs):
        calls.append(args[2])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    store.send("Rebased #141: 087570a", to="", room="general")   # a public send: visible in every turn
    await conversation.turn()
    assert calls == [], "the main agent's own send is context, not a reason to spend a turn"
    incoming(conversation, 1, "Is #141 rebased?")
    await conversation.turn()
    assert calls and calls[0]["main_sent"][0]["text"] == "Rebased #141: 087570a"


@pytest.mark.asyncio
async def test_escalations_keep_their_subject_beside_the_daemon_provenance(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b",
                                              to="alice", text="May I merge #141?"))
    async def run(*args, **kwargs):
        return {"summary": "", "replies": [], "escalations": [
            {"reason": "decision", "seq": 1, "question": "May bob merge #141?", "subject": "PR#141@087570a"}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    await conversation.turn()
    row = worker.Store(conversation.root).pending()[0]
    assert row["subject"] == "PR#141@087570a" and row["decision_source"]["sender_id"] == "p_b"
    assert "source_id" not in row and "private" not in row, "one provenance: #146's decision_source"


@pytest.mark.asyncio
async def test_a_hub_rejection_during_flush_stays_failed(conversation, tmp_path):
    """arma's P1: the flush rejects a permanent refusal and then acknowledges; failed must not
    become delivered. (After #146 a 404 for a to_id send is retried, so a 403 stands in.)"""
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    rid = store.send("to a name that is gone", to="ghost", to_id="p_gone")
    async def transport(request):
        return httpx.Response(403, json={"detail": "not allowed"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation._flush(store, store.snapshot()["generation"])
    entry = next(e for e in store.snapshot()["main_sent"] if e["id"] == rid)
    assert entry["status"] == "failed", entry
    assert store.outbox() == []



def _peer(name, pid):
    return {"name": name, "id": pid}


@pytest.mark.asyncio
async def test_a_private_send_is_shown_only_in_a_turn_answering_its_recipient(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    conversation.daemon.snapshot = {"participants": [_peer("alice", "p_a"), _peer("bob", "p_b"), _peer("carol", "p_c")]}
    store = worker.Store(conversation.root)
    secret = "key rotation Friday 18:00, do not share"
    store.send(secret, to="bob", to_id="p_b")
    seen = []
    async def run(*args, **kwargs):
        seen.append(args[2]["main_sent"][0])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="carol", sender_id="p_c", text="news?"))
    await conversation.turn()
    conversation.daemon.inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id="p_b", text="and?"))
    await conversation.turn()
    assert seen[0]["text"] == "sent a private reply to bob" and seen[0]["redacted"]
    assert seen[1]["text"] == secret, "in a turn answering bob, bob's own reply is visible"


@pytest.mark.asyncio
async def test_a_reply_repeating_a_private_send_to_someone_else_is_withheld(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    conversation.daemon.snapshot = {"participants": [_peer("alice", "p_a"), _peer("bob", "p_b"), _peer("carol", "p_c")]}
    store = worker.Store(conversation.root)
    secret = "key rotation Friday 18:00, do not share"
    store.send(secret, to="bob", to_id="p_b")
    for record in store.outbox():
        store.acknowledge(record["id"])
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="carol", sender_id="p_c", text="news?"))
    async def run(*args, **kwargs):
        return worker_runtime.validate_result({"summary": "", "escalations": [], "replies": [
            {"text": f"FYI: {secret}", "to": "carol", "room": ""}]})
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"seq": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
    assert sent == [] and store.status()["dropped_replies"] == 1



@pytest.mark.asyncio
async def test_a_turn_answering_the_recipient_and_someone_else_sees_the_private_send_redacted(
        conversation, tmp_path, monkeypatch):
    """Mixed turn: a paraphrase would defeat the substring guard, so the text is not shown at all."""
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("key rotation Friday 18:00, do not share", to="bob", to_id="p_b")
    seen = []
    async def run(*args, **kwargs):
        seen.append(args[2]["main_sent"][0])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", text="and?"))
    conversation.daemon.inbox.record(Envelope(seq=2, kind="chat", sender="carol", sender_id="p_c", text="news?"))
    await conversation.turn()
    assert seen and seen[0]["redacted"] is True


@pytest.mark.asyncio
async def test_a_rename_swap_never_shows_bobbys_private_text_to_the_new_bob(conversation, tmp_path, monkeypatch):
    """jarvis's probe: bobby (p_b) renamed away, carol renamed to 'bob'."""
    configure(conversation, tmp_path)
    conversation.daemon.snapshot = {"participants": [{"name": "alice", "id": "p_a"},
                                                     {"name": "bobby", "id": "p_b"}, {"name": "bob", "id": "p_c"}]}
    store = worker.Store(conversation.root)
    store.send("key rotation Friday 18:00, do not share", to="bob", to_id="p_b")
    seen = []
    async def run(*args, **kwargs):
        seen.append(args[2]["main_sent"][0])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_c", text="hi"))
    await conversation.turn()
    assert seen and seen[0]["redacted"] is True, "the name matches, the frozen id does not"


async def _turn_with_private_send(conversation, tmp_path, monkeypatch, *, incoming_event, replies, people=None):
    configure(conversation, tmp_path)
    conversation.daemon.snapshot = {"participants": people or [
        {"name": "alice", "id": "p_a"}, {"name": "bob", "id": "p_b"}, {"name": "carol", "id": "p_c"}]}
    store = worker.Store(conversation.root)
    store.send("key rotation Friday 18:00, do not share", to="bob", to_id="p_b")
    for record in store.outbox():
        store.acknowledge(record["id"])
    conversation.daemon.inbox.record(incoming_event)
    async def run(*args, **kwargs):
        return worker_runtime.validate_result({"summary": "", "escalations": [], "replies": replies})
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content).get("text"))
        return httpx.Response(200, json={"seq": 9})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
    return store, sent


@pytest.mark.asyncio
async def test_p5_a_paraphrase_of_a_shown_private_send_never_reaches_a_room(conversation, tmp_path, monkeypatch):
    """bob asks in #general, the turn sees bob's private text in full; a paraphrase to the room is withheld."""
    store, sent = await _turn_with_private_send(
        conversation, tmp_path, monkeypatch,
        incoming_event=Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", room="general", text="when?"),
        replies=[{"text": "Rotation is set for the end of the week, evening.", "to": "bob", "room": "general"}])
    assert sent == [] and store.status()["dropped_replies"] == 1


@pytest.mark.asyncio
async def test_p6_bobs_dm_answered_in_a_room_is_withheld_while_a_dm_to_bob_passes(conversation, tmp_path, monkeypatch):
    store, sent = await _turn_with_private_send(
        conversation, tmp_path, monkeypatch,
        incoming_event=Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", to="alice", text="when?"),
        replies=[{"text": "End of the week.", "to": "bob", "room": "general"},
                 {"text": "Friday evening, as I said.", "to": "bob", "room": ""}])
    assert sent == ["Friday evening, as I said."]


@pytest.mark.asyncio
async def test_p7_the_guard_follows_to_id_not_the_stored_name(conversation, tmp_path, monkeypatch):
    """After a swap, 'bob' is carol (p_c): a DM to 'bob' repeating p_b's private text is withheld."""
    people = [{"name": "alice", "id": "p_a"}, {"name": "bobby", "id": "p_b"}, {"name": "bob", "id": "p_c"}]
    store, sent = await _turn_with_private_send(
        conversation, tmp_path, monkeypatch, people=people,
        incoming_event=Envelope(seq=1, kind="chat", sender="bob", sender_id="p_c", to="alice", text="any news?"),
        replies=[{"text": "FYI: key rotation Friday 18:00, do not share", "to": "bob", "room": ""}])
    assert sent == []


@pytest.mark.asyncio
async def test_a_redaction_after_a_swap_names_the_ids_current_holder_or_nobody(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("key rotation Friday 18:00, do not share", to="bob", to_id="p_b")
    seen = []
    async def run(*args, **kwargs):
        seen.append(args[2]["main_sent"][0])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    conversation.daemon.snapshot = {"participants": [{"name": "bobby", "id": "p_b"}, {"name": "bob", "id": "p_c"}]}
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_c", text="hi"))
    await conversation.turn()
    conversation.daemon.snapshot = {"participants": [{"name": "bob", "id": "p_c"}]}   # p_b has left
    conversation.daemon.inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id="p_c", text="hi again"))
    await conversation.turn()
    assert seen[0]["text"] == "sent a private reply to bobby" and seen[0]["to"] == "bobby"
    assert seen[1]["text"] == "sent a private reply to another participant" and seen[1]["to"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("room, delivered", [("general", False), ("", True)])
async def test_an_answer_whose_reply_the_private_guard_withholds_is_held_not_dropped(
        tmp_path, monkeypatch, room, delivered):
    """bob asks a BROADCAST question in #general, the human answers, and the
    main agent's ledger holds a private send to bob. The turn is bob's alone,
    so that send is shown in full and the DMs-only guard lets only a DM to bob
    leave. A reply in #general — the right delivery for a broadcast question
    (#162) — was withheld and the human's answer consumed with it, counted as
    one more dropped reply (review of #148, the reviewer's probe). It must be
    HELD for the main agent, never dropped; a DM still goes out."""
    from types import SimpleNamespace

    from collab.client.inbox import Inbox
    from collab.client.worker_service import Conversation

    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    inbox = Inbox(tmp_path)
    inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b", room="general",
                          to=None, text="May I merge #148?"))
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    store.send("PRIVATE: the key rotation is Friday 18:00, do not share", to="bob", to_id="p_b")
    assert store.commit_turn(
        expected_cursor=0, cursor=1, summary="", replies=[],
        escalations=[{"seq": 1, "reason": "decision", "question": "May bob merge #148?"}],
        decision_sources={1: {"sender_id": "p_b", "sender": "bob", "room": "general",
                              "broadcast": True}})
    store.answer(store.pending()[0]["id"], "Tell bob yes, after the gate.", verbatim=False)
    inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id="p_b", room="general",
                          to=None, text="any news?"))
    seen = {}

    async def run(*a, **k):
        seen["main_sent"] = a[2]["main_sent"]
        return {"summary": "", "escalations": [],
                "replies": [{"text": "Yes, after the gate.", "to": "bob", "room": room}]}

    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []

    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"seq": 9})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            daemon = SimpleNamespace(
                profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                        url="http://hub", token="t"),
                inbox=inbox, _http=client,
                snapshot={"participants": [{"name": "alice", "id": "p_a"},
                                           {"name": "bob", "id": "p_b"}]})
            await Conversation(daemon).turn()
    finally:
        inbox.close()
    assert [not e.get("redacted") for e in seen["main_sent"]] == [True]
    delivered_now = [m for m in sent if "gate" in (m.get("text") or "")]
    with store._db() as db:
        held = db.execute("SELECT COUNT(*) FROM records WHERE kind='quarantine'").fetchone()[0]
    snapshot = store.snapshot()
    assert int(snapshot.get("dropped_replies", 0)) == 0, "the answer was counted as dropped"
    if delivered:
        assert len(delivered_now) == 1 and not delivered_now[0].get("room")
        assert held == 0
    else:
        assert delivered_now == [], "a withheld reply was sent anyway"
        assert held == 1, "the human's answer was consumed instead of held"
