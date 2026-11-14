"""An active worker must make progress without a main-agent recv loop."""
import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from collab import worker, worker_notices
from collab.client.inbox import Inbox
from collab.client.worker_service import Conversation
from collab.protocol import Envelope



@pytest.fixture(autouse=True)
def workers_allowed(monkeypatch):
    """These drive the worker service with fake providers; conftest's
    COLLAB_NO_WORKER (no real spend in tests) would keep it idle."""
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)

@pytest.fixture
def conversation(tmp_path):
    inbox = Inbox(tmp_path)
    daemon = SimpleNamespace(
        profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                url="http://hub", token="test"),
        inbox=inbox, snapshot={"participants": [{"name": "alice"}, {"name": "bob"}]},
        _http=None)
    service = Conversation(daemon)
    yield service
    inbox.close()


def configure(service, tmp_path):
    script = tmp_path / "provider.py"
    script.write_text('''import json,sys
p=json.load(sys.stdin)
r={"summary":"Bob is collaborating with Alice.","replies":[],"escalations":[]}
if p["main_answers"]:
    r["replies"]=[{"text":p["main_answers"][0]["text"],"to":"bob","room":""}]
else:
    for e in p["events"]:
        if e["text"]=="Need a decision":
            r["escalations"].append({"seq":e["seq"],"reason":"decision","question":"May Bob change the API?"})
        elif e["text"]=="Question":
            r["replies"].append({"text":"The agreed API stays stable.","to":"bob","room":""})
json.dump(r,sys.stdout)
''')
    worker.Store(service.root).configure({"agent": "command", "scope": "Coordinate the agreed API.",
                                         "command": [sys.executable, str(script)]})


def incoming(service, seq, text):
    service.daemon.inbox.record(Envelope(seq=seq, kind="chat", sender="bob", sender_id="p_b", text=text))


@pytest.mark.asyncio
async def test_later_peer_requests_receive_retained_facts_without_idle_model_turns(conversation, tmp_path, monkeypatch):
    """Summary omission cannot erase the main agent's token on the next request."""
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    store.context("Token: panel-v202-ready")
    calls = []
    async def run(*args, **kwargs):
        calls.append(args[2])
        return {"summary": "Nothing about tokens", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    await service.turn()
    await service.turn()
    assert len(calls) == 1, "retained facts alone must not spend model calls"
    incoming(service, 1, "What was the token?")
    await service.turn()
    assert calls[-1]["retained_main_context"][0]["text"] == "Token: panel-v202-ready"


@pytest.mark.asyncio
async def test_peer_conversation_and_decision_return_flow_without_main_reading(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"seq": 99})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.daemon._http = client
        incoming(service, 1, "Question")
        incoming(service, 2, "Need a decision")
        await service.turn()  # real subprocess, durable inbox, HTTP transport
        state = worker.Store(service.root)
        assert sent[0]["text"] == "The agreed API stays stable."
        assert state.snapshot()["cursor"] == 2
        assert service.daemon.inbox.unread_count() == 2
        notice = worker_notices.claim(service.root)
        assert notice and "May Bob" in notice["text"]
        worker_notices.commit(service.root, notice["token"])
        incoming(service, 3, "Question")
        await service.turn()  # a pending decision must not stop other work
        assert len(sent) == 2
        assert len(state.pending()) == 1
        decision = state.pending()[0]
        state.answer(decision["id"], "Keep the API stable; change only the implementation.")
        await Conversation(service.daemon).turn()  # restart keeps answer/summary/cursor
        assert sent[-1]["to"] == "bob"
        assert "change only" in sent[-1]["text"]
        assert not state.pending() and not state.snapshot()["answers"]
        assert worker_notices.claim(service.root) is None
        assert service.daemon.inbox.unread_count() == 3
        await service.turn()
        assert len(sent) == 3  # no periodic acknowledgement loop


