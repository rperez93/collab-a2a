"""A bounded arrival window groups real work without spending on presence churn."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from collab import worker
from collab.client.inbox import Inbox
from collab.client import worker_service as service_module
from collab.client.worker_service import Conversation
from collab.protocol import Envelope


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    inbox = Inbox(tmp_path)
    daemon = SimpleNamespace(
        profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a", url="http://synthetic", token="synthetic"),
        inbox=inbox, snapshot={"participants": [{"name": "bob", "id": "p_b"}]}, _http=object())
    service = Conversation(daemon)
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    yield service, state
    inbox.close()


def incoming(service, seq, kind="chat"):
    service.daemon.inbox.record(Envelope(seq=seq, kind=kind, sender="bob", sender_id="p_b",
                                        to="alice", to_id="p_a", text=f"Synthetic {kind} {seq}"))


def provider(monkeypatch):
    calls = []

    async def fake(*args, **kwargs):
        calls.append(args[2])
        return {"summary": "Synthetic summary", "replies": [], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", fake)
    return calls


@pytest.mark.asyncio
async def test_a_burst_waits_once_and_keeps_every_conversation_event(conversation, monkeypatch):
    """Continuous arrivals cannot restart the fixed deadline or lose task context."""
    service, state = conversation
    calls = provider(monkeypatch)
    released = asyncio.Event()
    timeouts = []
    original = asyncio.wait_for

    async def controlled_wait(awaitable, timeout):
        if timeout == service_module.COALESCE_SECONDS:
            awaitable.close()
            timeouts.append(timeout)
            await released.wait()
            raise asyncio.TimeoutError
        return await original(awaitable, timeout)

    monkeypatch.setattr(service_module.asyncio, "wait_for", controlled_wait)
    incoming(service, 1)
    await service.tick()
    waiting = service.task
    await asyncio.sleep(0)
    for seq, kind in enumerate(["activity", "chat", "task", "presence", "project", "request", "response"], 2):
        incoming(service, seq, kind)
        await service.tick()
        assert service.task is waiting
    assert not calls and timeouts == [0.15]
    released.set()
    await waiting
    assert [[event["seq"] for event in call["events"]] for call in calls] == [[1, 3, 4, 6, 7, 8]]
    assert state.snapshot()["cursor"] == 8
    assert state.status()["attempts_total"] == 1


@pytest.mark.asyncio
async def test_presence_activity_and_idle_churn_advances_without_a_model_or_wait(conversation, monkeypatch):
    """The existing skip path must stay durable across restart and free of reservations."""
    service, state = conversation
    calls = provider(monkeypatch)

    async def forbidden(_):
        raise AssertionError("churn should not open a coalescing window")

    monkeypatch.setattr(service, "_coalesced_turn", forbidden)
    for seq, kind in enumerate(["presence", "activity", "idle", "stats"], 1):
        incoming(service, seq, kind)
    await service.tick()
    await service.task
    assert not calls and state.snapshot()["cursor"] == 4
    assert state.status().get("attempts_total", 0) == 0
    assert worker.Store(service.root).snapshot()["cursor"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("urgent", ["context", "answer", "outbox"])
async def test_urgent_local_work_bypasses_the_arrival_window(conversation, monkeypatch, urgent):
    """Exact sends and local answers must not wait behind ordinary burst collection."""
    service, state = conversation
    calls = provider(monkeypatch)
    incoming(service, 1)
    if urgent == "context":
        state.context("Synthetic urgent context")
    elif urgent == "answer":
        state.commit_turn(expected_cursor=0, cursor=0, summary="", replies=[],
                          escalations=[{"seq": 0, "reason": "scope", "question": "Synthetic internal question"}])
        state.answer(state.pending()[0]["id"], "Synthetic local answer", verbatim=False)
    else:
        state.send("Synthetic exact message", to="bob", to_id="p_b")

    async def forbidden(_):
        raise AssertionError("urgent local work was delayed")

    monkeypatch.setattr(service, "_coalesced_turn", forbidden)
    sent = []

    async def transport(request):
        sent.append(request.url.path)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service.daemon._http = http
        await service.tick()
        await service.task
    assert len(calls) == 1
    if urgent == "outbox":
        assert sent == ["/ext/collab/v1/messages/by-id"]
        assert not state.outbox()


@pytest.mark.asyncio
@pytest.mark.parametrize("urgent", ["context", "answer", "outbox"])
async def test_an_urgent_arrival_wakes_an_existing_window(conversation, monkeypatch, urgent):
    """A human answer arriving after the first tick also releases the one wait."""
    service, state = conversation
    calls = provider(monkeypatch)
    monkeypatch.setattr(service_module, "COALESCE_SECONDS", 60)
    incoming(service, 1)
    await service.tick()
    await asyncio.sleep(0)
    if urgent == "context":
        state.context("Synthetic urgent context")
    elif urgent == "answer":
        state.commit_turn(expected_cursor=0, cursor=0, summary="", replies=[],
                          escalations=[{"seq": 0, "reason": "scope", "question": "Synthetic internal question"}])
        state.answer(state.pending()[0]["id"], "Synthetic urgent answer", verbatim=False)
    else:
        state.send("Synthetic exact message", to="bob", to_id="p_b")

    async def transport(request):
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service.daemon._http = http
        await service.tick()
        await asyncio.wait_for(service.task, 1)
        if service.delivery is not None:
            await service.delivery
    assert len(calls) == 1
    assert not state.outbox()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["off", "reconfigure", "no_worker"])
async def test_a_waiting_turn_rechecks_its_configuration_ownership(conversation, monkeypatch, change):
    """Waiting must not launch a stale or explicitly disabled model configuration."""
    service, state = conversation
    calls = provider(monkeypatch)
    monkeypatch.setattr(service_module, "COALESCE_SECONDS", 60)
    incoming(service, 1)
    await service.tick()
    await asyncio.sleep(0)
    if change == "off":
        state.off()
    elif change == "reconfigure":
        state.configure({"agent": "command", "command": ["never-executed"], "scope": "New scope"})
    else:
        monkeypatch.setenv("COLLAB_NO_WORKER", "1")
    service._coalescing_wake.set()
    await service.task
    assert not calls and state.snapshot()["cursor"] == 0


@pytest.mark.asyncio
async def test_cancelling_a_window_never_reserves_or_advances_work(conversation, monkeypatch):
    """Shutdown owns the pending wait and leaves unread chat for the next listener."""
    service, state = conversation
    calls = provider(monkeypatch)
    monkeypatch.setattr(service_module, "COALESCE_SECONDS", 60)
    incoming(service, 1)
    await service.tick()
    await asyncio.sleep(0)
    await service.stop()
    assert not calls and service.task is None and state.snapshot()["cursor"] == 0
