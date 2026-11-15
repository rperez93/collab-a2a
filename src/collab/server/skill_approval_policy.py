"""Preserve explicit strong skill-request policy across message transports."""
import json
import re

from ..protocol import KIND_CHAT, KIND_SYSTEM, MessageRefused

MAX_RECORDS = 4096
MAX_LEGACY_SCAN = 500
MAX_SOURCE = 32768
REQUEST_ID = re.compile(r"sr_[0-9a-f]{12}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS strong_skill_sources (
 requester_id TEXT NOT NULL, recipient_id TEXT NOT NULL, request_id TEXT NOT NULL,
 source_seq INTEGER NOT NULL, PRIMARY KEY(requester_id,recipient_id,request_id));
"""


class SkillApprovalRefused(MessageRefused):
    status_code = 409


def register(db, requester, recipient, request_id, source_seq):
    # Keep the policy while the original source remains visible, even after
    # the approval TTL. A later weak message must not resurrect that source.
    if _previous_acceptance(db, requester, recipient, request_id):
        raise SkillApprovalRefused("request ID has prior weak acceptance; issue a fresh strong request ID")
    db.execute("DELETE FROM strong_skill_sources WHERE NOT EXISTS (SELECT 1 FROM events WHERE seq=source_seq)")
    known = db.execute("SELECT source_seq FROM strong_skill_sources WHERE requester_id=? AND recipient_id=? AND request_id=?",
                       (requester, recipient, request_id)).fetchone()
    if known is not None:
        db.execute("UPDATE strong_skill_sources SET source_seq=MAX(source_seq,?) WHERE requester_id=? AND recipient_id=? AND request_id=?",
                   (source_seq, requester, recipient, request_id))
        return  # Keep policy through retention of the newest reused source.
    if db.execute("SELECT COUNT(*) FROM strong_skill_sources").fetchone()[0] >= MAX_RECORDS:
        raise SkillApprovalRefused("strong skill request capacity reached")
    db.execute("INSERT INTO strong_skill_sources VALUES(?,?,?,?)", (requester, recipient, request_id, source_seq))


def _known(db, requester, recipient, request_id):
    if db.execute("SELECT 1 FROM strong_skill_sources s JOIN events e ON e.seq=s.source_seq WHERE s.requester_id=? AND s.recipient_id=? AND s.request_id=?",
                  (requester, recipient, request_id)).fetchone():
        return True
    # Compatibility with draft databases created before this index. The old
    # skill reader considers at most 500 visible events. Iterate bounded SQL
    # prefixes rather than materializing an entire legacy history in Python.
    rows = db.execute("SELECT seq,substr(payload,1,?) AS payload FROM events WHERE sender_id=? AND recipient_id=? AND kind=? ORDER BY seq DESC LIMIT ?",
                      (MAX_SOURCE + 1, requester, recipient, KIND_CHAT, MAX_LEGACY_SCAN))
    for row in rows:
        raw = row["payload"]
        if len(raw) > MAX_SOURCE:
            raise SkillApprovalRefused("source history requires strong review")
        try:
            source = json.loads(raw).get("body", {}).get("skill_request")
        except (ValueError, RecursionError, AttributeError):
            raise SkillApprovalRefused("source history requires strong review") from None
        if isinstance(source, dict) and source.get("id") == request_id and source.get("required_strength") == "strong":
            return True
    return False


def _previous_acceptance(db, requester, recipient, request_id):
    rows = db.execute("SELECT substr(payload,1,?) AS payload FROM events WHERE sender_id=? AND recipient_id=? ORDER BY seq DESC LIMIT ?",
                      (MAX_SOURCE + 1, recipient, requester, MAX_LEGACY_SCAN))
    for row in rows:
        raw = row["payload"]
        if len(raw) > MAX_SOURCE:
            raise SkillApprovalRefused("response history requires a fresh strong request")
        try:
            reply = json.loads(raw).get("body", {}).get("skill_response")
        except (ValueError, RecursionError, AttributeError):
            raise SkillApprovalRefused("response history requires a fresh strong request") from None
        if isinstance(reply, dict) and reply.get("id") == request_id and reply.get("state") in ("accepted", "done"):
            return True
    return False


def check(db, env):
    """Called inside append_once's transaction, after delivery-key dedup."""
    if env.kind == KIND_SYSTEM:
        return  # Only server code can publish this kind through either API.
    body = env.body or {}
    if "strong_skill_response" in body or body.get("event") == "skill-approval":
        raise SkillApprovalRefused("strong skill responses are server-owned")
    reply = body.get("skill_response")
    if not isinstance(reply, dict) or reply.get("state") not in ("accepted", "done"):
        return
    request_id = reply.get("id")
    if (not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None
            or not env.sender_id or not env.to_id):
        raise SkillApprovalRefused("skill responses require immutable participant identities")
    if _known(db, env.to_id, env.sender_id, request_id):
        if (reply["state"] == "done" and env.kind == KIND_CHAT and not env.room
                and _accepted_source(db, env.to_id, env.sender_id, request_id)):
            return  # An execution report after approval, never an approval.
        raise SkillApprovalRefused("this skill request requires the strong approval endpoint")