@pytest.mark.asyncio
async def test_failed_publish_retries_the_same_outbox_id(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "Question")
    attempts = []
    async def transport(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(503 if len(attempts) == 1 else 200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.daemon._http = client
        await service.turn()
        store = worker.Store(service.root)
        assert store.status()["error"] and store.status()["outbox_count"] == 1
        assert store.snapshot()["cursor"] == 1
        store.defer(store.outbox()[0]["id"], retry_at=0, error="retry now")
        await Conversation(service.daemon).turn()
        assert attempts[0] == attempts[1]
        assert not store.outbox()


@pytest.mark.asyncio
async def test_off_cancels_inflight_provider_and_stale_actions_never_commit(conversation, tmp_path, monkeypatch):
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "Question")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def provider(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    async with httpx.AsyncClient() as client:
        service.daemon._http = client
        await service.tick()
        await asyncio.wait_for(started.wait(), 1)
        worker.Store(service.root).off()
        await service.tick()
        assert cancelled.is_set() and service.task is None
        assert worker.Store(service.root).snapshot()["cursor"] == 0


@pytest.mark.asyncio
async def test_bad_provider_is_visible_and_does_not_advance_cursor(conversation, tmp_path, monkeypatch):
    from collab.worker_runtime import WorkerRuntimeError
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "Question")
    async def provider(*args, **kwargs):
        raise WorkerRuntimeError("provider failed")
    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    state = worker.Store(service.root).status()
    assert state["error"] and state["cursor"] == 0 and state["retry_at"] > 0
    assert worker_notices.claim(service.root)


@pytest.mark.asyncio
async def test_failed_delivery_does_not_hold_new_questions_hostage(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "Question")
    async def broken(request):
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        service.daemon._http = client
        await service.turn()
        incoming(service, 2, "Need a decision")
        await service.turn()
        state = worker.Store(service.root).status()
        assert state["cursor"] == 2 and len(state["pending"]) == 1
        assert state["outbox_count"] == 1 and state["error"]


@pytest.mark.asyncio
async def test_main_answer_recovers_exact_peer_address_after_summary_loss(conversation, tmp_path, monkeypatch):
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "Need a decision")
    await service.turn()
    store = worker.Store(service.root)
    decision = store.pending()[0]
    store.answer(decision["id"], "Keep the API stable.")
    captured = []
    async def provider(agent, model, payload, *args, **kwargs):
        captured.append(payload)
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await Conversation(service.daemon).turn()
    source = captured[0]["main_answers"][0]["source"]
    assert source["sender"] == "bob" and source["seq"] == 1
    assert source["text"] == "Need a decision"


@pytest.mark.asyncio
async def test_large_unicode_event_is_bounded_without_stalling_the_cursor(conversation, tmp_path, monkeypatch):
    service = conversation
    configure(service, tmp_path)
    incoming(service, 1, "字" * 8000)
    captured = []
    async def provider(agent, model, payload, *args, **kwargs):
        captured.append(payload)
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    assert captured[0]["events"][0]["text"] == "字" * 8000
    assert worker.Store(service.root).snapshot()["cursor"] == 1


