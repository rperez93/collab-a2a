"""Real SQLite/outbox and in-memory hub delivery; no providers or live state."""
import asyncio
import json
import threading

import pytest

from collab.protocol import Envelope, KIND_CHAT, KIND_SYSTEM, client_kind_refusal
from collab.server import approval_alarms as alarms
from collab.server.approval_store import ApprovalStore, Rejected
from collab.server.hub import Hub
from collab.server.store import Store
from collab.wake import WAKE_KINDS
from test_approval_store import hub, signer, enrol, finish, spki


def serving(hub):
    return Hub(hub.store, session_id="s_test", host_name="test")


def pending(hub):
    return hub.store._db.execute("SELECT COUNT(*) FROM approval_alarm_outbox").fetchone()[0]


def test_alarm_is_atomic_with_enrollment(hub, signer, monkeypatch):
    challenge = hub.api.begin_enrol("test-bob", method="hello")
    original = alarms.enqueue
    def failed(*args):
        original(*args)
        if args[3] == "tofu_enrolled":
            raise RuntimeError("injected failure after alarm insertion")
    monkeypatch.setattr(alarms, "enqueue", failed)
    with pytest.raises(RuntimeError):
        finish(hub, signer, challenge)
    assert hub.api.keys("test-bob") == [] and pending(hub) == 0
    monkeypatch.setattr(alarms, "enqueue", original)
    finish(hub, signer, challenge)
    assert pending(hub) == 1


def test_restart_and_concurrent_drains_persist_one_safe_loud_event(hub, signer):
    enrol(hub, signer)
    # No handler callback ran. Another connection must recover the pending alarm.
    other = Store(hub.path)
    try:
        core = ApprovalStore(other, "s_test", clock=lambda: hub.now[0])
        host = Hub(other, session_id="s_test", host_name="test")
        async def run():
            sub = await host.subscribe(hub.people["alice"].id)
            assert sorted(await asyncio.gather(alarms.drain(host, core), alarms.drain(host, core))) == [0, 1]
            event = sub.queue.get_nowait()
            assert sub.queue.empty()
            return event
        event = asyncio.run(run())
        assert pending(hub) == 0 and other.max_seq() == 1
        assert event.kind == KIND_SYSTEM and "SECURITY:" in event.render_line()
        assert "TOFU, not attested" in event.text
        assert event.body["owner_id"] == hub.people["bob"].id
        encoded = json.dumps(event.to_dict())
        for secret in ("test-bob", "collab.test.key", spki(signer), "untrusted review content"):
            assert secret not in encoded
        assert set(event.body) == {"event", "alarm_id", "owner_id", "action", "credential_trust", "usable_at", "fingerprint"}
        assert KIND_SYSTEM not in WAKE_KINDS and client_kind_refusal(KIND_SYSTEM)
        from collab.client.watch import format_event
        from collab.client.tui import _body_lines
        assert "SECURITY:" in format_event(event)
        assert "SECURITY:" in " ".join(_body_lines(event, 80))
    finally:
        other.close()


def test_capacity_refuses_transition_without_losing_pending_alarm(hub, signer, monkeypatch):
    monkeypatch.setattr(alarms, "MAX_PENDING", 1)
    cred, _ = enrol(hub, signer)
    with pytest.raises(Rejected, match="alarm_capacity"):
        hub.api.report_missing("test-bob", cred)
    assert hub.api.keys("test-bob")[0]["state"] == "active" and pending(hub) == 1
    asyncio.run(alarms.drain(serving(hub), hub.api))
    assert hub.api.report_missing("test-bob", cred)["state"] == "frozen"
    assert pending(hub) == 1


def test_repeated_frozen_report_does_not_churn_epoch_audit_or_alarm(hub, signer):
    cred, _ = enrol(hub, signer)
    hub.api.report_missing("test-bob", cred)
    before = hub.api.audit("test-bob")
    epoch = hub.store._db.execute("SELECT generation FROM approval_generation").fetchone()[0]
    assert hub.api.report_missing("test-bob", cred)["state"] == "frozen"
    assert hub.api.audit("test-bob") == before and pending(hub) == 2
    assert hub.store._db.execute("SELECT generation FROM approval_generation").fetchone()[0] == epoch
    host = serving(hub)
    asyncio.run(alarms.drain(host, hub.api))
    frozen = hub.store.history()[-1]
    assert frozen.body["verified_human"] is False
    assert "not proof of a human gesture" in frozen.text


