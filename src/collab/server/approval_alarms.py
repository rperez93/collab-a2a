"""Transactional, non-secret approval alarms for the human session transcript.

One serving Hub owns fan-out. The database survives crashes; its subscribers
must replay on a delivery failure, before any newer sequence can escape.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import secrets

from ..protocol import DEFAULT_ROOM, Envelope, KIND_SYSTEM, scrub

MAX_PENDING = 256
SECURITY_RESERVE = 32
BATCH_SIZE = 16
RETRY_SECONDS = 1.0
log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_alarm_outbox (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload TEXT NOT NULL);
"""


def enqueue(db, session_id, owner, kind, now, metadata):
    """Called inside the authority transaction; never publish caller prose."""
    actions = {"tofu_enrolled": "enrolled", "key_reported_missing": "missing",
               "key_reported_changed": "changed", "key_change_applied": "key-change",
               "host_credentials_reset": "reset"}
    if kind not in actions:
        return
    fingerprint = metadata["fingerprint"]
    if (type(fingerprint) is not str or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise ValueError("invalid internal approval fingerprint")
    identity = "aa_" + secrets.token_urlsafe(24)
    action = actions[kind]
    body = {"event": "approval-security", "alarm_id": identity,
            "owner_id": owner, "action": action, "credential_trust": "tofu-unverified",
            "fingerprint": fingerprint}
    if action == "enrolled":
        text = f"SECURITY: participant {owner} enrolled a first approval credential (TOFU, not attested)."
    elif action == "reset":
        body["verified_human"] = False
        text = (f"SECURITY: the hub machine reset the approval credentials of host {owner}; every host key "
                "is retired and a first key may be enrolled again (TOFU, with the cooling-off). "
                "If nobody at the hub machine did this, treat that machine as compromised.")
    elif action in ("missing", "changed"):
        body["verified_human"] = False
        text = (f"SECURITY: participant {owner} reported {action} approval key material; "
                "the credential is frozen. This report is not proof of a human gesture.")
    else:
        change = metadata.get("kind_applied")
        if change not in ("add", "rotate", "recover"):
            raise ValueError("invalid internal approval alarm action")
        authorizer = metadata["authorizer_id"]
        body.update(action=change, authorizer_id=authorizer)
        text = (f"SECURITY: approval credential {change} committed for participant {owner}; "
                f"authorized by participant {authorizer} under the enrolled-key policy (TOFU, not attested).")
    if action in ("enrolled", "key-change"):
        usable = metadata["usable_at"]
        body["usable_at"] = usable
        text += f" New credential cooldown until {datetime.fromtimestamp(usable, timezone.utc).isoformat()}."
    text += f" Credential fingerprint: {fingerprint}."
    env = Envelope(kind=KIND_SYSTEM, sender="collab", room=DEFAULT_ROOM,
                   ts=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                   text=scrub(text), body=body)
    enqueue_event(db, session_id, now, identity, env)


def enqueue_event(db, session_id, now, identity, env):
    """Internal adoption boundary for server-created approval system events."""
    if env.kind != KIND_SYSTEM:
        raise ValueError("approval event must be server-owned")
    # The security alarm keeps a reserve: a burst of remote outcomes can
    # fill the outbox only up to MAX_PENDING - SECURITY_RESERVE.
    limit = MAX_PENDING if env.body.get("event") == "approval-security" else MAX_PENDING - SECURITY_RESERVE
    if db.execute("SELECT COUNT(*) FROM approval_alarm_outbox").fetchone()[0] >= limit:
        from .approval_store import Rejected
        raise Rejected("alarm_capacity", 429)
    db.execute("INSERT INTO approval_alarm_outbox VALUES(?,?,?,?)",
               (identity, session_id, now, json.dumps(env.to_dict())))


def _persist_next(core):
    """Transfer one outbox row to events atomically, never a second event."""
    with core.store._lock:
        db = core.store._db
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT id,payload FROM approval_alarm_outbox WHERE session_id=? ORDER BY rowid LIMIT 1",
                             (core.session_id,)).fetchone()
            if row is None:
                db.commit()
                return None
            env = Envelope.from_dict(json.loads(row["payload"]))
            cur = db.execute("INSERT INTO events(kind,room,sender,recipient,sender_id,recipient_id,ts,payload) VALUES(?,?,?,?,?,?,?,?)",
                             (env.kind, env.room, env.sender, env.to, env.sender_id,
                              env.to_id or ("unresolved:private" if env.to else None), env.ts, ""))
            env.seq = int(cur.lastrowid)
            db.execute("UPDATE events SET payload=? WHERE seq=?", (json.dumps(env.to_dict()), env.seq))
            db.execute("DELETE FROM approval_alarm_outbox WHERE id=?", (row["id"],))
            db.commit()
            return env
        except BaseException:
            db.rollback()
            raise


async def _close_feeds(hub):
    # No await after acquiring the subscription lock: clear every queued later
    # sequence and force normal durable replay via the existing retry sentinel.
    async with hub._lock:
        for subscriptions in hub._subs.values():
            for sub in subscriptions:
                if sub.close_reason == "revoked":
                    continue
                sub.close_reason = "slow-consumer"
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)


async def _drain(hub, core, limit):
    count = 0
    async with hub._publish_lock:
        for _ in range(limit):
            try:
                # The entire critical section runs in its own shielded task.
                # Caller cancellation cannot abandon a still-running thread
                # between DB commit and ordered fan-out.
                env = await asyncio.to_thread(_persist_next, core)
                if env is None:
                    break
                await hub._deliver(env)
                count += 1
            except BaseException:
                # Persistence may have committed even if its return failed.
                # Close ALL existing feeds before releasing publish ordering.
                await _close_feeds(hub)
                raise
    return count


async def drain(hub, core, *, limit=BATCH_SIZE):
    """Publish at most one bounded batch; cancellation waits for safe fan-out."""
    if type(limit) is not int or not 1 <= limit <= BATCH_SIZE:
        raise ValueError("invalid alarm batch size")
    task = asyncio.create_task(_drain(hub, core, limit))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A normal request/shutdown cancellation must not release the ordering
        # lock while the persistence thread could still commit an older seq.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # Retrieve any exception; safety cleanup has already completed while
        # holding publication ordering. Preserve caller cancellation otherwise.
        task.result()
        raise


def install(app, hub, core):
    """Register startup/retry/shutdown hooks; return an optional wake Event.

    A mutation handler may set the returned event after success, but alarms
    remain durable and the bounded timer drains them without any such hint.
    """
    wake = asyncio.Event()

    async def loop():
        while True:
            wake.clear()
            try:
                await drain(hub, core)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fixed text: no proof, payload, or storage exception contents.
                log.warning("approval alarm delivery failed; retrying durable backlog")
            try:
                await asyncio.wait_for(wake.wait(), timeout=RETRY_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def start():
        app.state.approval_alarm_task = asyncio.create_task(loop())

    async def stop():
        task = getattr(app.state, "approval_alarm_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", stop)
    return wake
