"""Human decision text uses its recorded peer identity, never a recycled name."""

from types import SimpleNamespace

import httpx
import pytest

from collab import worker
from collab.client.inbox import Inbox
from collab.client.worker_service import Conversation
from collab.protocol import Envelope


def _store(path, *, source=True, broadcast=False, sender_id="p_original"):
    store = worker.Store(path)
    store.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    sources = {1: {"sender_id": sender_id, "sender": "bob", "room": "general",
                   "broadcast": broadcast}} if source else {}
    assert store.commit_turn(expected_cursor=0, cursor=1, summary="", replies=[],
                             escalations=[{"seq": 1, "reason": "decision", "question": "Synthetic question"}],
                             decision_sources=sources)
    return store, store.pending()[0]["id"]


@pytest.mark.parametrize("operation", ["await", "answer"])
def test_a_human_decision_without_a_verified_identity_stays_pending(tmp_path, operation):
    """Legacy name-only decisions cannot authorize a private reply to today's holder."""
    store, decision = _store(tmp_path, source=False)
    with pytest.raises(ValueError, match="verified peer identity"):
        if operation == "await":
            store.await_human(decision, "Synthetic waiting note", to="bob")
        else:
            store.answer(decision, "Synthetic human answer", verbatim=True, notify_to="bob")
    assert store.pending()[0]["id"] == decision
    assert not store.outbox()


def test_a_private_question_does_not_authorize_a_room_reply(tmp_path):
    """Even a known sender identity cannot turn private human text into a broadcast."""
    store, decision = _store(tmp_path)
    with pytest.raises(ValueError, match="public room"):
        store.await_human(decision, "Synthetic private note", to="bob", room="general")
    assert not store.outbox()
    assert store.pending()[0]["id"] == decision


def test_a_known_public_question_keeps_its_room_and_original_peer(tmp_path):
    """Public provenance authorizes only an actual broadcast to its source room."""
    store, decision = _store(tmp_path, broadcast=True)
    store.await_human(decision, "Synthetic waiting note", to="bob", room="general")
    note = store.outbox()[0]
    assert not note["to"] and not note.get("to_id")
    assert note["room"] == "general" and note["decision_public"] is True
    store.answer(decision, "Synthetic final response", verbatim=True)
    assert not store.outbox()[-1]["to"]
    assert store.outbox()[-1]["room"] == "general"


@pytest.mark.asyncio
async def test_a_marked_legacy_decision_delivery_without_an_id_is_kept_unsent(tmp_path):
    """A malformed sensitive record cannot fall through to ordinary name routing."""
    state, _ = _store(tmp_path)
    with state._db() as db:
        delivery = state._add(db, "outbox", {"text": "Synthetic private answer", "to": "bob",
                                            "room": "", "decision_delivery": True, "routing_version": 1})

    class NeverHTTP:
        def stream(self, *_, **__):
            raise AssertionError("an unbound decision must not reach HTTP")

    service = Conversation(SimpleNamespace(
        profile=SimpleNamespace(dir=tmp_path, url="http://unused", token="synthetic"), _http=NeverHTTP()))
    await service._flush(state, state.snapshot()["generation"])
    assert state.outbox()[0]["id"] == delivery
    assert state.outbox()[0]["identity_quarantined"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["await", "answer"])
