"""A human answer follows its original principal, while ambiguous inputs remain local."""

import json
from types import SimpleNamespace

import httpx
import pytest

from collab import worker
from collab.client.inbox import Inbox
from collab.client.worker_service import Conversation
from collab.protocol import Envelope


def roster(hub):
    return {"participants": [{"name": person.name, "id": person.id} for person in hub.participants()],
            "rooms": ["general", "ops"]}


def setup(tmp_path, session):
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    inbox = Inbox(tmp_path)
    daemon = SimpleNamespace(profile=SimpleNamespace(
        dir=tmp_path, name="alice", participant_id=session["store"].resolve_name("alice"),
        url="http://testserver", token=session["host_token"]),
        inbox=inbox, snapshot=roster(session["store"]), _http=None)
    return state, inbox, daemon


def answer(state, inbox, person, seq, text, *, room=""):
    inbox.record(Envelope(seq=seq, kind="chat", sender=person.name, sender_id=person.id,
                          to="alice", to_id="p_host", room=room, text="Synthetic source question"))
    snapshot = state.snapshot()
    assert state.commit_turn(expected_cursor=snapshot["cursor"], cursor=seq, summary="Unchanged prior summary",
        replies=[], escalations=[{"seq": seq, "reason": "decision", "question": "Synthetic question"}],
        decision_sources={seq: {"sender_id": person.id, "sender": person.name, "room": room, "broadcast": False}})
    pending = next(row for row in state.pending() if row["seq"] == seq)
    state.answer(pending["id"], text, verbatim=False)
    return pending["id"]


