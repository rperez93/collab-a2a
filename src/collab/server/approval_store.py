"""Durable approval state, sharing the hub's participant transaction boundary.

Internal service only: no route enables it yet. Callers pass the bearer from
the authenticated request, not a participant name or caller-asserted ID. Each
transaction revalidates that token against the hub's current participant row.
The caller still owns operation-specific authorization and dispatch semantics.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import secrets
import time

from . import approval_challenge as challenges
from . import approval_crypto as crypto
from .store import token_hash

KEY_COOLDOWN = 600
REQUEST_SECONDS = 600
RECEIPT_SECONDS = 120
MAX_REQUESTS = 1024
MAX_OWNER_REQUESTS = 64
MAX_CHALLENGES = 1024
MAX_AUDIT = 1000
RATE_WINDOW = 60
RATE_LIMIT = 30
_HELLO_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_session (singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_generation (
 session_id TEXT NOT NULL, owner_id TEXT NOT NULL, generation INTEGER NOT NULL,
 bootstrapped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id,owner_id));
CREATE TABLE IF NOT EXISTS approval_keys (
 session_id TEXT NOT NULL, owner_id TEXT NOT NULL, credential_id TEXT NOT NULL,
 method TEXT NOT NULL, algorithm INTEGER NOT NULL, spki TEXT NOT NULL,
 fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL, usable_at INTEGER NOT NULL,
 state TEXT NOT NULL, counter INTEGER NOT NULL, backup_eligible INTEGER,
 PRIMARY KEY(session_id,owner_id,credential_id));
CREATE TABLE IF NOT EXISTS approval_requests (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, requester_id TEXT NOT NULL,
 target_id TEXT NOT NULL, canonical BLOB NOT NULL, digest TEXT NOT NULL,
 subject TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 state TEXT NOT NULL, receipt_hash TEXT, receipt_expires INTEGER,
 credential_id TEXT, fingerprint TEXT, method TEXT, credential_generation INTEGER,
 kind TEXT NOT NULL DEFAULT 'operation');
CREATE INDEX IF NOT EXISTS approval_requests_target ON approval_requests(session_id,target_id);
CREATE INDEX IF NOT EXISTS approval_requests_requester ON approval_requests(session_id,requester_id);
CREATE TABLE IF NOT EXISTS approval_challenges (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 purpose TEXT NOT NULL, request_id TEXT, credential_id TEXT, method TEXT NOT NULL,
 origin TEXT, canonical BLOB NOT NULL, value BLOB NOT NULL, expires_at INTEGER NOT NULL,
 generation INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS approval_challenges_owner ON approval_challenges(session_id,owner_id);
CREATE TABLE IF NOT EXISTS approval_rate (
 session_id TEXT NOT NULL, owner_id TEXT NOT NULL, started INTEGER NOT NULL,
 hits INTEGER NOT NULL, PRIMARY KEY(session_id,owner_id));
CREATE TABLE IF NOT EXISTS approval_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 kind TEXT NOT NULL, at INTEGER NOT NULL, request_id TEXT NOT NULL,
 metadata TEXT NOT NULL);
"""


class Rejected(ValueError):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


def _id(prefix):
    return prefix + secrets.token_urlsafe(24)


def _text(value, maximum=200):
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise Rejected("invalid_field", 400)
    return value