async def test_a_queued_human_reply_survives_a_rename_without_reaching_a_replacement(
        session, tmp_path, operation):
    """Use the actual worker flush and hub API; no model or approval is invoked."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("carol", "synthetic-replacement")
    state, decision = _store(tmp_path / "worker", sender_id=original.id)
    if operation == "await":
        state.await_human(decision, "Synthetic identity-bound note", to="bob")
    else:
        state.answer(decision, "Synthetic identity-bound note", verbatim=True, notify_to="bob")
    delivery = state.outbox()[0]["id"]
    hub.rename(original.id, "bobby")
    hub.rename(replacement.id, "bob")
    profile = SimpleNamespace(dir=tmp_path / "worker", url="http://testserver",
                              token=session["host_token"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
        service = Conversation(SimpleNamespace(profile=profile, _http=http))
        await service._flush(state, state.snapshot()["generation"])
    assert not state.outbox()
    own = [event for event in hub.history(viewer=original.id) if event.text == "Synthetic identity-bound note"]
    other = [event for event in hub.history(viewer=replacement.id) if event.text == "Synthetic identity-bound note"]
    assert len(own) == 1 and not other
    assert own[0].body["collab_worker_delivery"] == delivery


@pytest.mark.asyncio
async def test_the_service_records_the_exact_inbox_identity_before_a_later_decision(
        tmp_path, monkeypatch):
    """A model can propose a question, but cannot supply its authorizing peer ID."""
    inbox = Inbox(tmp_path)
    try:
        profile = SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_me", url="http://unused", token="synthetic")
        service = Conversation(SimpleNamespace(profile=profile, inbox=inbox, _http=None,
                                               snapshot={"participants": [{"id": "p_replacement", "name": "bob"}]}))
        state = worker.Store(tmp_path)
        state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
        inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_original",
                              to="alice", to_id="p_me", text="Synthetic source question"))

        async def fake_provider(*_, **__):
            return {"summary": "", "replies": [], "escalations": [
                {"seq": 1, "reason": "decision", "question": "Synthetic question"}]}

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        await service.turn()
        decision = state.pending()[0]
        assert decision["decision_source"]["sender_id"] == "p_original"
        state.answer(decision["id"], "Synthetic human response", verbatim=True, notify_to="bob")
        assert state.outbox()[0]["to_id"] == "p_original"
    finally:
        inbox.close()


def _durable(state):
    """The human's answer is kept: still an answer, or quarantined intact (never delivered)."""
    with state._db() as db:
        return db.execute("SELECT COUNT(*) FROM records WHERE kind IN ('answer','quarantine')").fetchone()[0] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [True, False])