def held(state, record_id):
    with state._db() as db:
        row = db.execute("SELECT kind,data FROM records WHERE id=?", (record_id,)).fetchone()
    assert row is not None and row[0] == "quarantine"
    return json.loads(row[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", [("bob", "bobby"), ("bobby", "bob")])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("swap", [False, True])
async def test_a_renamed_answer_uses_the_original_id_or_holds_only_a_conflicting_alias(
        tmp_path, session, monkeypatch, old, new, stale, swap):
    """Current labels work; an unoccupied stale alias is safe, a reused alias is not guessed."""
    hub = session["store"]
    original = hub.add_participant(old, "synthetic-original")
    replacement = hub.add_participant("replacement", "synthetic-replacement")
    state, inbox, daemon = setup(tmp_path, session)
    rid = answer(state, inbox, original, 1, "PRIVATE-HUMAN-MARKER", room="ops")
    hub.rename(original.id, new)
    if swap:
        hub.rename(replacement.id, old)
    daemon.snapshot = roster(hub)
    calls = []

    async def model(*args, **kwargs):
        payload = args[2]
        calls.append(payload)
        assert payload["main_answers"][0]["source"]["sender"] == new
        assert payload["main_answers"][0]["prompt"]["decision_source"]["sender"] == new
        assert payload["main_answers"][0]["source"]["broadcast"] is False
        return {"summary": "PRIVATE-HUMAN-MARKER model summary", "replies": [
            {"to": old if stale else new, "room": "ops", "text": "Synthetic private answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
        assert len(calls) == 1
        original_messages = [event for event in hub.history(viewer=original.id) if event.text == "Synthetic private answer"]
        other_messages = [event for event in hub.history(viewer=replacement.id) if event.text == "Synthetic private answer"]
        assert not other_messages
        if stale and swap:
            assert not original_messages
            assert held(state, rid)["text"] == "PRIVATE-HUMAN-MARKER"
            assert state.snapshot()["summary"] == "Unchanged prior summary"
        else:
            assert len(original_messages) == 1
            assert original_messages[0].to_id == original.id and original_messages[0].to == new
            assert not state.snapshot()["answers"] and not state.outbox()
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_a_rejected_answer_cannot_contaminate_the_next_unrelated_turn(
        tmp_path, session, monkeypatch):
    """Discard the whole rejected result, then normalize unread old labels by ID on restart."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("replacement", "synthetic-replacement")
    other = hub.add_participant("dave", "synthetic-other")
    state, inbox, daemon = setup(tmp_path, session)
    rid = answer(state, inbox, original, 1, "HELD-SECRET-MARKER")
    hub.rename(original.id, "bobby")
    hub.rename(replacement.id, "bob")
    daemon.snapshot = roster(hub)
    inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id=original.id, text="Independent original-peer update"))
    inbox.record(Envelope(seq=3, kind="chat", sender="dave", sender_id=other.id, text="Independent question"))
    calls = []

    async def model(*args, **kwargs):
        payload = args[2]
        calls.append(payload)
        if len(calls) == 1:
            return {"summary": "HELD-SECRET-MARKER rejected summary", "replies": [
                {"to": "bob", "room": "", "text": "HELD-SECRET-MARKER rejected output"}], "escalations": []}
        assert "HELD-SECRET-MARKER" not in json.dumps(payload)
        assert [event["sender"] for event in payload["events"]] == ["bobby", "dave"]
        return {"summary": "Safe new progress", "replies": [
            {"to": "dave", "room": "", "text": "Safe unrelated answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
            assert state.snapshot()["cursor"] == 1
            assert state.snapshot()["summary"] == "Unchanged prior summary"
            await Conversation(daemon).turn()
            await Conversation(daemon).turn()
        assert len(calls) == 2 and state.snapshot()["cursor"] == 3
        assert held(state, rid)["text"] == "HELD-SECRET-MARKER"
        assert len([row for row in state.pending() if row.get("once") == "quarantine:" + rid]) == 1
        assert not any("HELD-SECRET-MARKER" in (event.text or "") for event in hub.history())
        assert any(event.text == "Safe unrelated answer" for event in hub.history(viewer=other.id))
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_an_ambiguous_answer_is_held_before_a_valid_answer_reaches_the_model(
        tmp_path, session, monkeypatch):
    """One malformed principal mapping cannot block or disclose an unrelated valid instruction."""
    hub = session["store"]
    bad = hub.add_participant("bob", "synthetic-bad")
    good = hub.add_participant("dave", "synthetic-good")
    state, inbox, daemon = setup(tmp_path, session)
    bad_id = answer(state, inbox, bad, 1, "AMBIGUOUS-HELD-SECRET")
    answer(state, inbox, good, 2, "Valid private answer")
    daemon.snapshot["participants"].append({"name": "another-alias", "id": bad.id})
    calls = []

    async def model(*args, **kwargs):
        payload = args[2]
        calls.append(payload)
        assert "AMBIGUOUS-HELD-SECRET" not in json.dumps(payload)
        assert [row["source"]["sender_id"] for row in payload["main_answers"]] == [good.id]
        return {"summary": "Valid answer handled", "replies": [
            {"to": "dave", "room": "", "text": "Valid delivered answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
            await Conversation(daemon).turn()
        assert len(calls) == 1
        assert held(state, bad_id)["text"] == "AMBIGUOUS-HELD-SECRET"
        assert any(event.text == "Valid delivered answer" for event in hub.history(viewer=good.id))
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_a_rename_during_the_model_call_keeps_its_frozen_id_and_restart_progress(
        tmp_path, session, monkeypatch):
    """No after-await roster lookup may substitute the participant taking the old label."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("replacement", "synthetic-replacement")
    state, inbox, daemon = setup(tmp_path, session)
    answer(state, inbox, original, 1, "Synthetic private human instruction")
    calls = []

    async def model(*args, **kwargs):
        payload = args[2]
        calls.append(payload)
        if len(calls) == 1:
            assert payload["main_answers"][0]["source"]["sender"] == "bob"
            hub.rename(original.id, "bobby")
            hub.rename(replacement.id, "bob")
            daemon.snapshot = roster(hub)
            label = "bob"
        else:
            assert payload["events"][0]["sender"] == "bobby"
            label = "bobby"
        return {"summary": "Safe progress", "replies": [
            {"to": label, "room": "", "text": "Synthetic frozen-ID answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
            inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id=original.id, text="Next independent question"))
            await Conversation(daemon).turn()
        assert len(calls) == 2 and state.snapshot()["cursor"] == 2
        assert len([event for event in hub.history(viewer=original.id) if event.text == "Synthetic frozen-ID answer"]) == 2
        assert not any(event.text == "Synthetic frozen-ID answer" for event in hub.history(viewer=replacement.id))
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_quarantine_at_full_capacity_reuses_the_record_and_can_be_acknowledged_locally(
        tmp_path, session, monkeypatch):
    """No extra escalation slot may roll back the hold or repeat its private text."""
    person = session["store"].add_participant("bob", "synthetic-original")
    state, inbox, daemon = setup(tmp_path, session)
    rid = answer(state, inbox, person, 1, "FULL-CAPACITY-PRIVATE-MARKER")
    daemon.snapshot["participants"].append({"id": person.id, "name": "ambiguous-name"})
    monkeypatch.setattr(worker, "MAX_PENDING", 1)

    async def forbidden(*args, **kwargs):
        raise AssertionError("a fully quarantined input must not invoke a model")

    monkeypatch.setattr("collab.worker_runtime.run_turn", forbidden)
    try:
        for _ in range(3):
            await Conversation(daemon).turn()
        notice = state.pending()[0]
        assert notice["id"] == rid and notice["local_only"] is True
        assert "FULL-CAPACITY-PRIVATE-MARKER" not in json.dumps(notice)
        assert worker.pending_count_readonly(tmp_path) == 1
        with state._db() as db:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        with pytest.raises(ValueError, match="cannot be forwarded"):
            state.answer(rid, "Do not forward", verbatim=True)
        assert state.await_human(rid, "PRIVATE-LOCAL-WAIT-NOTE") == ""
        assert state.pending()[0]["awaiting_human"] is True
        assert "PRIVATE-LOCAL-WAIT-NOTE" not in json.dumps(state.pending())
        assert worker.pending_count_readonly(tmp_path) == 0
        assert not state.snapshot()["answers"] and not state.outbox()
        state.answer(rid, "Reviewed locally", verbatim=False)
        assert not state.pending() and worker.pending_count_readonly(tmp_path) == 0
        assert not state.snapshot()["answers"] and not state.outbox()
        assert held(state, rid)["text"] == "FULL-CAPACITY-PRIVATE-MARKER"
        assert held(state, rid)["recovery_resolved"] is True
        assert state.status()["held_answer_count"] == 1
        await Conversation(daemon).turn()
        assert not state.pending()
    finally:
        inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_room", ["general", "not-in-snapshot"])
async def test_a_private_answer_cannot_change_its_source_room_after_a_rename(
        tmp_path, session, monkeypatch, target_room):
    """Current alias support does not expand the private source's room boundary."""
    person = session["store"].add_participant("bob", "synthetic-original")
    state, inbox, daemon = setup(tmp_path, session)
    rid = answer(state, inbox, person, 1, "PRIVATE-ROOM-SECRET", room="ops")
    session["store"].rename(person.id, "bobby")
    daemon.snapshot = roster(session["store"])

    async def model(*args, **kwargs):
        return {"summary": "Must be discarded", "replies": [
            {"to": "bobby", "room": target_room, "text": "Must not be delivered"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
        assert held(state, rid)["text"] == "PRIVATE-ROOM-SECRET"
        assert state.snapshot()["summary"] == "Unchanged prior summary"
        assert not any(event.text == "Must not be delivered" for event in session["store"].history())
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_two_historical_claimants_without_current_alias_evidence_are_held_individually(
        tmp_path, session, monkeypatch):
    """Conflicting stale labels never put either private answer into another peer's turn."""
    hub = session["store"]
    first = hub.add_participant("bob", "synthetic-first")
    state, inbox, daemon = setup(tmp_path, session)
    first_id = answer(state, inbox, first, 1, "FIRST-HELD-SECRET")
    hub.rename(first.id, "bobby")
    second = hub.add_participant("bob", "synthetic-second")
    second_id = answer(state, inbox, second, 2, "SECOND-HELD-SECRET")
    good = hub.add_participant("dave", "synthetic-good")
    answer(state, inbox, good, 3, "Valid unrelated instruction")
    daemon.snapshot = {"participants": [{"name": "bob"}, {"name": "dave"}]}

    async def model(*args, **kwargs):
        payload = args[2]
        assert "FIRST-HELD-SECRET" not in json.dumps(payload)
        assert "SECOND-HELD-SECRET" not in json.dumps(payload)
        assert [row["source"]["sender_id"] for row in payload["main_answers"]] == [good.id]
        return {"summary": "Safe progress", "replies": [
            {"to": "dave", "room": "", "text": "Safe unrelated reply"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
        assert held(state, first_id)["text"] == "FIRST-HELD-SECRET"
        assert held(state, second_id)["text"] == "SECOND-HELD-SECRET"
        assert any(event.text == "Safe unrelated reply" for event in hub.history(viewer=good.id))
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_a_stale_model_result_cannot_quarantine_answers_owned_by_a_new_configuration(
        tmp_path, session, monkeypatch):
    """The new failure path must retain the same generation fence as successful commits."""
    hub = session["store"]
    person = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("replacement", "synthetic-replacement")
    state, inbox, daemon = setup(tmp_path, session)
    rid = answer(state, inbox, person, 1, "Synthetic private instruction")
    hub.rename(person.id, "bobby")
    hub.rename(replacement.id, "bob")
    daemon.snapshot = roster(hub)

    async def model(*args, **kwargs):
        state.configure({"agent": "command", "command": ["never-executed"], "scope": "New owner configuration"})
        return {"summary": "Stale summary", "replies": [
            {"to": "bob", "room": "", "text": "Stale answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        await Conversation(daemon).turn()
        assert [row["id"] for row in state.snapshot()["answers"]] == [rid]
        assert not state.pending() and not state.outbox()
        assert state.snapshot()["summary"] == "Unchanged prior summary"
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_a_conflicting_model_alias_holds_only_its_answer_and_retries_the_other_without_it(
        tmp_path, session, monkeypatch):
    """Discard all outputs of the contaminated turn, retaining valid answers for a clean call."""
    hub = session["store"]
    bad = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("replacement", "synthetic-replacement")
    good = hub.add_participant("dave", "synthetic-good")
    state, inbox, daemon = setup(tmp_path, session)
    bad_id = answer(state, inbox, bad, 1, "BAD-HELD-PRIVATE-MARKER")
    good_id = answer(state, inbox, good, 2, "Valid human instruction")
    hub.rename(bad.id, "bobby")
    hub.rename(replacement.id, "bob")
    daemon.snapshot = roster(hub)
    calls = []

    async def model(*args, **kwargs):
        payload = args[2]
        calls.append(payload)
        if len(calls) == 1:
            return {"summary": "BAD-HELD-PRIVATE-MARKER contaminated summary", "replies": [
                {"to": "bob", "room": "", "text": "Must be discarded"},
                {"to": "dave", "room": "", "text": "Also discarded this turn"}], "escalations": []}
        assert "BAD-HELD-PRIVATE-MARKER" not in json.dumps(payload)
        assert [row["id"] for row in payload["main_answers"]] == [good_id]
        return {"summary": "Clean progress", "replies": [
            {"to": "dave", "room": "", "text": "Clean valid answer"}], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
            assert [row["id"] for row in state.snapshot()["answers"]] == [good_id]
            assert not state.outbox()
            assert state.snapshot()["summary"] == "Unchanged prior summary"
            await Conversation(daemon).turn()
        assert len(calls) == 2
        assert held(state, bad_id)["text"] == "BAD-HELD-PRIVATE-MARKER"
        assert not any(event.text in ("Must be discarded", "Also discarded this turn") for event in hub.history())
        assert any(event.text == "Clean valid answer" for event in hub.history(viewer=good.id))
    finally:
        inbox.close()