@pytest.mark.asyncio
async def test_persisted_hourly_limit_does_not_consume_new_peer_events(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    generation = store.snapshot()["generation"]
    for _ in range(60):
        assert store.reserve_turn(expected_generation=generation) == 0
    incoming(service, 1, "Question")
    await service.turn()
    assert store.status()["cursor"] == 0
    assert "limit" in store.status()["error"]
    assert service.next_at > 0


@pytest.mark.asyncio
async def test_daemon_wakes_for_worker_decisions_even_with_old_inbox_notice(conversation, tmp_path, monkeypatch):
    from collab import attention, wake
    from collab.client.daemon import Daemon
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    store.commit_turn(expected_cursor=0, cursor=1, summary="", replies=[],
                      escalations=[{"reason": "decision", "question": "Private question", "seq": 1}])
    attention.delivered(service.root, 1, seqs=[1])
    wake.write_config(service.root, wake.WakeConfig(command=["true"]))
    daemon = Daemon.__new__(Daemon)
    daemon.profile = service.daemon.profile
    daemon.waker = wake.Waker(service.root, "s_worker", attended=lambda: True)
    daemon._waking = None
    calls = []
    async def deliver(batch, reminder=""):
        calls.append((batch, reminder))
        daemon.waker.succeeded(None)
    daemon._wake = deliver
    await daemon._maybe_wake()
    assert daemon._waking is not None
    await daemon._waking
    assert calls[0][0] is None
    assert "collab worker pending" in calls[0][1]
    assert "Private question" in calls[0][1]
    assert worker_notices.claim(service.root) is None


@pytest.mark.asyncio
async def test_worker_failed_wake_releases_notice_for_monitor_recovery(conversation, tmp_path):
    from collab import wake
    from collab.client.daemon import Daemon
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    store.health(error="provider unavailable")
    wake.write_config(service.root, wake.WakeConfig(command=["false"]))
    daemon = Daemon.__new__(Daemon)
    daemon.profile = service.daemon.profile
    daemon.waker = wake.Waker(service.root, "s_worker", attended=lambda: False)
    daemon._waking = None
    async def deliver(batch, reminder=""):
        daemon.waker.failed(None)
    daemon._wake = deliver
    await daemon._maybe_wake()
    await daemon._waking
    assert worker_notices.claim(service.root) is not None


@pytest.mark.asyncio
async def test_json_escape_expansion_is_budgeted_and_every_message_remains_whole(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    for seq in range(1, 4):
        incoming(service, seq, "\x01" * 7990)
    await service.turn()  # actual custom subprocess + runtime byte ceiling
    store = worker.Store(service.root)
    assert store.snapshot()["cursor"] == 1 and not store.status()["error"]
    await service.turn()
    await service.turn()
    assert store.snapshot()["cursor"] == 3 and not store.status()["error"]


@pytest.mark.asyncio
async def test_pending_decision_excerpt_cannot_hide_later_decisions(conversation, tmp_path):
    from collab.client.worker_service import _page
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    store.commit_turn(expected_cursor=0, cursor=1, summary="", replies=[], escalations=[
        {"reason": "decision", "question": "\x01" * 2000, "seq": 1},
        {"reason": "blocker", "question": "Also blocked", "seq": 1}])
    projected = _page([service._source(row) for row in store.pending()], budget=12000)
    assert len(projected) == 2
    assert projected[0]["question_truncated"]
    assert store.pending()[0]["question"] == "\x01" * 2000


@pytest.mark.parametrize("summary", ["😀" * 4000, "\x01" * 4000])
def test_schema_valid_summary_commits_without_a_smaller_storage_byte_limit(conversation, tmp_path, summary):
    from collab.worker_runtime import validate_result
    configure(conversation, tmp_path)
    result = validate_result({"summary": summary, "replies": [], "escalations": []})
    store = worker.Store(conversation.root)
    assert store.commit_turn(expected_cursor=0, cursor=0, **result)
    assert store.snapshot()["summary"] == summary


@pytest.mark.asyncio
async def test_an_oversized_first_event_becomes_visible_and_does_not_stall_peers(conversation, tmp_path):
    service = conversation
    configure(service, tmp_path)
    service.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b",
                                        text="Question", room="r" * 70000))
    incoming(service, 2, "Need a decision")
    await service.turn()
    store = worker.Store(service.root)
    assert store.snapshot()["cursor"] == 1
    assert store.pending()[0]["seq"] == 1
    assert "exceeds" in store.pending()[0]["question"]
    assert worker_notices.claim(service.root)
    await service.turn()
    assert store.snapshot()["cursor"] == 2
    assert len(store.pending()) == 2
    first = next(item for item in store.pending() if item["seq"] == 1)
    assert service._source(first)["source"]["routing_unavailable"]
    assert len(json.dumps(service._source(first))) < 12000


def test_worker_wake_prompt_is_a_decision_notice_not_a_standing_reminder(tmp_path):
    from collab.wake import Waker
    waker = Waker(tmp_path, "s_worker", attended=lambda: False)
    text = "Collab worker: 1 decision(s) need your input. Run collab worker pending."
    assert waker.turn_prompt(None, text) == text
    path = waker.write_prompt(None, text)
    assert path.name == "worker-notice.txt" and path.read_text() == text


@pytest.mark.parametrize('agent', ['codex', 'claude'])
async def test_idle_health_keeps_a_missing_default_model_visible_until_corrected(conversation, agent, monkeypatch):
    """The first idle tick must not hide a configuration error from onboarding."""
    monkeypatch.delenv('COLLAB_NO_WORKER', raising=False)  # tests automatic setup
    from collab import runtime_settings
    runtime_settings.set_value('worker_agent', agent)
    runtime_settings.set_value('worker_' + agent + '_model', '')
    store = worker.Store(conversation.root)
    store.ensure_default()
    await conversation.turn()
    assert 'requires a model' in store.status()['error']
    assert store.status().get('attempts', 0) == 0
    runtime_settings.set_value('worker_' + agent + '_model', worker.DEFAULT_MODELS[agent])
    await conversation.turn()
    assert store.status()['error'] == ''