@pytest.mark.parametrize("failure", ["persist_return", "deliver"])
def test_post_commit_failure_closes_feeds_before_newer_sequence(hub, signer, monkeypatch, failure):
    enrol(hub, signer)
    host = serving(hub)
    async def run():
        sub = await host.subscribe(hub.people["alice"].id)
        if failure == "persist_return":
            original = alarms._persist_next
            def broken(core):
                original(core)
                raise RuntimeError("after DB commit")
            monkeypatch.setattr(alarms, "_persist_next", broken)
        else:
            original = host._deliver
            async def broken(env):
                await original(env)
                raise RuntimeError("after partial fan-out")
            monkeypatch.setattr(host, "_deliver", broken)
        with pytest.raises(RuntimeError):
            await alarms.drain(host, hub.api)
        assert sub.close_reason == "slow-consumer" and sub.queue.get_nowait() is None
        assert sub.queue.empty() and pending(hub) == 0
        monkeypatch.undo()
        await host.publish(Envelope(kind=KIND_CHAT, text="later", sender="test"))
        assert sub.queue.empty()  # no higher sequence can advance this old feed
        replay = hub.store.since(0, viewer=hub.people["alice"].id)
        assert [e.kind for e in replay] == [KIND_SYSTEM, KIND_CHAT]
        assert [e.seq for e in replay] == [1, 2]
        assert await alarms.drain(host, hub.api) == 0
    asyncio.run(run())


def test_repeated_cancellation_waits_for_ordered_fanout(hub, signer, monkeypatch):
    enrol(hub, signer)
    host = serving(hub)
    committed, release = threading.Event(), threading.Event()
    original = alarms._persist_next
    def paused(core):
        env = original(core)
        if env is not None:
            committed.set()
            assert release.wait(timeout=5)
        return env
    monkeypatch.setattr(alarms, "_persist_next", paused)
    async def run():
        sub = await host.subscribe(hub.people["alice"].id)
        task = asyncio.create_task(alarms.drain(host, hub.api))
        try:
            assert await asyncio.to_thread(committed.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            later = asyncio.create_task(host.publish(Envelope(kind=KIND_CHAT, text="later")))
            await asyncio.sleep(0)
            assert not later.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await later
        assert [sub.queue.get_nowait().seq, sub.queue.get_nowait().seq] == [1, 2]
        assert sub.queue.empty() and not sub.close_reason
    asyncio.run(run())


def test_lifecycle_timer_drains_without_handler_hint(hub, signer, monkeypatch):
    enrol(hub, signer)
    monkeypatch.setattr(alarms, "RETRY_SECONDS", 0.01)
    from fastapi import FastAPI
    app = FastAPI()
    host = serving(hub)
    alarms.install(app, host, hub.api)
    async def run():
        sub = await host.subscribe(hub.people["alice"].id)
        for callback in app.router.on_startup:
            await callback()
        try:
            env = await asyncio.wait_for(sub.queue.get(), timeout=3)
            assert env.kind == KIND_SYSTEM and pending(hub) == 0
        finally:
            for callback in app.router.on_shutdown:
                await callback()
        assert app.state.approval_alarm_task.done()
    asyncio.run(run())


def test_same_timestamp_alarms_follow_transaction_insertion_order(hub, signer, monkeypatch):
    cred, _ = enrol(hub, signer, ready=False)
    # Force the opposite lexical order to avoid a random, flaky regression.
    hub.store._db.execute("UPDATE approval_alarm_outbox SET id='zz-first'")
    hub.store._db.commit()
    monkeypatch.setattr(alarms.secrets, "token_urlsafe", lambda size: "a-second")
    hub.api.report_missing("test-bob", cred)
    asyncio.run(alarms.drain(serving(hub), hub.api))
    events = hub.store.history()
    assert [event.body["action"] for event in events] == ["enrolled", "missing"]
    assert all(len(event.body["fingerprint"]) == 64 for event in events)
    assert all(event.body["fingerprint"] in event.text for event in events)


def test_signed_key_change_alarm_survives_audit_eviction(hub, signer, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec
    from collab.server.approval_keys import KeyChanges
    from test_approval_key_changes import propose, authorize
    monkeypatch.setattr("collab.server.approval_store.MAX_AUDIT", 1)
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    req = propose(changes, ec.generate_private_key(ec.SECP256R1()))
    receipt = authorize(hub, signer, req)
    changes.apply("test-bob", req["change_id"], receipt=receipt["receipt"])
    assert len(hub.api.audit("test-bob")) == 1 and pending(hub) == 2
    asyncio.run(alarms.drain(serving(hub), hub.api))
    events = hub.store.history()
    assert [event.body["action"] for event in events] == ["enrolled", "add"]
    assert events[-1].body["authorizer_id"] == hub.people["bob"].id
    encoded = json.dumps([event.to_dict() for event in events])
    assert receipt["receipt"] not in encoded and req["digest"] not in encoded