async def test_an_instructed_human_answer_uses_its_original_peer_or_stays_durable(
        session, tmp_path, monkeypatch, source):
    """Model wording cannot grant the current name holder private human context."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("carol", "synthetic-replacement")
    state, decision = _store(tmp_path, source=source, sender_id=original.id)
    state.answer(decision, "Synthetic private instruction", verbatim=False)
    hub.rename(original.id, "bobby")
    hub.rename(replacement.id, "bob")
    inbox = Inbox(tmp_path)
    try:
        profile = SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_host",
                                  url="http://testserver", token=session["host_token"])

        async def fake_provider(*_, **__):
            return {"summary": "", "replies": [{"text": "Synthetic paraphrased answer", "to": "bob", "room": ""}],
                    "escalations": []}

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            service = Conversation(SimpleNamespace(profile=profile, inbox=inbox, _http=http,
                                                   snapshot={"participants": [{"name": "bob", "id": original.id if source else replacement.id}]}))
            await service.turn()
        observed = [event for event in hub.history(viewer=original.id) if event.text == "Synthetic paraphrased answer"]
        assert bool(observed) is source
        assert not any(event.text == "Synthetic paraphrased answer" for event in hub.history(viewer=replacement.id))
        assert _durable(state) is not source   # consumed when sourced; kept (answer or quarantine) when not
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_a_legacy_targeted_outbox_item_is_retained_without_network(tmp_path):
    """Old ordinary sends and decision replies are indistinguishable, so neither is guessed."""
    state, _ = _store(tmp_path)
    with state._db() as db:
        delivery = state._add(db, "outbox", {"text": "Synthetic legacy private text", "to": "bob", "room": ""})

    class NeverHTTP:
        def stream(self, *_, **__):
            raise AssertionError("legacy name-only routing must not reach HTTP")

    service = Conversation(SimpleNamespace(
        profile=SimpleNamespace(dir=tmp_path, url="http://unused", token="synthetic"), _http=NeverHTTP()))
    await service._flush(state, state.snapshot()["generation"])
    assert state.outbox()[0]["id"] == delivery
    assert "explicitly resend" in state.outbox()[0]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "conflicting", "unrelated"])
async def test_an_ambiguous_human_answer_never_consumes_or_sends_the_decision(
        tmp_path, monkeypatch, failure):
    """An original name with two identities is no basis for private model delivery."""
    state, first = _store(tmp_path, source=failure != "missing")
    state.answer(first, "Synthetic private instruction", verbatim=False)
    if failure == "conflicting":
        assert state.commit_turn(expected_cursor=1, cursor=2, summary="", replies=[],
                                 escalations=[{"seq": 2, "reason": "decision", "question": "Another question"}],
                                 decision_sources={2: {"sender": "bob", "sender_id": "p_other", "room": ""}})
        state.answer(state.pending()[0]["id"], "Another private instruction", verbatim=False)
    inbox = Inbox(tmp_path)
    calls = []
    try:
        async def fake_provider(*_, **__):
            calls.append(True)
            return {"summary": "", "replies": [{"text": "Synthetic private reply",
                    "to": "carol" if failure == "unrelated" else "bob", "room": ""}], "escalations": []}

        class NeverHTTP:
            def stream(self, *_, **__):
                raise AssertionError("ambiguous private routing must not reach HTTP")

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        service = Conversation(SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_me", url="http://unused", token="synthetic"),
            inbox=inbox, _http=NeverHTTP(), snapshot={"participants": [{"name": "bob"}, {"name": "carol"}]}))
        await service.turn()
        assert _durable(state)
        assert not state.outbox()
        assert bool(calls) is (failure == "unrelated")
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_an_old_hub_cannot_receive_an_id_bound_reply_through_its_name_endpoint(tmp_path):
    """A 404 retains the same item; no fallback may disclose it through old /messages."""
    state, decision = _store(tmp_path)
    state.answer(decision, "Synthetic private response", verbatim=True, notify_to="bob")
    delivery = state.outbox()[0]["id"]
    paths = []

    async def old_hub(request):
        paths.append(request.url.path)
        return httpx.Response(404 if request.url.path.endswith("/messages/by-id") else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(old_hub)) as http:
        service = Conversation(SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path, url="http://old-hub", token="synthetic"), _http=http))
        generation = state.snapshot()["generation"]
        await service._flush(state, generation)
        await service._flush(state, generation)
    assert paths == ["/ext/collab/v1/messages/by-id"]
    assert state.outbox()[0]["id"] == delivery
    assert state.outbox()[0]["error"] == "HTTPStatusError"
    assert state.outbox()[0]["retry_at"] > 0


@pytest.mark.asyncio
async def test_a_model_reply_keeps_the_prompt_time_identity_when_a_rename_lands_during_the_call(
        session, tmp_path, monkeypatch):
    """The mapping must be copied before await, not rebuilt from the replacement roster."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("carol", "synthetic-replacement")
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    inbox = Inbox(tmp_path)
    try:
        inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id=original.id,
                              to="alice", to_id="p_host", text="Synthetic question"))
        daemon = SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_host",
                                    url="http://testserver", token=session["host_token"]),
            inbox=inbox, snapshot={"participants": [{"name": "bob", "id": original.id}]}, _http=None)

        async def fake_provider(*_, **__):
            hub.rename(original.id, "bobby")
            hub.rename(replacement.id, "bob")
            daemon.snapshot = {"participants": [{"name": "bob", "id": replacement.id}]}
            return {"summary": "", "replies": [{"text": "Synthetic model-private marker", "to": "bob", "room": ""}],
                    "escalations": []}

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
            daemon._http = http
            await Conversation(daemon).turn()
        assert any(event.text == "Synthetic model-private marker" for event in hub.history(viewer=original.id))
        assert not any(event.text == "Synthetic model-private marker" for event in hub.history(viewer=replacement.id))
        assert not state.outbox()
    finally:
        inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["", "p_conflicting"])
async def test_a_model_reply_with_missing_or_conflicting_prompt_identity_is_not_committed(
        tmp_path, monkeypatch, source_id):
    """Names alone and a contradictory historical author cannot authorize delivery."""
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    inbox = Inbox(tmp_path)
    try:
        inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id=source_id, text="Synthetic question"))

        async def fake_provider(*_, **__):
            return {"summary": "", "replies": [{"text": "Synthetic private marker", "to": "bob", "room": ""}],
                    "escalations": []}

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_me"),
                                 inbox=inbox, _http=None,
                                 snapshot={"participants": [{"name": "bob", "id": "p_current"}]})
        await Conversation(daemon).turn()
        assert not state.outbox()
        assert state.snapshot()["cursor"] == 0
    finally:
        inbox.close()