def _accepted_source(db, requester, recipient, request_id):
    # Store can run without the optional approval service. Missing durable
    # approval state is never permission to accept a completion as consent.
    if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('approval_skill_bindings','approval_requests')").fetchone()[0] != 2:
        return False
    # Compare the newest exact source, not just its reusable short ID. A
    # later request must not inherit a previous request's accepted state.
    accepted = db.execute("""SELECT s.source_seq FROM strong_skill_sources s
        JOIN approval_skill_bindings b ON b.source_seq=s.source_seq
            AND b.owner_id=s.recipient_id AND b.requester_id=s.requester_id
            AND b.source_id=s.request_id
        JOIN approval_requests r ON r.id=b.request_id AND r.session_id=b.session_id
        WHERE s.requester_id=? AND s.recipient_id=? AND s.request_id=?
          AND b.state='accepted' AND b.event_id IS NOT NULL AND r.state='consumed'
        LIMIT 1""", (requester, recipient, request_id)).fetchone()
    if accepted is None:
        return False
    # Old clients key completion by the short ID, not source sequence. Even
    # a genuine acceptance of the newest source cannot mark an earlier weak
    # request done. The pair's newest 500 chats include every such source the
    # legacy reader could see in its newest 500 total events.
    seen = []
    rows = db.execute("SELECT seq,substr(payload,1,?) AS payload FROM events WHERE sender_id=? AND recipient_id=? AND kind=? ORDER BY seq DESC LIMIT ?",
                      (MAX_SOURCE + 1, requester, recipient, KIND_CHAT, MAX_LEGACY_SCAN))
    for row in rows:
        raw = row["payload"]
        if len(raw) > MAX_SOURCE:
            return False
        try:
            ask = json.loads(raw).get("body", {}).get("skill_request")
        except (ValueError, RecursionError, AttributeError):
            return False
        if isinstance(ask, dict) and ask.get("id") == request_id:
            seen.append(row["seq"])
            if len(seen) > 1:
                return False
    return seen == [accepted["source_seq"]]


def record(db, env):
    if env.kind != KIND_CHAT:
        return
    ask = (env.body or {}).get("skill_request")
    if not isinstance(ask, dict) or ask.get("required_strength") != "strong":
        return
    identity = ask.get("id")
    if (not isinstance(identity, str) or REQUEST_ID.fullmatch(identity) is None
            or not env.sender_id or not env.to_id):
        raise SkillApprovalRefused("strong skill requests require immutable private routing and a bounded ID")
    register(db, env.sender_id, env.to_id, identity, env.seq)
