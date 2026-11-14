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
    snap = store.snapshot()
    escalation = {"reason": "decision", "seq": seq, "question": question, "subject": subject}
    if source_id:
        escalation.update(source_id=source_id, private=private)
    assert store.commit_turn(expected_cursor=snap["cursor"], cursor=max(seq, snap["cursor"]), summary="",
                             replies=[], escalations=[escalation])


# --- own-sent ledger -----------------------------------------------------------------

def test_the_main_agents_sends_survive_a_restart_with_their_status(tmp_path):
    store = store_on(tmp_path)
    delivered = store.send("Rebased #141: 087570a", to="jarvis")
    failed = store.send("for the room", to="", room="general")
    store.acknowledge(delivered)
    store.reject(failed, reason="404")
    again = worker.Store(tmp_path)                       # restart: a new handle reads the same state
    ledger = {e["id"]: e for e in again.snapshot()["main_sent"]}
    assert ledger[delivered]["status"] == "delivered" and ledger[delivered]["private"] is True
    assert ledger[failed]["status"] == "failed", "a failed send must not read as delivered"


def test_repeated_delivery_is_idempotent_and_the_ledger_is_bounded(tmp_path):
    store = store_on(tmp_path)
    rid = store.send("once", to="bob")
    store.acknowledge(rid)
    store.acknowledge(rid)                               # a retried acknowledgement
    assert [e["id"] for e in store.snapshot()["main_sent"]].count(rid) == 1
    for i in range(worker.MAIN_SENT_KEEP + 5):
        store.send("x" * 900 + str(i), to="bob")
    ledger = store.snapshot()["main_sent"]
    assert len(ledger) <= worker.MAIN_SENT_KEEP
    assert len(json.dumps(ledger).encode()) <= worker.MAIN_SENT_BYTES


def test_an_ordinary_send_never_closes_a_pending_decision(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="May bob merge #141?", subject="PR#141@087570a")
    store.send("Yes, merge #141.", to="bob")             # said to bob, not an answer to the decision
    assert len(store.pending()) == 1


# --- exact-subject dedupe ------------------------------------------------------------

def test_the_same_versioned_subject_from_the_same_asker_joins_one_decision(tmp_path):
    store = store_on(tmp_path)
    ask(store, seq=0, question="Rebase #141 now?", subject="PR#141@087570a")
    ask(store, seq=0, question="Is the #141 rebase go-ahead accurate?", subject="PR#141@087570a")
    pending = store.pending()
    assert len(pending) == 1 and pending[0]["follow_ups"] == ["Is the #141 rebase go-ahead accurate?"]


@pytest.mark.parametrize("second", [
    {"subject": "PR#141@bddbfba"},                  # changed SHA: a new question
    {"source_id": "p_c"},                            # same subject, someone else asking
    {"private": True},                               # same asker, private versus public
    {"subject": ""},                                 # no subject: never fuzzy-matched
    {"source_id": ""},                               # unknown asker: no dedupe
])
def test_anything_short_of_an_exact_match_stays_a_separate_decision(tmp_path, second):
    store = store_on(tmp_path)
    ask(store, seq=0, question="Rebase #141 now?", subject="PR#141@087570a")
    ask(store, seq=0, **{"question": "Rebase #141 now, really?", "subject": "PR#141@087570a", **second})
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
    store.send("Rebased #141: 087570a", to="bob")
    await conversation.turn()
    assert calls == [], "the main agent's own send is context, not a reason to spend a turn"
    incoming(conversation, 1, "Is #141 rebased?")
    await conversation.turn()
    assert calls and calls[0]["main_sent"][0]["text"] == "Rebased #141: 087570a"


@pytest.mark.asyncio
async def test_escalations_carry_the_askers_id_and_audience_from_the_source(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b",
                                              to="alice", text="May I merge #141?"))
    async def run(*args, **kwargs):
        return {"summary": "", "replies": [], "escalations": [
            {"reason": "decision", "seq": 1, "question": "May bob merge #141?", "subject": "PR#141@087570a"}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    await conversation.turn()
    row = worker.Store(conversation.root).pending()[0]
    assert (row["source_id"], row["private"], row["subject"]) == ("p_b", True, "PR#141@087570a")


@pytest.mark.asyncio
async def test_a_hub_rejection_during_flush_stays_failed(conversation, tmp_path):
    """arma's P1: the flush rejects on 404 and then acknowledges; failed must not become delivered."""
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    rid = store.send("to a name that is gone", to="ghost")
    async def transport(request):
        return httpx.Response(404, json={"detail": "no such participant"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation._flush(store, store.snapshot()["generation"])
    entry = next(e for e in store.snapshot()["main_sent"] if e["id"] == rid)
    assert entry["status"] == "failed", entry
    assert store.outbox() == []