@pytest.mark.parametrize("status", [200, 404])
def test_worker_off_keeps_id_bound_messages_on_the_dedicated_endpoint(tmp_path, monkeypatch, status):
    """The actual CLI and HubClient cannot fall back to old name-only transport."""
    from collab import cli
    from collab.client.hub_client import HubClient

    state, decision = _store(tmp_path)
    state.answer(decision, "Synthetic private response", verbatim=True, notify_to="bob")
    delivery = state.outbox()[0]["id"]
    paths = []

    def hub(request):
        import json
        paths.append(request.url.path)
        sent = json.loads(request.content)
        assert sent["to_id"] == "p_original"
        assert sent["body"]["collab_worker_delivery"] == delivery
        return httpx.Response(status, json={"seq": 1})

    api = HubClient.__new__(HubClient)
    api.base_url = "http://owned-synthetic"
    api.token = "synthetic"
    api._verified_url = api.base_url
    api._client = httpx.Client(transport=httpx.MockTransport(hub))
    monkeypatch.setattr(cli, "_require_own_profile", lambda _: SimpleNamespace(dir=tmp_path, name="alice"))
    monkeypatch.setattr(cli, "_client", lambda _: api)
    assert cli.cmd_worker(SimpleNamespace(worker_action="off")) == 0
    assert paths == ["/ext/collab/v1/messages/by-id"]
    assert bool(state.outbox()) is (status == 404)


@pytest.mark.parametrize("kind", ["chat", "file"])
def test_worker_off_quarantines_legacy_targets_before_message_or_file_io(tmp_path, monkeypatch, kind):
    """A legacy filename need not even exist: target refusal precedes every read."""
    from collab import cli

    state, _ = _store(tmp_path)
    with state._db() as db:
        delivery = state._add(db, "outbox", {"kind": kind, "text": "Synthetic legacy text",
            "to": "bob", "room": "", "stash": str(tmp_path / "never-read"), "name": "never-read"})

    class NeverHub:
        def resolve_recipient(self, name):
            assert name == "bob"
            return "p_observed_current"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send(self, *_):
            raise AssertionError("legacy target must be held before message IO")

        def upload_file(self, *_args, **_kw):
            raise AssertionError("legacy target must be held before file IO")

    monkeypatch.setattr(cli, "_require_own_profile", lambda _: SimpleNamespace(dir=tmp_path, name="alice"))
    monkeypatch.setattr(cli, "_client", lambda _: NeverHub())
    assert cli.cmd_worker(SimpleNamespace(worker_action="off")) == 0
    assert state.outbox()[0]["id"] == delivery
    assert state.outbox()[0]["identity_quarantined"] is True
    decisions = [row for row in state.pending() if row.get("quarantined_delivery") == delivery]
    assert len(decisions) == 1
    assert decisions[0]["observed_current_id"] == "p_observed_current"
    assert "not historical proof" in decisions[0]["question"]


@pytest.mark.parametrize("kind", ["chat", "file"])
def test_a_new_targeted_queue_item_requires_its_principal_before_any_file_read(tmp_path, kind):
    """A newly queued name cannot be deferred for later resolution after a rename."""
    state, _ = _store(tmp_path)
    with pytest.raises(ValueError, match="original participant ID"):
        if kind == "chat":
            state.send("Synthetic targeted text", to="bob")
        else:
            state.queue_file(tmp_path / "never-read", to="bob")
    assert not state.outbox()


@pytest.mark.asyncio
async def test_a_queued_private_file_keeps_its_original_recipient_after_a_rename(session, tmp_path):
    """The actual async uploader uses guarded stable-ID multipart routing."""
    hub = session["store"]
    original = hub.add_participant("bob", "synthetic-original")
    replacement = hub.add_participant("carol", "synthetic-replacement")
    state, _ = _store(tmp_path / "worker")
    source = tmp_path / "synthetic.txt"
    source.write_text("Synthetic private queued bytes")
    delivery = state.queue_file(source, to="bob", to_id=original.id)
    hub.rename(original.id, "bobby")
    hub.rename(replacement.id, "bob")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"])) as http:
        service = Conversation(SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path / "worker", url="http://testserver", token=session["host_token"]), _http=http))
        await service._flush(state, state.snapshot()["generation"])
        assert not state.outbox()
        from collab.compatibility import request_headers
        own = await http.get("http://testserver/ext/collab/v1/files", headers=request_headers("synthetic-original"))
        other = await http.get("http://testserver/ext/collab/v1/files", headers=request_headers("synthetic-replacement"))
        assert own.status_code == other.status_code == 200
        assert "synthetic.txt" in own.text and "synthetic.txt" not in other.text
    assert len(own.json()["files"]) == 1