@pytest.mark.parametrize('participant_name', ['alice', 'a' * 64, 'á' * 40])
async def test_a_decision_is_routed_internally_and_answered_without_the_peer_resending(conversation, tmp_path, participant_name):
    """A custom adapter gets the shared identity and keeps the original return route."""
    from collab import worker_runtime
    conversation.daemon.profile.name = participant_name
    script = tmp_path / 'decision-provider.py'
    script.write_text("import json,sys\n" +
        "p=json.load(sys.stdin)\n" +
        f"assert p['self']['name']=={participant_name!r}\n" +
        f"assert {worker_runtime.PEER_IDENTITY!r} in p['instructions']\n" +
        "r={'summary':'Decision awaiting an answer','replies':[],'escalations':[]}\n" +
        "if p['main_answers']:\n" +
        " a=p['main_answers'][0]; source=a['source']\n" +
        " assert source['seq']==1 and source['sender']=='bob' and source['room']=='api'\n" +
        " r['replies']=[{'to':source['sender'],'room':source['room'],'text':a['text']}]\n" +
        "elif p['events']:\n" +
        " e=p['events'][0]\n" +
        " r['escalations']=[{'seq':e['seq'],'reason':'decision','question':'May the public API change?'}]\n" +
        " r['replies']=[{'to':e['sender'],'room':e['room'],'text':'I will check and get back to you.'}]\n" +
        "json.dump(r,sys.stdout)\n")
    store = worker.Store(conversation.root)
    store.configure({'agent':'command', 'scope':'Coordinate API ownership',
                     'command':[sys.executable, str(script)]})
    conversation.daemon.inbox.record(Envelope(seq=1, kind='chat', sender='bob',
        sender_id='p_b', room='api', text='May I change the public API?'))
    sent = []
    async def transport(request):
        sent.append((request.headers.get('authorization'), json.loads(request.content)))
        return httpx.Response(200, json={'seq':99})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
        assert store.snapshot()['cursor'] == 1
        assert len(store.pending()) == 1
        assert worker_notices.claim(conversation.root)
        assert sent[0][1]['text'] == 'I will check and get back to you.'
        store.answer(store.pending()[0]['id'], 'Keep the public API unchanged.')
        # No new peer message: the persisted decision carries the return route
        # through a listener restart, even after its initial turn was consumed.
        await Conversation(conversation.daemon).turn()
        assert len(sent) == 2
        assert sent[1][1]['text'] == 'Keep the public API unchanged.'
        assert all(body['to']=='bob' and body['room']=='api' for _, body in sent)
        assert sent[0][0] == sent[1][0] == 'Bearer test'
        assert not store.pending() and not store.snapshot()['answers']


async def test_an_empty_poll_does_not_delay_the_next_peer_question(conversation, tmp_path, monkeypatch):
    """Idle polling formerly imposed five seconds before any model was called."""
    import time
    service = conversation
    configure(service, tmp_path)
    calls = []
    async def run(*args, **kwargs):
        calls.append(time.monotonic())
        return {'summary': '', 'replies': [], 'escalations': []}
    monkeypatch.setattr('collab.worker_runtime.run_turn', run)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        service.daemon._http = client
        await service.tick()
        await service.task
        assert service.next_at == 0
        arrived = time.monotonic()
        incoming(service, 1, 'Question')
        await service.tick()
        await service.task
        assert len(calls) == 1 and calls[0] - arrived < .5
        incoming(service, 2, 'Another question')
        await service.tick()
        assert len(calls) == 1, 'real model calls still obey their configured gap'


@pytest.mark.asyncio
async def test_main_send_is_delivered_while_a_model_turn_is_in_flight(conversation, tmp_path, monkeypatch):
    """R3: a slow provider turn must not hold the main agent's queued message."""
    service = conversation
    configure(service, tmp_path)
    store = worker.Store(service.root)
    release = asyncio.Event()
    async def slow(*args, **kwargs):
        await release.wait()
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", slow)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"seq": 9})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.daemon._http = client
        incoming(service, 1, "Question")
        await service.tick()  # starts the slow turn
        await asyncio.sleep(0)
        assert service.task is not None and not service.task.done()
        store.send("main handoff", to="", room="general", thread="t1")
        await service.tick()
        await asyncio.wait_for(service.delivery, 2)
        assert [(m["text"], m["room"], m["thread"]) for m in sent] == [("main handoff", "general", "t1")]
        assert not store.outbox()
        release.set()
        await asyncio.wait_for(service.task, 2)
        assert len(sent) == 1, "the finished turn must not post the record again"


