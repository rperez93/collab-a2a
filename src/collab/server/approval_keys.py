"""Signed key changes layered on the durable approval service.

The new key proves possession first. A separate request must then be approved
by an existing mature key (same owner for add/rotate, host for recovery).
No bearer-only path can install a replacement or unfreeze a credential.
"""
from __future__ import annotations

import hashlib
import json

from . import approval_challenge as challenges
from . import approval_crypto as crypto
from .approval_store import KEY_COOLDOWN, REQUEST_SECONDS, Rejected, _id, _text

MAX_ACTIVE_KEYS = 4
MAX_RETAINED_KEYS = 64
MAX_CHANGES = 128
MAX_OWNER_CHANGES = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_key_changes (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 authorizer_id TEXT NOT NULL, kind TEXT NOT NULL, replaced_id TEXT,
 generation INTEGER NOT NULL, credential_id TEXT NOT NULL,
 method TEXT NOT NULL, algorithm INTEGER NOT NULL, spki TEXT NOT NULL,
 fingerprint TEXT NOT NULL, canonical BLOB NOT NULL, digest TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, state TEXT NOT NULL,
 counter INTEGER, backup_eligible INTEGER, request_id TEXT);
CREATE INDEX IF NOT EXISTS approval_key_changes_owner ON approval_key_changes(session_id,owner_id);
"""


class KeyChanges:
    def __init__(self, core):
        self.core = core
        with core.store._lock:
            core.store._db.executescript(SCHEMA)

    def _policy(self, db, owner, kind, replaced, now, *, authorizer=None):
        core = self.core
        core._active_person(db, owner)
        if not core._generation(db, owner)[1]:
            raise Rejected("initial_enrollment_required", 409)
        keys = db.execute("SELECT credential_id,state FROM approval_keys WHERE session_id=? AND owner_id=?",
                          (core.session_id, owner)).fetchall()
        if len(keys) >= MAX_RETAINED_KEYS:
            raise Rejected("credential_capacity", 429)
        if kind == "recover":
            if replaced is not None or any(k["state"] == "active" for k in keys):
                raise Rejected("recovery_not_needed", 409)
            hosts = db.execute("SELECT id FROM participants WHERE is_host=1 AND revoked=0").fetchall()
            if len(hosts) != 1 or hosts[0][0] == owner:
                # Losing the only host key cannot silently bootstrap a new one.
                raise Rejected("host_recovery_unavailable", 409)
            selected = hosts[0][0]
        elif kind in ("add", "rotate"):
            selected = owner
            if kind == "add":
                if replaced is not None:
                    raise Rejected("invalid_replacement", 400)
                if sum(k["state"] == "active" for k in keys) >= MAX_ACTIVE_KEYS:
                    raise Rejected("active_credential_capacity", 429)
            else:
                old = core._key(db, owner, replaced)
                if old["state"] == "retired":
                    raise Rejected("credential_retired", 409)
                resulting_active = sum(k["state"] == "active" for k in keys) + 1 - int(old["state"] == "active")
                if resulting_active > MAX_ACTIVE_KEYS:
                    raise Rejected("active_credential_capacity", 429)
        else:
            raise Rejected("invalid_key_change", 400)
        if authorizer is not None and selected != authorizer:
            raise Rejected("authorizer_changed", 409)
        if db.execute("SELECT 1 FROM approval_keys WHERE session_id=? AND owner_id=? AND state='active' AND usable_at<=? LIMIT 1",
                      (core.session_id, selected, now)).fetchone() is None:
            raise Rejected("authorizer_not_ready", 409)
        return selected

    def _new_key(self, db, owner, identity, fingerprint):
        if db.execute("SELECT 1 FROM approval_keys WHERE session_id=? AND owner_id=? AND (credential_id=? OR fingerprint=?)",
                      (self.core.session_id, owner, identity, fingerprint)).fetchone():
            raise Rejected("credential_already_registered", 409)

    def _get(self, db, actor, identity, now, *, role="owner_id"):
        if role not in ("owner_id", "authorizer_id"):
            raise ValueError("invalid internal role")
        row = db.execute(f"SELECT * FROM approval_key_changes WHERE id=? AND session_id=? AND {role}=?",
                         (_text(identity), self.core.session_id, actor)).fetchone()
        if row is None:
            raise Rejected("unknown_key_change", 404)
        if now >= row["expires_at"]:
            raise Rejected("key_change_expired", 410)
        if row["generation"] != self.core._generation(db, row["owner_id"])[0]:
            raise Rejected("credential_changed", 409)
        return row

    def begin(self, token, *, kind, credential_id, method, algorithm, public_key_spki,
              replaced_credential_id=None, origin=None):
        core = self.core
        with core._transaction(token) as (db, owner, now):
            core._method(method, origin)
            core._credential_id(method, credential_id)
            crypto.load_key(public_key_spki, algorithm=algorithm, method=method)
            fingerprint = hashlib.sha256(crypto.decode(public_key_spki, limit=crypto.MAX_SPKI)).hexdigest()
            authorizer = self._policy(db, owner, kind, replaced_credential_id, now)
            self._new_key(db, owner, credential_id, fingerprint)
            core._prune(db, now)
            db.execute("DELETE FROM approval_key_changes WHERE expires_at<=?", (now,))
            if (db.execute("SELECT COUNT(*) FROM approval_key_changes").fetchone()[0] >= MAX_CHANGES
                    or db.execute("SELECT COUNT(*) FROM approval_key_changes WHERE session_id=? AND owner_id=?",
                                  (core.session_id, owner)).fetchone()[0] >= MAX_OWNER_CHANGES):
                raise Rejected("key_change_capacity", 429)
            identity = _id("ak_")
            generation = core._generation(db, owner)[0]
            subject = "Approve credential " + kind
            canonical = challenges.review({
                "api_version": 1, "session_id": core.session_id, "requester_id": owner,
                "target_id": authorizer, "subject": subject,
                "destination": "approval-registry:" + owner, "authority": "approval:key:" + kind,
                "payload": {"change_id": identity, "owner_id": owner, "generation": generation,
                            "credential_id": credential_id, "method": method, "algorithm": algorithm,
                            "fingerprint": fingerprint, "replaced_credential_id": replaced_credential_id,
                            "credential_trust": "tofu-unverified", "cooldown_seconds": KEY_COOLDOWN}})
            db.execute("INSERT INTO approval_key_changes(id,session_id,owner_id,authorizer_id,kind,replaced_id,generation,credential_id,method,algorithm,spki,fingerprint,canonical,digest,created_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'possession')",
                       (identity, core.session_id, owner, authorizer, kind, replaced_credential_id, generation,
                        credential_id, method, algorithm, public_key_spki, fingerprint, canonical.canonical,
                        canonical.digest, now, now + REQUEST_SECONDS))
            challenge = core._add_challenge(db, owner, purpose="key-possession", request_id=identity,
                credential_id=credential_id, method=method, origin=origin, digest=canonical.digest,
                subject=subject, requester_id=owner, generation=generation, now=now)
            core._audit(db, owner, "key_change_started", now, change_id=identity, kind_requested=kind,
                        fingerprint=fingerprint, authorizer_id=authorizer)
            return {**challenge, "change_id": identity, "authorizer_id": authorizer,
                    "canonical_review": crypto.encode(canonical.canonical), "review": json.loads(canonical.canonical)}

    def prove(self, token, change_id, *, challenge_id, proof):
        core = self.core
        with core._transaction(token) as (db, owner, now):
            change = self._get(db, owner, change_id, now)
            if change["state"] != "possession":
                raise Rejected("key_change_unavailable", 409)
            self._policy(db, owner, change["kind"], change["replaced_id"], now, authorizer=change["authorizer_id"])
            row = core._challenge(db, owner, challenge_id, "key-possession", now)
            if row["request_id"] != change_id or row["credential_id"] != change["credential_id"]:
                raise Rejected("challenge_binding", 403)
            assertion = core._verify(row, change["spki"], change["algorithm"], proof)
            now = core._now()
            if now >= min(row["expires_at"], change["expires_at"]):
                raise Rejected("challenge_expired", 410)
            canonical = challenges.Review(bytes(change["canonical"]), change["digest"])
            request = core._insert_request(db, owner, change["authorizer_id"], canonical,
                json.loads(canonical.canonical)["subject"], now, expires=change["expires_at"], kind="key-change")
            db.execute("UPDATE approval_challenges SET used=1 WHERE id=?", (challenge_id,))
            db.execute("UPDATE approval_key_changes SET state='approval',counter=?,backup_eligible=?,request_id=? WHERE id=?",
                       (assertion.counter if assertion else 0, int(assertion.backup_eligible) if assertion else None,
                        request["request_id"], change_id))
            core._audit(db, owner, "key_possession_verified", now, request["request_id"],
                        change_id=change_id, fingerprint=change["fingerprint"], credential_trust="tofu-unverified")
            return {**request, "change_id": change_id, "authorizer_id": change["authorizer_id"]}

    def apply(self, token, change_id, *, receipt):
        core = self.core
        with core._transaction(token) as (db, authorizer, now):
            change = self._get(db, authorizer, change_id, now, role="authorizer_id")
            if change["state"] != "approval":
                raise Rejected("key_change_unavailable", 409)
            owner = change["owner_id"]
            self._policy(db, owner, change["kind"], change["replaced_id"], now, authorizer=authorizer)
            request, signer = core._receipt(db, authorizer, change["request_id"], receipt, change["digest"], now)
            if request["kind"] != "key-change" or bytes(request["canonical"]) != bytes(change["canonical"]):
                raise Rejected("key_change_binding", 403)
            self._new_key(db, owner, change["credential_id"], change["fingerprint"])
            if change["kind"] == "rotate":
                db.execute("UPDATE approval_keys SET state='retired' WHERE session_id=? AND owner_id=? AND credential_id=?",
                           (core.session_id, owner, change["replaced_id"]))
            elif change["kind"] == "recover":
                db.execute("UPDATE approval_keys SET state='retired' WHERE session_id=? AND owner_id=?",
                           (core.session_id, owner))
            db.execute("INSERT INTO approval_keys VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                       (core.session_id, owner, change["credential_id"], change["method"], change["algorithm"],
                        change["spki"], change["fingerprint"], now, now + KEY_COOLDOWN,
                        change["counter"], change["backup_eligible"]))
            db.execute("UPDATE approval_generation SET generation=generation+1 WHERE session_id=? AND owner_id=?",
                       (core.session_id, owner))
            db.execute("DELETE FROM approval_challenges WHERE session_id=? AND owner_id=?", (core.session_id, owner))
            db.execute("UPDATE approval_requests SET state='consumed',receipt_hash=NULL WHERE id=?", (request["id"],))
            db.execute("UPDATE approval_key_changes SET state='applied' WHERE id=?", (change_id,))
            metadata = dict(change_id=change_id, kind_applied=change["kind"], fingerprint=change["fingerprint"],
                            authorizer_id=authorizer, signer_fingerprint=signer["fingerprint"],
                            strength="strong", credential_trust="tofu-unverified", usable_at=now + KEY_COOLDOWN)
            core._audit(db, owner, "key_change_applied", now, request["id"], **metadata)
            if authorizer != owner:
                core._audit(db, authorizer, "key_change_authorized", now, request["id"], target_id=owner, **metadata)
            return {"api_version": 1, "change_id": change_id, "state": "applied",
                    "credential": core._key_metadata(db, owner, change["credential_id"])}

    def status(self, token, change_id):
        core = self.core
        with core._transaction(token, charge=False) as (db, owner, now):
            # Applied changes remain inspectable despite the intentional epoch
            # advance. No read confers authority or releases a receipt.
            row = db.execute("SELECT state,request_id,authorizer_id,expires_at FROM approval_key_changes WHERE id=? AND session_id=? AND owner_id=?",
                             (_text(change_id), core.session_id, owner)).fetchone()
            if row is None:
                raise Rejected("unknown_key_change", 404)
            return {"api_version": 1, "change_id": change_id, **dict(row)}
