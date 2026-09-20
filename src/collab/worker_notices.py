"""One bounded decision doorbell shared by monitors and wake delivery.

Inbox-read state is intentionally irrelevant here: a worker has its own cursor,
and its unanswered decision must remain visible even after the main agent has
read every peer message. Leases recover a killed deliverer; tokens prevent its
late completion from stealing a newer delivery's claim.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .worker import Store


def enabled(root: Path) -> bool:
    config = Store(root).configuration()
    return bool(config and config.get("enabled"))


@contextmanager
def _db(root):
    path = Path(root) / "worker-notices.db"
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        path.chmod(0o600)
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS notice (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _load(db):
    row = db.execute("SELECT data FROM notice WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {}


def _save(db, data):
    db.execute("INSERT OR REPLACE INTO notice VALUES(1,?)", (json.dumps(data),))


def claim(root: Path, *, now: float | None = None, repeat: float | None = None,
          min_gap: float | None = None, lease: float = 120) -> dict | None:
    from .runtime_settings import get
    repeat = get("worker_notice_repeat") if repeat is None else repeat
    min_gap = get("worker_notice_gap") if min_gap is None else min_gap
    if not enabled(root):
        return None
    state = Store(root).status()
    pending = state["pending"]
    error = state["error"]
    if not pending and not error:
        return None
    now = time.time() if now is None else now
    fingerprint = hashlib.sha256(json.dumps({"ids": [r["id"] for r in pending],
        "error": error, "generation": state["generation"]}, sort_keys=True).encode()).hexdigest()
    # Only local counts and fixed instructions enter the working thread. The
    # question and provider stderr remain an explicit, inspectable read away.
    text = "Collab worker: "
    if pending:
        text += (f"{len(pending)} decision(s) need your input. Run `collab worker pending`, "
                 "then `collab worker reply ID 'decision'` to continue coordination. ")
    if error:
        text += "The conversation worker needs recovery. Run `collab worker status` to inspect the error. "
    text += "Continue your task; resolve this at the next safe boundary."
    with _db(root) as db:
        prior = _load(db)
        if prior.get("token") and prior.get("lease_until", 0) > now:
            return None
        if "delivered_at" in prior:
            gap = repeat if prior.get("fingerprint") == fingerprint else min_gap
            if now - prior["delivered_at"] < gap:
                return None
        token = uuid.uuid4().hex
        prior.update(token=token, lease_until=now + lease, claimed_fingerprint=fingerprint)
        _save(db, prior)
        return {"token": token, "text": text}


def commit(root: Path, token: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _db(root) as db:
        state = _load(db)
        if state.get("token") != token:
            return False
        state.update(fingerprint=state.pop("claimed_fingerprint"), delivered_at=now,
                     token="", lease_until=0)
        _save(db, state)
        return True


def release(root: Path, token: str) -> None:
    with _db(root) as db:
        state = _load(db)
        if state.get("token") == token:
            state.update(token="", lease_until=0)
            _save(db, state)