@pytest.mark.asyncio
async def test_a_daemon_with_collab_no_worker_runs_no_worker_at_all(conversation, monkeypatch):
    """The env reaches the daemon through spawn; even an enabled worker.db stays idle."""
    monkeypatch.delenv('COLLAB_NO_WORKER', raising=False)
    store = worker.Store(conversation.root)
    store.configure({'agent': 'claude', 'scope': 'Coordinate'})
    monkeypatch.setenv('COLLAB_NO_WORKER', '1')
    calls = []
    monkeypatch.setattr("collab.worker_runtime.run_turn", lambda *a, **k: calls.append(a))
    incoming(conversation, 1, "Question")
    conversation.daemon._http = object()
    await conversation.tick()
    assert conversation.task is None and calls == []


@pytest.mark.asyncio
async def test_a_send_the_hub_refuses_for_good_is_dropped_and_escalated(conversation, tmp_path):
    """A rejected address used to retry forever and fill MAX_PENDING."""
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("for nobody", to="ghost")
    async def transport(request):
        return httpx.Response(404, json={"detail": "no such participant"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation._flush(store, store.snapshot()["generation"])
    assert not store.outbox()
    assert "@ghost could not be delivered (hub answered 404)" in store.pending()[0]["question"]


@pytest.mark.asyncio
async def test_two_concurrent_flushes_post_each_record_once(conversation, tmp_path):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("once", to="bob")
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content))
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"seq": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        generation = store.snapshot()["generation"]
        await asyncio.gather(conversation._flush(store, generation), conversation._flush(store, generation))
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_board_comments_ride_the_outbox_in_order_with_chat(conversation, tmp_path):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("first", to="bob")
    store.queue_comment("task", "T_1", "evidence: tests pass")
    seen = []
    async def transport(request):
        seen.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation._flush(store, store.snapshot()["generation"])
    assert [route for route, _ in seen] == ["messages", "comments"]
    comment = seen[1][1]
    assert comment["subject"] == "task" and comment["id"] == "T_1" and comment["collab_worker_delivery"]
    assert not store.outbox()