@pytest.mark.asyncio
async def test_an_old_hub_refusal_keeps_the_id_bound_file_and_stash_without_fallback(tmp_path):
    """A missing guarded upload endpoint is not permission for ordinary /files."""
    state, _ = _store(tmp_path / "worker")
    source = tmp_path / "synthetic.txt"
    source.write_text("Synthetic private bytes")
    delivery = state.queue_file(source, to="bob", to_id="p_original")
    stash = state.outbox()[0]["stash"]
    paths = []

    async def old_hub(request):
        paths.append(request.url.path)
        assert request.url.params["to_id"] == "p_original"
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(old_hub)) as http:
        service = Conversation(SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path / "worker", url="http://old-hub", token="synthetic"), _http=http))
        await service._flush(state, state.snapshot()["generation"])
    assert paths == ["/ext/collab/v1/files/by-id"]
    assert state.outbox()[0]["id"] == delivery
    from pathlib import Path
    assert Path(stash).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_an_internal_unanchored_answer_is_context_and_does_not_stall_normal_replies(
        tmp_path, monkeypatch, reply):
    """A budget/quarantine instruction is local coordination, never guessed peer authority."""
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    assert state.commit_turn(expected_cursor=0, cursor=0, summary="", replies=[],
                             escalations=[{"seq": 0, "reason": "scope", "question": "Synthetic internal question"}])
    state.answer(state.pending()[0]["id"], "Synthetic internal instruction", verbatim=False)
    inbox = Inbox(tmp_path)
    try:
        async def fake_provider(*_, **__):
            return {"summary": "Local coordination updated", "escalations": [], "replies": [
                {"text": "Synthetic private reply", "to": "bob", "room": ""}] if reply else []}

        monkeypatch.setattr("collab.worker_runtime.run_turn", fake_provider)
        service = Conversation(SimpleNamespace(
            profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_me"),
            inbox=inbox, _http=None, snapshot={"participants": [{"name": "bob", "id": "p_bob"}]}))
        await service.turn()
        assert not state.snapshot()["answers"]
        assert bool(state.outbox()) is reply
        if reply:
            assert state.outbox()[0]["to_id"] == "p_bob", "normal prompt-time identity routes the reply"
        if not reply:
            assert state.snapshot()["summary"] == "Local coordination updated"
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_answering_an_internal_decision_does_not_stall_later_conversation_turns(tmp_path, monkeypatch):
    """A sequence-zero instruction must be consumed once while later chats progress."""
    state=worker.Store(tmp_path)
    state.configure({"agent":"command","command":["never-executed"],"scope":"Synthetic only"})
    state.commit_turn(expected_cursor=0,cursor=0,summary="",replies=[],
                      escalations=[{"seq":0,"reason":"scope","question":"Synthetic internal guidance"}])
    state.answer(state.pending()[0]["id"],"Continue ordinary peer coordination",verbatim=False)
    inbox=Inbox(tmp_path)
    sent=[]
    def accept(request):
        import json
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={})
    async def model(*args,**kwargs):
        return {"summary":"Continued","escalations":[],"replies":[{"to":"bob","room":"","text":"Synthetic reply"}]}
    from collab.client import worker_service as module
    original_setting=module.setting
    monkeypatch.setattr(module,"setting",lambda key:0 if key=="worker_turn_gap" else original_setting(key))
    monkeypatch.setattr("collab.worker_runtime.run_turn",model)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(accept)) as http:
            service=Conversation(SimpleNamespace(profile=SimpleNamespace(dir=tmp_path,name="alice",participant_id="p_me",token="synthetic",url="http://owned"),inbox=inbox,_http=http,snapshot={"participants":[{"name":"bob","id":"p_bob"}]}))
            for seq in range(1,4):
                inbox.record(Envelope(kind="chat",sender="bob",sender_id="p_bob",to="alice",to_id="p_me",seq=seq,text="Synthetic incoming"))
                await service.turn()
                assert state.snapshot()["cursor"]==seq
                assert not state.snapshot()["answers"]
            assert len(sent)==3
            assert all(row["to_id"]=="p_bob" for row in sent)
    finally:inbox.close()
