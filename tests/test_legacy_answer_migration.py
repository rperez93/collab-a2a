"""T_e9a523bbbcdb: an answer queued before #146 never blocks the worker, and is never misdelivered."""
import json
import sqlite3

import pytest

from collab import worker
from collab.protocol import Envelope


def store_on(tmp_path):
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    return store


def legacy_answer(store, *, seq, text="Yes, merge it.", record_id=None):
    """Write an answer row exactly as a pre-#146 build left it: a peer seq, no decision_source."""
    with store._db() as db:
        rid = store._add(db, "answer", {"text": text, "prompt": {"reason": "decision", "seq": seq,
                                                                  "question": "May I merge?"}})
    return rid


def kinds(store):
    with store._db() as db:
        return {row[0]: row[1] for row in db.execute("SELECT id, kind FROM records")}


SOURCES = {7: {"sender_id": "p_b", "sender": "bob", "room": "general", "broadcast": True},
           8: {"sender_id": "p_c", "sender": "carol", "room": "", "broadcast": False}}


def test_mixed_queue_repairs_exact_sources_and_quarantines_the_rest(tmp_path):
    store = store_on(tmp_path)
    public = legacy_answer(store, seq=7)
    private = legacy_answer(store, seq=8)
    unknown = legacy_answer(store, seq=99)
    result = store.repair_legacy_answers(SOURCES.get)
    assert result == {"repaired": 2, "quarantined": 1}
    answers = {a["id"]: a for a in store.snapshot()["answers"]}
    assert answers[public]["prompt"]["decision_source"] == {"sender_id": "p_b", "sender": "bob",
                                                            "room": "general", "broadcast": True}
    assert answers[private]["prompt"]["decision_source"]["broadcast"] is False, "the private audience is kept"
    assert kinds(store)[unknown] == "quarantine" and unknown not in answers, "never runnable"


def test_ambiguous_or_identity_less_sources_stay_quarantined(tmp_path):
    store = store_on(tmp_path)
    no_id = legacy_answer(store, seq=5)
    store.repair_legacy_answers({5: {"sender_id": "", "sender": "bob", "room": "general"}}.get)
    assert kinds(store)[no_id] == "quarantine", "a name alone is not provenance"


def test_one_recovery_decision_per_record_and_restart_idempotence(tmp_path):
    store = store_on(tmp_path)
    rid = legacy_answer(store, seq=99, text="the secret plan")
    store.repair_legacy_answers(SOURCES.get)
    again = worker.Store(tmp_path)                          # restart
    assert again.repair_legacy_answers(SOURCES.get) == {"repaired": 0, "quarantined": 0}
    notices = [p for p in again.pending() if p.get("once") == f"quarantine:{rid}"]
    assert len(notices) == 1 and rid in notices[0]["question"]
    with again._db() as db:
        kept = json.loads(db.execute("SELECT data FROM records WHERE id=?", (rid,)).fetchone()[0])
    assert kept["text"] == "the secret plan" and kept["prompt"]["seq"] == 99, "content and references intact"


@pytest.mark.asyncio
async def test_the_worker_keeps_running_past_a_legacy_answer(tmp_path, monkeypatch):
    """Before: every turn raised ValueError and no peer was answered. Now the turn proceeds."""
    from types import SimpleNamespace
    from collab.client.inbox import Inbox
    from collab.client.worker_service import Conversation
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    inbox = Inbox(tmp_path)
    daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                                     url="http://hub", token="t"),
                             inbox=inbox, snapshot={"participants": [{"name": "alice", "id": "p_a"},
                                                                     {"name": "bob", "id": "p_b"}]}, _http=None)
    service = Conversation(daemon)
    store = store_on(tmp_path)
    inbox.record(Envelope(seq=4, kind="chat", sender="bob", sender_id="p_b", room="general", text="May I merge?"))
    legacy_answer(store, seq=4)
    legacy_answer(store, seq=77)                            # its source is gone: quarantined
    calls = []
    async def run(*args, **kwargs):
        calls.append(args[2])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    try:
        await service.turn()
    finally:
        inbox.close()
    assert calls, "the turn ran instead of raising"
    assert [a["prompt"]["decision_source"]["sender_id"] for a in calls[0]["main_answers"]] == ["p_b"]
    assert not store.status()["error"]


def test_a_rename_or_name_reuse_cannot_redirect_a_repair(tmp_path):
    """Provenance is the event's sender id; the current owner of the name is irrelevant."""
    store = store_on(tmp_path)
    rid = legacy_answer(store, seq=7)
    store.repair_legacy_answers({7: {"sender_id": "p_b", "sender": "bob", "room": "", "broadcast": False}}.get)
    answer = next(a for a in store.snapshot()["answers"] if a["id"] == rid)
    assert answer["prompt"]["decision_source"]["sender_id"] == "p_b"