@pytest.mark.asyncio
async def test_a_queued_file_is_uploaded_and_its_copy_removed(conversation, tmp_path):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    source = tmp_path / "r.txt"
    source.write_text("data")
    store.queue_file(source, to="bob")
    stash = store.outbox()[0]["stash"]
    seen = []
    async def transport(request):
        seen.append((request.url.path.rsplit("/", 1)[-1], request.url.params.get("to")))
        return httpx.Response(200, json={"id": "f_1"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation._flush(store, store.snapshot()["generation"])
    assert seen == [("files", "bob")] and not store.outbox()
    from pathlib import Path
    assert not Path(stash).exists()


@pytest.mark.asyncio
async def test_under_collab_no_worker_queued_messages_are_still_delivered(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    store.send("ship it", to="bob")
    monkeypatch.setenv("COLLAB_NO_WORKER", "1")
    calls = []
    monkeypatch.setattr("collab.worker_runtime.run_turn", lambda *a, **k: calls.append(a))
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"seq": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        incoming(conversation, 1, "Question")
        await conversation.tick()
        await asyncio.wait_for(conversation.delivery, 2)
    assert sent == ["ship it"] and calls == [], "delivered, but no model call"


@pytest.mark.asyncio
async def test_an_escalation_with_a_wrong_seq_is_kept_not_thrown_away(conversation, tmp_path, monkeypatch):
    """The whole paid turn used to be discarded and backed off for one bad reference."""
    configure(conversation, tmp_path)
    async def run(*args, **kwargs):
        return {"summary": "", "replies": [], "escalations": [
            {"reason": "decision", "seq": 999, "question": "Which branch?"}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    incoming(conversation, 1, "Question")
    await conversation.turn()
    store = worker.Store(conversation.root)
    assert [(p["seq"], p["question"]) for p in store.pending()] == [(1, "Which branch?")], "the only message in the turn"
    assert not store.status()["error"]


@pytest.mark.asyncio
async def test_a_worker_reply_repeating_an_open_question_to_someone_else_is_dropped(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    incoming(conversation, 1, "May we drop the legacy API in 3.0?")
    snap = store.snapshot()
    store.commit_turn(expected_cursor=snap["cursor"], cursor=1, summary="", replies=[],
                      escalations=[{"reason": "decision", "seq": 1,
                                    "question": "Bob asks: may we drop the legacy API in 3.0?"}])
    # a message in another room, so "ops" is a room this turn may answer in
    conversation.daemon.inbox.record(Envelope(seq=2, kind="chat", sender="bob", sender_id="p_b",
                                              room="ops", text="anything new?"))
    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": [
            {"text": "FYI ops: Bob asks: may we drop the legacy API in 3.0? waiting", "to": "", "room": "ops"},
            {"text": "In general: Bob asks: may we drop the legacy API in 3.0? Still open.", "to": "", "room": "general"},
            {"text": "Still with the main agent.", "to": "bob", "room": ""}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"seq": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
    assert sent == ["In general: Bob asks: may we drop the legacy API in 3.0? Still open.",
                    "Still with the main agent."], "dropped in another room, kept in the asker's own"
    assert store.status()["dropped_replies"] == 1


@pytest.mark.asyncio
async def test_the_worker_sees_the_open_board(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    conversation.daemon.snapshot["tasks"] = [{"id": "T_22", "title": "R22 consent primitive", "owner": "arma",
                                              "state": "working"}]
    payloads = []
    async def run(*args, **kwargs):
        payloads.append(args[2])
        return {"summary": "", "replies": [], "escalations": []}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    incoming(conversation, 1, "does R22 exist?")
    await conversation.turn()
    assert payloads[0]["board"] == [{"id": "T_22", "title": "R22 consent primitive", "owner": "arma",
                                     "state": "working"}]


@pytest.mark.asyncio
async def test_the_leak_guard_compares_participant_ids_across_a_rename(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b",
                                              text="May we drop the legacy API in 3.0?"))
    snap = store.snapshot()
    store.commit_turn(expected_cursor=snap["cursor"], cursor=1, summary="", replies=[],
                      escalations=[{"reason": "decision", "seq": 1,
                                    "question": "Bob asks: may we drop the legacy API in 3.0?"}])
    # bob renamed to robert; a carol joined under the old name 'bob'
    conversation.daemon.snapshot["participants"] = [{"name": "robert", "id": "p_b"},
                                                    {"name": "bob", "id": "p_c"}, {"name": "alice", "id": "p_a"}]
    incoming(conversation, 2, "any news?")
    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": [
            {"text": "Bob asks: may we drop the legacy API in 3.0? still open", "to": "robert", "room": ""},
            {"text": "Bob asks: may we drop the legacy API in 3.0? still open", "to": "bob", "room": ""}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content)["to"])
        return httpx.Response(200, json={"seq": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
    assert sent == ["robert"], "the asker under its new name only; never the new holder of the old name"



@pytest.mark.asyncio
async def test_a_private_question_carrying_a_room_does_not_open_that_room(conversation, tmp_path, monkeypatch):
    configure(conversation, tmp_path)
    store = worker.Store(conversation.root)
    conversation.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="bob", sender_id="p_b",
                                              to="alice", room="general",
                                              text="May we drop the legacy API in 3.0?"))
    snap = store.snapshot()
    store.commit_turn(expected_cursor=snap["cursor"], cursor=1, summary="", replies=[],
                      escalations=[{"reason": "decision", "seq": 1,
                                    "question": "Bob asks: may we drop the legacy API in 3.0?"}])
    incoming(conversation, 2, "anything new?")
    async def run(*args, **kwargs):
        return {"summary": "", "escalations": [], "replies": [
            {"text": "All: Bob asks: may we drop the legacy API in 3.0? Still open.", "to": "", "room": "general"}]}
    monkeypatch.setattr("collab.worker_runtime.run_turn", run)
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"seq": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        conversation.daemon._http = client
        await conversation.turn()
    assert sent == [], "a DM that carried room=general stays private"
