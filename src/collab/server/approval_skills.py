"""Strong acceptance of an exact peer skill request, never skill execution.

Only explicitly strong requests enter this adapter. Frontends must recognize
the server-owned response; legacy chat is not strong authorization.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from ..protocol import Envelope, KIND_CHAT, KIND_SYSTEM
from ..skill_sharing import NAME_RE
from . import approval_alarms as events
from . import approval_challenge as challenges
from .approval_store import Rejected, _id, _text

SOURCE_TTL = 86400
MAX_SOURCE_BYTES = 32768
MAX_BINDINGS = 4096
MAX_OWNER_BINDINGS = 256
REQUEST_ID = re.compile(r"sr_[0-9a-f]{12}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_skill_bindings (
 session_id TEXT NOT NULL, source_seq INTEGER NOT NULL, owner_id TEXT NOT NULL,
 requester_id TEXT NOT NULL, source_id TEXT NOT NULL,
 source_hash TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
 expires_at INTEGER NOT NULL, state TEXT NOT NULL, event_id TEXT,
 PRIMARY KEY(session_id,source_seq), UNIQUE(session_id,owner_id,requester_id,source_id));
CREATE INDEX IF NOT EXISTS approval_skill_bindings_owner ON approval_skill_bindings(session_id,owner_id);
"""


