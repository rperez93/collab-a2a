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
