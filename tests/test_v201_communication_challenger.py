"""Independent integration counterexamples for the v2.0.1 communication bridge."""
import asyncio
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from collab import worker
from collab.client.inbox import Inbox
from collab.client.worker_service import Conversation
from collab.compatibility import request_headers
from collab.protocol import Envelope, EXT_PREFIX


def make_service(root, url, name, token, http, participant_id=""):
    daemon = SimpleNamespace(
        profile=SimpleNamespace(dir=root, name=name, participant_id=participant_id,
                                url=url, token=token),
        inbox=Inbox(root), snapshot={"participants": [{"name": "alice"}, {"name": "bob"}]},
        _http=http)
    return Conversation(daemon)


def install_fake_native(tmp_path, monkeypatch):
    """Exercise native argv/stdin/output plumbing without billing an account."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = '''#!PYTHON
import json,sys,pathlib
provider=pathlib.Path(sys.argv[0]).name
p=json.loads(sys.stdin.read().split("Conversation input:\\n",1)[1])
r={"summary":"Coordinate the agreed API; one evidence-backed decision.","replies":[],"escalations":[]}
if p["main_answers"]:
    a=p["main_answers"][0]
    r["replies"]=[{"to":a["source"]["sender"],"room":a["source"]["room"],"text":a["text"]}]
else:
    for e in p["events"]:
        if e["text"]=="The tests pass. Please verify the API choice.":
            r["replies"].append({"to":e["sender"],"room":e["room"],"text":"Should we change the API?"})
        elif e["text"]=="Should we change the API?":
            r["escalations"].append({"seq":e["seq"],"reason":"decision","question":"Change the API or preserve compatibility?"})
        elif e["text"]=="Keep compatibility; the acceptance tests require it.":
            r["replies"].append({"to":e["sender"],"room":e["room"],"text":"Acknowledged."})
if provider=="codex":
    pathlib.Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text(json.dumps(r))
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20,"cached_input_tokens":30}}))
elif provider=="opencode":
    print(json.dumps({"type":"text","part":{"text":json.dumps(r)}}))
    print(json.dumps({"type":"step_finish","part":{"tokens":{"input":70,"output":20,"cache":{"read":30,"write":0}},"cost":0.001}}))
else:
    print(json.dumps({"result":json.dumps(r),"usage":{"input_tokens":70,"output_tokens":20,"cache_read_input_tokens":30},"total_cost_usd":0.001}))
'''.replace("PYTHON", sys.executable)
    for name in ("codex", "claude", "opencode", "cursor-agent"):
        path = bindir / name
        path.write_text(script)
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CURSOR_API_KEY", "synthetic-not-a-real-key")


async def sync_inbox(service):
    response = await service.daemon._http.get(
        service.daemon.profile.url + EXT_PREFIX + "/history",
        headers=request_headers(service.daemon.profile.token))
    response.raise_for_status()
    for event in response.json()["events"]:
        service.daemon.inbox.record(Envelope.from_dict(event))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude", "opencode", "cursor"])
async def test_two_native_workers_relay_main_handoff_and_decision_then_go_quiet(
        tmp_path, monkeypatch, live_server, provider):
    """A real hub must carry the complete exchange without marking main reads."""
    install_fake_native(tmp_path, monkeypatch)
    url = live_server["base"]
    async with httpx.AsyncClient() as http:
        joined = await http.post(url + EXT_PREFIX + "/join", json={
            "invite": live_server["invite"], "name": "bob", "version": "2.0.0", "protocol_major": 2})
        joined.raise_for_status()
        services = [make_service(tmp_path / "alice", url, "alice", live_server["host_token"], http),
                    make_service(tmp_path / "bob", url, "bob", joined.json()["token"], http)]
        alice, bob = services
        try:
            for service in services:
                worker.Store(service.root).configure({"agent": provider, "model": "test/model",
                    "scope": "Agree on the API using acceptance-test evidence, no repeated debate."})
            store = worker.Store(alice.root)
            delivery_id = store.send("The tests pass. Please verify the API choice.", to="bob")
            assert worker.Store(alice.root).outbox()[0]["id"] == delivery_id
            await Conversation(alice.daemon).turn()  # restart after main send
            assert not store.outbox() and store.status().get("attempts_total", 0) == 0
            await sync_inbox(bob)
            await bob.turn()
            await sync_inbox(alice)
            await alice.turn()
            assert len(store.pending()) == 1
            store.answer(store.pending()[0]["id"], "Keep compatibility; the acceptance tests require it.")
            await Conversation(alice.daemon).turn()
            await sync_inbox(bob)
            await bob.turn()
            await sync_inbox(alice)
            await alice.turn()  # acknowledgement must end the conversation
            before = [worker.Store(s.root).status().get("attempts_total", 0) for s in services]
            for _ in range(3):
                for service in services:
                    await sync_inbox(service)
                    await service.turn()
            after = [worker.Store(s.root).status().get("attempts_total", 0) for s in services]
            assert before == after
            assert not store.pending() and not store.snapshot()["answers"]
            assert all(s.daemon.inbox.unread_count() > 0 for s in services)
            history = await http.get(url + EXT_PREFIX + "/history", headers=request_headers(live_server["host_token"]))
            chat = [e for e in history.json()["events"] if e["kind"] == "chat"]
            assert [e["text"] for e in chat] == [
                "The tests pass. Please verify the API choice.", "Should we change the API?",
                "Keep compatibility; the acceptance tests require it.", "Acknowledged."]
        finally:
            for service in services:
                await service.stop()
                service.daemon.inbox.close()


@pytest.mark.asyncio
async def test_main_delivery_survives_restart_and_model_budget_backoff(tmp_path):
    """Model spending limits must not hold already authorized HTTP sends hostage."""
    attempts = []
    async def transport(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(503 if len(attempts) == 1 else 200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = make_service(tmp_path / "alice", "http://hub", "alice", "test", http)
        try:
            store = worker.Store(service.root)
            store.configure({"agent": "codex", "scope": "Coordinate"})
            generation = store.snapshot()["generation"]
            assert store.reserve_turn(expected_generation=generation, limit=1) == 0
            assert store.reserve_turn(expected_generation=generation, limit=1) > time.time()
            delivery = store.send("Proceed with the verified fix.", to="bob")
            await service.tick()
            await service.task
            assert store.outbox()[0]["id"] == delivery
            assert "limit" in store.status()["error"]
            store.defer(delivery, retry_at=0, error="retry now")
            restarted = Conversation(service.daemon)
            await restarted.tick()
            await restarted.task
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            assert not store.outbox() and store.status()["attempts_total"] == 1
            assert "limit" in store.status()["error"]
            await restarted.stop()
        finally:
            await service.stop()
            service.daemon.inbox.close()


@pytest.mark.asyncio
async def test_delivery_deadline_cancels_a_hub_that_never_acknowledges(tmp_path, monkeypatch):
    """An HTTP peer without response headers cannot occupy delivery forever."""
    from collab.client import worker_service
    original = worker_service.setting
    monkeypatch.setattr(worker_service, "setting", lambda key:
                        0.05 if key == "worker_delivery_timeout" else original(key))
    cancelled = asyncio.Event()
    async def transport(request):
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = make_service(tmp_path / "alice", "http://hub", "alice", "test", http)
        store = worker.Store(service.root)
        store.configure({"agent": "codex", "scope": "Coordinate"})
        delivery_id = store.send("handoff", to="bob")
        started = time.monotonic()
        cpu_started = time.process_time()
        try:
            await service.turn()
            assert time.monotonic() - started < 0.5
            assert time.process_time() - cpu_started < 0.1
            assert cancelled.is_set()
            assert store.outbox()[0]["id"] == delivery_id
            assert store.outbox()[0]["error"] == "TimeoutError"
        finally:
            await service.stop()
            service.daemon.inbox.close()


def test_main_send_requires_an_enabled_worker_and_explicit_safe_address(tmp_path):
    store = worker.Store(tmp_path)
    with pytest.raises(ValueError, match="start the worker"):
        store.send("handoff", to="bob")
    store.configure({"agent": "codex", "scope": "Coordinate"})
    for recipient in ("", " ", "bob\0", "b" * 201):
        with pytest.raises(ValueError):
            store.send("handoff", to=recipient)
    assert not store.outbox()
    store.off()
    with pytest.raises(ValueError, match="start the worker"):
        store.send("handoff", to="bob")


@pytest.mark.asyncio
async def test_maximum_escaped_guidance_still_allows_queued_main_input_to_progress(tmp_path, monkeypatch):
    """Individually valid settings must not form a permanently unencodable turn."""
    from collab import runtime_settings, worker_runtime
    runtime_settings.set_value("rules_text", "\x01" * 8000)
    runtime_settings.set_value("worker_instructions", "\x02" * 8000)
    async with httpx.AsyncClient() as http:
        service = make_service(tmp_path / "alice", "http://hub", "alice", "test", http)
        store = worker.Store(service.root)
        store.configure({"agent": "codex", "scope": "s" * 15998})
        store.commit_turn(expected_cursor=0, cursor=0, summary="\x03" * 4000,
                          replies=[], escalations=[])
        for _ in range(3):
            store.context("\x04" * 2600)
        seen = []
        async def provider(agent, model, payload, directory, **kwargs):
            seen.append(payload)
            assert len(worker_runtime._encode_payload(payload)) <= worker_runtime.MAX_INPUT_BYTES
            assert payload["local_rules"] == "\x01" * 8000
            assert payload["local_guidance"] == "\x02" * 8000
            return {"summary": "\x03" * 4000, "replies": [], "escalations": []}
        monkeypatch.setattr(worker_runtime, "run_turn", provider)
        try:
            for _ in range(3):
                await service.turn()
            assert len(seen) == 3 and all(p["main_context"] for p in seen)
            assert not store.snapshot()["context"] and not store.status()["error"]
        finally:
            await service.stop()
            service.daemon.inbox.close()


@pytest.mark.asyncio
async def test_delivery_does_not_buffer_a_flooding_acknowledgement(tmp_path, record_property):
    """A hub's unsolicited body must cost neither unbounded RAM nor drain CPU."""
    class Flood(httpx.AsyncByteStream):
        read = 0
        closed = False
        async def __aiter__(self):
            # A real peer may produce gigabytes. Ten MiB are sufficient to
            # detect eager buffering safely if this regression returns.
            for _ in range(10):
                self.read += 1
                yield b"x" * (1024 * 1024)
        async def aclose(self):
            self.closed = True
    flood = Flood()
    async def transport(request):
        return httpx.Response(200, stream=flood)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = make_service(tmp_path / "alice", "http://hub", "alice", "test", http)
        store = worker.Store(service.root)
        store.configure({"agent": "codex", "scope": "Coordinate"})
        store.send("handoff", to="bob")
        tracemalloc.start()
        started = time.process_time()
        try:
            await service.turn()
            cpu = time.process_time() - started
            _, peak = tracemalloc.get_traced_memory()
            record_property("delivery_cpu_seconds", cpu)
            record_property("delivery_peak_python_bytes", peak)
            assert flood.read == 0 and flood.closed
            assert peak < 1024 * 1024 and cpu < 0.5
            assert not store.outbox()
        finally:
            tracemalloc.stop()
            await service.stop()
            service.daemon.inbox.close()