class SkillApprovals:
    def __init__(self, core):
        self.core = core
        with core.store._lock:
            core.store._db.executescript(SCHEMA)

    def _source(self, db, owner, seq, now):
        if type(seq) is not int or not 0 < seq <= 2**63 - 1:
            raise Rejected("invalid_source", 400)
        # Existing chat bodies may be larger than approval inputs. Bound the
        # SQL result itself before sqlite3 allocates a Python payload string.
        row = db.execute("SELECT substr(payload,1,?) AS payload,sender_id,recipient_id,kind FROM events WHERE seq=? AND recipient_id=?",
                         (MAX_SOURCE_BYTES + 1, seq, owner)).fetchone()
        if row is None:
            raise Rejected("unknown_skill_request", 404)
        if row["kind"] != KIND_CHAT or not row["sender_id"] or row["sender_id"] == owner:
            raise Rejected("invalid_skill_request", 400)
        self.core._active_person(db, row["sender_id"])
        if len(row["payload"]) > MAX_SOURCE_BYTES or len(row["payload"].encode("utf-8")) > MAX_SOURCE_BYTES:
            raise Rejected("skill_request_too_large", 413)
        try:
            env = json.loads(row["payload"])
        except (ValueError, RecursionError):
            raise Rejected("invalid_skill_request", 400) from None
        if type(env) is not dict:
            raise Rejected("invalid_skill_request", 400)
        if env.get("fromId") != row["sender_id"] or env.get("toId") != owner:
            raise Rejected("skill_identity_mismatch", 403)
        body = env.get("body")
        source = body.get("skill_request") if type(body) is dict else None
        if type(source) is not dict or set(source) != {"id", "skill", "args", "required_strength"}:
            raise Rejected("invalid_skill_request", 400)
        if (type(source["id"]) is not str or REQUEST_ID.fullmatch(source["id"]) is None
                or type(source["skill"]) is not str or NAME_RE.fullmatch(source["skill"]) is None
                or type(source["args"]) is not str or len(source["args"]) > 4000
                or source["required_strength"] != "strong"):
            raise Rejected("invalid_skill_request", 400)
        try:
            if type(env.get("ts")) is not str:
                raise ValueError("invalid timestamp")
            stamp = datetime.fromisoformat(env["ts"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timestamp needs timezone")
            issued = int(stamp.timestamp())
            expires = issued + SOURCE_TTL
        except (ValueError, TypeError, OverflowError):
            raise Rejected("invalid_skill_timestamp", 400) from None
        if issued > now or now >= expires:
            raise Rejected("skill_request_expired", 410)
        return source, row["sender_id"], expires, hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()

    def prepare(self, token, *, source_seq):
        core = self.core
        with core._transaction(token) as (db, owner, now):
            source, requester, expires, fingerprint = self._source(db, owner, source_seq, now)
            db.execute("DELETE FROM approval_skill_bindings WHERE expires_at<=?", (now,))
            binding = db.execute("SELECT * FROM approval_skill_bindings WHERE session_id=? AND owner_id=? AND (source_seq=? OR (requester_id=? AND source_id=?))",
                                 (core.session_id, owner, source_seq, requester, source["id"])).fetchone()
            if binding is not None:
                if binding["source_seq"] != source_seq or binding["source_hash"] != fingerprint:
                    raise Rejected("skill_request_changed", 409)
                if binding["state"] == "accepted":
                    raise Rejected("skill_already_accepted", 409)
                current = db.execute("SELECT state,expires_at,digest,receipt_expires FROM approval_requests WHERE id=?",
                                     (binding["request_id"],)).fetchone()
                receipt_expired = current is not None and current["state"] == "approved" and now >= current["receipt_expires"]
                if current is not None and now < current["expires_at"] and not receipt_expired:
                    if current["state"] not in ("pending", "approved"):
                        raise Rejected("request_unavailable", 409)
                    return {"api_version": 1, "request_id": binding["request_id"], "source_seq": source_seq,
                            "digest": current["digest"], "expires_at": current["expires_at"]}
                if receipt_expired:
                    db.execute("UPDATE approval_requests SET state='declined',receipt_hash=NULL WHERE id=?", (binding["request_id"],))
                    db.execute("DELETE FROM approval_challenges WHERE request_id=?", (binding["request_id"],))
            if binding is None and (db.execute("SELECT COUNT(*) FROM approval_skill_bindings").fetchone()[0] >= MAX_BINDINGS
                    or db.execute("SELECT COUNT(*) FROM approval_skill_bindings WHERE owner_id=?", (owner,)).fetchone()[0] >= MAX_OWNER_BINDINGS):
                raise Rejected("skill_binding_capacity", 429)
            canonical = challenges.review({"api_version": 1, "session_id": core.session_id,
                "requester_id": requester, "target_id": owner, "subject": "Accept peer skill request",
                "destination": "participant:" + owner, "authority": "skill-request:accept",
                "payload": {"source_seq": source_seq, "source_fingerprint": fingerprint,
                            "skill_request_id": source["id"], "skill": source["skill"], "args": source["args"],
                            "effect": "Record acceptance; execution remains under the receiving host permissions"}})
            request = core._insert_request(db, requester, owner, canonical, "Accept peer skill request", now,
                                           expires=expires, kind="skill-accept")
            db.execute("INSERT OR REPLACE INTO approval_skill_bindings VALUES(?,?,?,?,?,?,?,?, 'prepared',NULL)",
                       (core.session_id, source_seq, owner, requester, source["id"], fingerprint, request["request_id"], expires))
            return {**request, "source_seq": source_seq}

    def accept(self, token, request_id, *, receipt):
        core = self.core
        with core._transaction(token) as (db, owner, now):
            binding = db.execute("SELECT * FROM approval_skill_bindings WHERE request_id=? AND session_id=? AND owner_id=?",
                                 (_text(request_id), core.session_id, owner)).fetchone()
            if binding is None:
                raise Rejected("unknown_skill_approval", 404)
            if binding["state"] != "prepared":
                raise Rejected("skill_already_accepted", 409)
            source, requester, _, fingerprint = self._source(db, owner, binding["source_seq"], now)
            if fingerprint != binding["source_hash"]:
                raise Rejected("skill_request_changed", 409)
            request = core._request(db, owner, request_id, now)
            if request["kind"] != "skill-accept" or request["requester_id"] != requester:
                raise Rejected("skill_approval_binding", 403)
            request, key = core._receipt(db, owner, request_id, receipt, request["digest"], now)
            event_id = _id("sa_")
            person = db.execute("SELECT name FROM participants WHERE id=?", (owner,)).fetchone()
            peer = db.execute("SELECT name FROM participants WHERE id=?", (requester,)).fetchone()
            env = Envelope(kind=KIND_SYSTEM, sender=person[0], sender_id=owner,
                to=peer[0], to_id=requester, room=None,
                ts=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                text="Accepted the reviewed peer skill request under the enrolled-key policy; execution has not been reported complete.",
                body={"event": "skill-approval", "event_id": event_id,
                    "strong_skill_response": {"id": source["id"], "state": "accepted", "source_seq": binding["source_seq"],
                        "request_id": request_id, "digest": request["digest"], "strength": "strong",
                        "credential_trust": "tofu-unverified"}})
            events.enqueue_event(db, core.session_id, now, event_id, env)
            db.execute("UPDATE approval_requests SET state='consumed',receipt_hash=NULL WHERE id=?", (request_id,))
            db.execute("UPDATE approval_skill_bindings SET state='accepted',event_id=? WHERE request_id=?", (event_id, request_id))
            core._audit(db, owner, "skill_request_accepted", now, request_id,
                        source_seq=binding["source_seq"], fingerprint=key["fingerprint"], digest=request["digest"],
                        strength="strong", credential_trust="tofu-unverified", event_id=event_id)
            return {"api_version": 1, "state": "accepted", "request_id": request_id,
                    "source_seq": binding["source_seq"], "event_id": event_id, "execution": "not_performed"}

    def status(self, token, request_id):
        core = self.core
        with core._transaction(token, charge=False) as (db, owner, now):
            row = db.execute("SELECT source_seq,state,event_id,expires_at FROM approval_skill_bindings WHERE request_id=? AND session_id=? AND owner_id=?",
                             (_text(request_id), core.session_id, owner)).fetchone()
            if row is None:
                raise Rejected("unknown_skill_approval", 404)
            return {"api_version": 1, "request_id": request_id, **dict(row), "execution": "not_performed"}