class ApprovalStore:
    def __init__(self, store, session_id: str, *, clock=time.time):
        self.store, self.session_id, self.clock = store, _text(session_id), clock
        with store._lock:
            store._db.executescript(SCHEMA)
            with store._db:
                columns = {r[1] for r in store._db.execute("PRAGMA table_info(approval_requests)")}
                if "credential_generation" not in columns:
                    # Existing draft receipts with unknown generation cannot
                    # authorize dispatch under the new key lifecycle.
                    store._db.execute("ALTER TABLE approval_requests ADD COLUMN credential_generation INTEGER")
                if "kind" not in columns:
                    store._db.execute("ALTER TABLE approval_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'operation'")
                store._db.execute("INSERT OR IGNORE INTO approval_session VALUES(1,?)", (session_id,))
                if store._db.execute("SELECT id FROM approval_session WHERE singleton=1").fetchone()[0] != session_id:
                    raise Rejected("wrong_session", 403)

    def _now(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 253402300000:
            raise Rejected("invalid_clock", 503)
        return int(value)

    def _auth(self, db, token):
        if type(token) is not str or not token or len(token) > 4096:
            raise Rejected("unauthorized", 401)
        row = db.execute("SELECT id FROM participants WHERE token_hash=? AND revoked=0", (token_hash(token),)).fetchone()
        if row is None or not row[0]:
            raise Rejected("unauthorized", 401)
        return row[0]

    def _audit(self, db, owner, kind, now, request_id="", **metadata):
        db.execute("INSERT INTO approval_audit(session_id,owner_id,kind,at,request_id,metadata) VALUES(?,?,?,?,?,?)",
                   (self.session_id, owner, kind, now, request_id, json.dumps(metadata, sort_keys=True)))
        db.execute("DELETE FROM approval_audit WHERE seq <= COALESCE((SELECT seq FROM approval_audit ORDER BY seq DESC LIMIT 1 OFFSET ?),-1)",
                   (MAX_AUDIT,))

    def _charge(self, db, owner, now):
        row = db.execute("SELECT started,hits FROM approval_rate WHERE session_id=? AND owner_id=?", (self.session_id, owner)).fetchone()
        if row is not None and now < row[0] + RATE_WINDOW:
            if row[1] >= RATE_LIMIT:
                raise Rejected("rate_limited", 429)
            db.execute("UPDATE approval_rate SET hits=hits+1 WHERE session_id=? AND owner_id=?", (self.session_id, owner))
        else:
            db.execute("INSERT OR REPLACE INTO approval_rate VALUES(?,?,?,1)", (self.session_id, owner, now))

    @contextmanager
    def _transaction(self, token, *, charge=True):
        # Quota commits separately so invalid proofs cannot roll it back.
        # The operation then reauthenticates in its own write transaction. A
        # revoke/rotation from another DB connection cannot race that check.
        with self.store._lock:
            db = self.store._db
            now = self._now()
            owner = None
            admitted = False
            try:
                if charge:
                    db.execute("BEGIN IMMEDIATE")
                    now = self._now()
                    owner = self._auth(db, token)
                    self._charge(db, owner, now)
                    db.commit()
                    admitted = True
                db.execute("BEGIN IMMEDIATE")
                # BEGIN can wait on another process's writer. Never authorize
                # against a timestamp sampled before that wait (or quota work).
                now = self._now()
                owner = self._auth(db, token)
                yield db, owner, now
                db.commit()
            except (Rejected, crypto.InvalidProof) as exc:
                db.rollback()
                failure = exc if isinstance(exc, Rejected) else Rejected("invalid_proof", 403)
                if owner is not None and admitted:
                    with db:
                        self._audit(db, owner, "refused", now, code=failure.code)
                raise failure from None
            except BaseException:
                db.rollback()
                raise

    def _prune(self, db, now):
        db.execute("DELETE FROM approval_challenges WHERE used=1 OR expires_at<=?", (now,))
        db.execute("DELETE FROM approval_requests WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM approval_rate WHERE started+?<=?", (RATE_WINDOW, now))

    def _active_person(self, db, person):
        if db.execute("SELECT 1 FROM participants WHERE id=? AND revoked=0", (person,)).fetchone() is None:
            raise Rejected("participant_unavailable", 403)

    def _generation(self, db, owner):
        row = db.execute("SELECT generation,bootstrapped FROM approval_generation WHERE session_id=? AND owner_id=?",
                         (self.session_id, owner)).fetchone()
        return tuple(row) if row else (0, 0)

    def _add_challenge(self, db, owner, *, purpose, request_id, credential_id, method,
                       origin, digest, subject, requester_id, generation, now):
        if db.execute("SELECT COUNT(*) FROM approval_challenges").fetchone()[0] >= MAX_CHALLENGES:
            raise Rejected("challenge_capacity", 429)
        issued = challenges.issue(purpose=purpose, session_id=self.session_id, target_id=owner,
            requester_id=requester_id, subject=subject, digest=digest, request_id=request_id,
            credential_id=credential_id, origin=origin, now=now)
        identity = _id("ac_")
        db.execute("INSERT INTO approval_challenges(id,session_id,owner_id,purpose,request_id,credential_id,method,origin,canonical,value,expires_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (identity, self.session_id, owner, purpose, request_id, credential_id, method,
                    origin, issued.canonical, issued.value, issued.expires_at, generation))
        # Explicit columns below keep wire names separate from internal schema.
        return {"api_version": 1, "challenge_id": identity, "challenge": crypto.encode(issued.value),
                "expires_at": issued.expires_at, "method": method, "credential_id": credential_id,
                "request_id": request_id, "digest": digest,
                **({"origin": origin, "rp_id": "localhost"} if method == "passkey" else {})}

    def _challenge(self, db, owner, identity, purpose, now):
        row = db.execute("SELECT * FROM approval_challenges WHERE id=? AND session_id=? AND owner_id=?",
                         (_text(identity), self.session_id, owner)).fetchone()
        if row is None:
            raise Rejected("unknown_challenge", 404)
        if row["purpose"] != purpose or row["used"]:
            raise Rejected("challenge_unavailable", 409)
        if now >= row["expires_at"]:
            raise Rejected("challenge_expired", 410)
        if row["generation"] != self._generation(db, owner)[0]:
            raise Rejected("credential_changed", 409)
        return row

    @staticmethod
    def _method(method, origin):
        if method not in ("hello", "passkey"):
            raise Rejected("unsupported_method", 400)
        if method == "passkey":
            crypto.validate_origin(origin)
        elif origin is not None:
            raise Rejected("unexpected_origin", 400)

    @staticmethod
    def _credential_id(method, identity):
        if method == "hello":
            if type(identity) is not str or _HELLO_NAME.fullmatch(identity) is None:
                raise Rejected("invalid_credential_id", 400)
        else:
            crypto.decode(identity, limit=1024)

    @staticmethod
    def _verify(row, spki, algorithm, proof):
        crypto.load_key(spki, algorithm=algorithm, method=row["method"])
        if row["method"] == "hello":
            crypto.verify_hello(spki=spki, challenge=bytes(row["value"]), proof=proof)
            return None
        return crypto.verify_passkey(spki=spki, algorithm=algorithm, challenge=bytes(row["value"]),
                                     origin=row["origin"], proof=proof)

    def begin_enrol(self, token, *, method, origin=None):
        with self._transaction(token) as (db, owner, now):
            self._method(method, origin)
            self._prune(db, now)
            generation, bootstrapped = self._generation(db, owner)
            if bootstrapped:
                raise Rejected("signed_key_change_required", 409)
            if db.execute("SELECT 1 FROM approval_challenges WHERE session_id=? AND owner_id=? AND purpose='enrol'",
                          (self.session_id, owner)).fetchone():
                raise Rejected("enrollment_in_progress", 409)
            generation += 1
            db.execute("INSERT OR REPLACE INTO approval_generation VALUES(?,?,?,0)", (self.session_id, owner, generation))
            digest = challenges.review({"purpose": "enrol", "method": method, "origin": origin,
                                        "session_id": self.session_id, "target_id": owner,
                                        "trust": "tofu-unverified"}).digest
            result = self._add_challenge(db, owner, purpose="enrol", request_id=None, credential_id=None,
                method=method, origin=origin, digest=digest, subject="First credential enrollment",
                requester_id=owner, generation=generation, now=now)
            self._audit(db, owner, "enrollment_started", now, method=method)
            return {**result, "bootstrap": "tofu-unverified", "algorithms": [-257] if method == "hello" else [-7, -257]}

    def finish_enrol(self, token, *, challenge_id, credential_id, algorithm, public_key_spki, proof):
        with self._transaction(token) as (db, owner, now):
            row = self._challenge(db, owner, challenge_id, "enrol", now)
            self._credential_id(row["method"], credential_id)
            if self._generation(db, owner)[1]:
                raise Rejected("signed_key_change_required", 409)
            assertion = self._verify(row, public_key_spki, algorithm, proof)
            now = self._now()
            if now >= row["expires_at"]:
                raise Rejected("challenge_expired", 410)
            fingerprint = hashlib.sha256(crypto.decode(public_key_spki, limit=crypto.MAX_SPKI)).hexdigest()
            db.execute("INSERT INTO approval_keys VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                (self.session_id, owner, credential_id, row["method"], algorithm, public_key_spki,
                 fingerprint, now, now + KEY_COOLDOWN, assertion.counter if assertion else 0,
                 int(assertion.backup_eligible) if assertion else None))
            db.execute("UPDATE approval_generation SET bootstrapped=1 WHERE session_id=? AND owner_id=?", (self.session_id, owner))
            db.execute("UPDATE approval_challenges SET used=1 WHERE id=?", (challenge_id,))
            self._audit(db, owner, "tofu_enrolled", now, fingerprint=fingerprint, method=row["method"],
                        credential_trust="tofu-unverified", usable_at=now + KEY_COOLDOWN)
            return self._key_metadata(db, owner, credential_id)

    def _key_metadata(self, db, owner, identity):
        row = self._key(db, owner, identity)
        return {k: row[k] for k in ("credential_id", "method", "algorithm", "fingerprint", "created_at", "usable_at", "state")} | {
            "trust": "tofu-unverified"}

    def _key(self, db, owner, identity, *, ready_at=None):
        row = db.execute("SELECT * FROM approval_keys WHERE session_id=? AND owner_id=? AND credential_id=?",
                         (self.session_id, owner, _text(identity, 1366))).fetchone()
        if row is None:
            raise Rejected("unknown_credential", 404)
        if ready_at is not None and (row["state"] != "active" or ready_at < row["usable_at"]):
            raise Rejected("credential_not_ready", 409)
        return row

    def keys(self, token):
        with self._transaction(token, charge=False) as (db, owner, now):
            identities = db.execute("SELECT credential_id FROM approval_keys WHERE session_id=? AND owner_id=? ORDER BY created_at",
                                    (self.session_id, owner)).fetchall()
            return [self._key_metadata(db, owner, row[0]) for row in identities]

    def create_request(self, token, *, target_id, operation):
        """Internal adoption boundary; the service must authorize operation type."""
        with self._transaction(token) as (db, requester, now):
            _text(target_id)
            self._active_person(db, target_id)
            if type(operation) is not dict or set(operation) != {"subject", "destination", "authority", "payload"}:
                raise Rejected("invalid_operation", 400)
            for field in ("subject", "authority"):
                _text(operation[field])
            _text(operation["destination"], 2048)
            review = challenges.review({**operation, "api_version": 1, "session_id": self.session_id,
                                        "requester_id": requester, "target_id": target_id})
            return self._insert_request(db, requester, target_id, review, operation["subject"], now)

    def _insert_request(self, db, requester, target, review, subject, now, *, expires=None, kind="operation"):
        self._prune(db, now)
        if (db.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] >= MAX_REQUESTS
                or db.execute("SELECT COUNT(*) FROM approval_requests WHERE requester_id=? OR target_id=?",
                              (requester, target)).fetchone()[0] >= MAX_OWNER_REQUESTS):
            raise Rejected("request_capacity", 429)
        expires = min(expires, now + REQUEST_SECONDS) if expires is not None else now + REQUEST_SECONDS
        identity = _id("ar_")
        db.execute("INSERT INTO approval_requests(id,session_id,requester_id,target_id,canonical,digest,subject,created_at,expires_at,state,kind) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",
                   (identity, self.session_id, requester, target, review.canonical, review.digest,
                    subject, now, expires, kind))
        self._audit(db, requester, "requested", now, identity, digest=review.digest, target_id=target)
        return {"api_version": 1, "request_id": identity, "digest": review.digest, "expires_at": expires}

    def _request(self, db, owner, identity, now):
        row = db.execute("SELECT * FROM approval_requests WHERE id=? AND session_id=? AND target_id=?",
                         (_text(identity), self.session_id, owner)).fetchone()
        if row is None:
            raise Rejected("unknown_request", 404)
        if now >= row["expires_at"]:
            raise Rejected("request_expired", 410)
        self._active_person(db, row["requester_id"])
        return row

    def review(self, token, request_id):
        with self._transaction(token, charge=False) as (db, owner, now):
            row = self._request(db, owner, request_id, now)
            return {"api_version": 1, "request_id": request_id, "review": json.loads(row["canonical"]),
                    "canonical_review": crypto.encode(row["canonical"]), "digest": row["digest"],
                    "state": row["state"], "required_strength": "strong"}

    def challenge(self, token, request_id, *, credential_id, origin=None):
        with self._transaction(token) as (db, owner, now):
            request = self._request(db, owner, request_id, now)
            if request["state"] != "pending":
                raise Rejected("request_unavailable", 409)
            key = self._key(db, owner, credential_id, ready_at=now)
            self._method(key["method"], origin)
            self._prune(db, now)
            # One active challenge per request. A refresh invalidates the old
            # challenge instead of allowing a queue of outstanding approvals.
            db.execute("DELETE FROM approval_challenges WHERE request_id=?", (request_id,))
            return self._add_challenge(db, owner, purpose="approve", request_id=request_id,
                credential_id=credential_id, method=key["method"], origin=origin, digest=request["digest"],
                subject=request["subject"], requester_id=request["requester_id"],
                generation=self._generation(db, owner)[0], now=now)

    def approve(self, token, request_id, *, challenge_id, credential_id, proof):
        with self._transaction(token) as (db, owner, now):
            request = self._request(db, owner, request_id, now)
            if request["state"] != "pending":
                raise Rejected("request_unavailable", 409)
            row = self._challenge(db, owner, challenge_id, "approve", now)
            if row["request_id"] != request_id or row["credential_id"] != credential_id:
                raise Rejected("challenge_binding", 403)
            key = self._key(db, owner, credential_id, ready_at=now)
            assertion = self._verify(row, key["spki"], key["algorithm"], proof)
            now = self._now()
            if now >= min(row["expires_at"], request["expires_at"]):
                raise Rejected("challenge_expired", 410)
            if assertion:
                crypto.validate_counter(assertion, previous=key["counter"], backup_eligible=bool(key["backup_eligible"]))
                db.execute("UPDATE approval_keys SET counter=? WHERE session_id=? AND owner_id=? AND credential_id=?",
                           (assertion.counter, self.session_id, owner, credential_id))
            receipt = secrets.token_urlsafe(32)
            expires = min(now + RECEIPT_SECONDS, request["expires_at"])
            db.execute("UPDATE approval_challenges SET used=1 WHERE id=?", (challenge_id,))
            db.execute("UPDATE approval_requests SET state='approved',receipt_hash=?,receipt_expires=?,credential_id=?,fingerprint=?,method=?,credential_generation=? WHERE id=?",
                       (token_hash(receipt), expires, credential_id, key["fingerprint"], key["method"],
                        self._generation(db, owner)[0], request_id))
            self._audit(db, owner, "approved", now, request_id, digest=request["digest"],
                        fingerprint=key["fingerprint"], method=key["method"], strength="strong", credential_trust="tofu-unverified")
            return {"api_version": 1, "request_id": request_id, "state": "approved", "receipt": receipt,
                    "expires_at": expires, "credential_trust": "tofu-unverified"}

    def decline(self, token, request_id):
        with self._transaction(token) as (db, owner, now):
            row = self._request(db, owner, request_id, now)
            if row["state"] not in ("pending", "approved"):
                raise Rejected("request_unavailable", 409)
            db.execute("UPDATE approval_requests SET state='declined',receipt_hash=NULL WHERE id=?", (request_id,))
            db.execute("DELETE FROM approval_challenges WHERE request_id=?", (request_id,))
            self._audit(db, owner, "declined", now, request_id, verified_human=False)
            return {"api_version": 1, "request_id": request_id, "state": "declined"}

    def consume(self, token, request_id, *, receipt, digest):
        """Claim dispatch exactly once; never automatically retry after a crash.

        The adopting executor must use request_id as its idempotency key and
        execute ONLY the returned canonical operation. A crash after this
        transaction is an uncertain outcome, not permission for a second claim.
        """
        with self._transaction(token) as (db, owner, now):
            row, key = self._receipt(db, owner, request_id, receipt, digest, now)
            if row["kind"] != "operation":
                raise Rejected("wrong_dispatch", 403)
            db.execute("UPDATE approval_requests SET state='consumed',receipt_hash=NULL WHERE id=?", (request_id,))
            self._audit(db, owner, "dispatch_claimed", now, request_id, digest=row["digest"],
                        fingerprint=key["fingerprint"], method=key["method"], strength="strong", credential_trust="tofu-unverified")
            return {"request_id": request_id, "canonical_review": bytes(row["canonical"]),
                    "digest": row["digest"], "strength": "strong", "credential_trust": "tofu-unverified"}

    def _receipt(self, db, owner, request_id, receipt, digest, now):
        row = self._request(db, owner, request_id, now)
        if row["state"] != "approved":
            raise Rejected("request_unavailable", 409)
        _text(receipt, 128)
        if digest != row["digest"] or not secrets.compare_digest(token_hash(receipt), row["receipt_hash"]):
            raise Rejected("receipt_binding", 403)
        if now >= row["receipt_expires"]:
            raise Rejected("receipt_expired", 410)
        key = self._key(db, owner, row["credential_id"], ready_at=now)
        if (key["fingerprint"] != row["fingerprint"]
                or row["credential_generation"] != self._generation(db, owner)[0]):
            raise Rejected("credential_changed", 409)
        return row, key

    def report_key(self, token, credential_id, *, observed_spki=None):
        """A client reports missing/changed local key material; never replace it.

        The bearer can request a refusal/freeze but cannot claim a human gesture.
        No private key is transmitted. An unchanged public key is a no-op.
        """
        with self._transaction(token) as (db, owner, now):
            key = self._key(db, owner, credential_id)
            if key["state"] == "retired":
                raise Rejected("credential_retired", 409)
            observed = None
            if observed_spki is not None:
                # An observation can only freeze authority, never create it.
                # Compare bounded public-key bytes independently of the old
                # algorithm: a replacement with a different algorithm is
                # precisely a change, not a reason to leave the old key active.
                observed = hashlib.sha256(crypto.decode(observed_spki, limit=crypto.MAX_SPKI)).hexdigest()
                if secrets.compare_digest(observed, key["fingerprint"]):
                    return self._key_metadata(db, owner, credential_id)
            db.execute("UPDATE approval_keys SET state='frozen' WHERE session_id=? AND owner_id=? AND credential_id=?",
                       (self.session_id, owner, credential_id))
            db.execute("UPDATE approval_generation SET generation=generation+1 WHERE session_id=? AND owner_id=?", (self.session_id, owner))
            self._audit(db, owner, "key_reported_missing" if observed is None else "key_reported_changed", now,
                        fingerprint=key["fingerprint"], observed_fingerprint=observed, verified_human=False)
            return self._key_metadata(db, owner, credential_id)

    def report_missing(self, token, credential_id):
        return self.report_key(token, credential_id)

    def audit(self, token):
        with self._transaction(token, charge=False) as (db, owner, now):
            return [dict(row) for row in db.execute("SELECT seq,kind,at,request_id,metadata FROM approval_audit WHERE session_id=? AND owner_id=? ORDER BY seq DESC LIMIT ?",
                                                  (self.session_id, owner, MAX_AUDIT)).fetchall()]
