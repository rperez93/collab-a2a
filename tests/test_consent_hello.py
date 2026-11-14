"""Windows Hello as the proof of a human approval — tested with fakes only.

No test here raises a real Hello prompt: that is the user's, at the final live
check. What is tested is everything around the gesture — that a signature
Hello made verifies and a tampered one does not, that nothing a caller supplies
becomes PowerShell code, that every call is bounded however the far side
behaves, and that the approval window signs only after Approve.

The signature vector is REAL: the user made the gesture on 2026-11-14, signing
«collab r22b probe challenge 1» with a TPM-backed Hello key, and this is the
public key that key reported (.claude/r22b-strong-approval-design.md).
"""

from __future__ import annotations

import base64
import json
import os
import resource
import sys
import time

import pytest

from collab import consent_hello as hello

REAL_SPKI = base64.b64decode(
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAy9/WYAaAhr3PfMyHhtx+QiQmaQg68A0HasO5"
    "KdmSEibW967d4kUU5RalSlMG8A7X0qsQrmyLE+5gxnat/Mzr2fq3m+VsZ307CkXeXtlBMgXlbqzhtR7h"
    "dEDuaicEdnNutRooakFHjoiIpi3eRIgK4qYrjLMeuq5mpMSPoJf988j06E++oss+P2FgjcBJDK0OEH5g"
    "nZp5QJAsrgL2EJ948fANDWHVyG1GWGIR7rl2rG7N9JTxV+z1OQwpnBiD2fwPaPxIaSahXmfepSrM6Aj/"
    "XpLJrWdFkRYPygBCag2MwS21gD0SgL7p+Vbvz8ikqfHjSeEPX1RI2wxWqfgsM4B8wQIDAQAB")
REAL_MESSAGE = b"collab r22b probe challenge 1"
REAL_SIGNATURE = base64.b64decode(
    "J1Eeemt/VRFmUsL/rvbw1+qpvxBL9GAIr6Dj4qE++baQckwzmdUP6x4s3i7oOG2T04RWxjjdGLruMfZg"
    "6oUaWdndlYntczQVJ8vXnU3PNoN1FghPqW5ZvnovEPxnpBkejH4n8W1E6Dtsa8MfN7/1A0JCUSHXKrAX"
    "Wj3hNlJ3On8MVaVHjcm3Mr3pfuHaV3CtdBCRG5EgpYD4z5OtMJaB9yuCoiLEeuSB+2qsjFl4OyBrtsFd"
    "PGa5saHPn2RNL1uU2tmZ3NpzS1SogKMrIeEqY+uNlLXZmqHwfXUOuLpYEigRRXausSxeX/Fubogwou2x"
    "r0QAjX8PgqbYL1NgmrQHtg==")


# --- a fake powershell.exe ---------------------------------------------------------

FAKE = r'''#!{python}
import base64, json, os, sys, time, subprocess
plan = json.loads(open(os.environ["FAKE_HELLO_PLAN"]).read())
log = os.environ["FAKE_HELLO_LOG"]
line = sys.stdin.readline()
req = json.loads(base64.b64decode(line))
with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "req": req}}) + "\n")
how = plan.get(req["op"], {{"status": "Success"}})
if how == "silent":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.environ["FAKE_HELLO_LOG"] + ".child", "w").write(str(child.pid))
    time.sleep(60)
elif how == "flood":
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
elif how == "garbage":
    print("this is not json")
elif isinstance(how, dict) and how.get("bom"):
    sys.stdout.buffer.write(b"\xef\xbb\xbfWindows PowerShell banner\r\n"
                            + json.dumps(how["answer"]).encode() + b"\r\n")
else:
    print(json.dumps(how))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Install the fake and return a function that sets its plan."""
    exe = tmp_path / "powershell.exe"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(hello, "_powershell", lambda: str(exe))
    monkeypatch.setenv("FAKE_HELLO_PLAN", str(plan))
    monkeypatch.setenv("FAKE_HELLO_LOG", str(log))

    class Fake:
        def plan(self, **ops):
            plan.write_text(json.dumps(ops))

        def calls(self):
            if not log.exists():
                return []
            return [json.loads(x) for x in log.read_text().splitlines()]

        child_file = tmp_path / "calls.jsonl.child"

    return Fake()


# --- the signature is the proof -------------------------------------------------------

def test_a_signature_hello_made_verifies_and_nothing_else_does():
    """The real vector: the user's gesture, the key Hello reported. A changed
    message, a changed signature and a key that is not RSA are all refused."""
    assert hello.verify(REAL_SPKI, REAL_MESSAGE, REAL_SIGNATURE)
    assert not hello.verify(REAL_SPKI, b"collab r22b probe challenge 2", REAL_SIGNATURE)
    tampered = bytearray(REAL_SIGNATURE)
    tampered[10] ^= 1
    assert not hello.verify(REAL_SPKI, REAL_MESSAGE, bytes(tampered))
    assert not hello.verify(b"not a key", REAL_MESSAGE, REAL_SIGNATURE)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_spki = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert not hello.verify(ec_spki, REAL_MESSAGE, REAL_SIGNATURE)


# --- the four operations, against the fake ----------------------------------------------

def test_availability_is_asked_without_a_prompt_and_never_raises(fake, monkeypatch):
    fake.plan(supported={"status": "Success", "supported": True})
    assert hello.available() is True
    fake.plan(supported={"status": "Success", "supported": False})
    assert hello.available() is False
    fake.plan(supported="garbage")
    assert hello.available() is False
    monkeypatch.setattr(hello, "_powershell", lambda: None)
    assert hello.available() is False, "no powershell.exe: not available, not an error"


def test_enrolment_returns_what_the_hub_registers(fake):
    spki = base64.b64encode(REAL_SPKI).decode()
    fake.plan(create={"status": "Success", "spki": spki})
    record = hello.enrol("collab-alice")
    assert record["cred_id"] == "collab-alice" and record["method"] == "hello"
    assert record["alg"] == "RS256" and base64.b64decode(record["spki_der_b64"]) == REAL_SPKI
    assert fake.calls()[-1]["req"] == {"op": "create", "name": "collab-alice"}


@pytest.mark.parametrize("status, error", [
    ("UserCanceled", hello.Refused), ("UserPrefersPassword", hello.Refused),
    ("CredentialAlreadyExists", hello.AlreadyEnrolled),
    ("SecurityDeviceLocked", hello.Unavailable), ("UnknownError", hello.HelloError),
])
def test_every_status_hello_can_give_has_its_meaning(fake, status, error):
    """A cancel is a refusal, an existing key is never replaced, a locked TPM
    is unavailability — and anything else is an error naming the status."""
    fake.plan(create={"status": status}, sign={"status": status})
    with pytest.raises(error) as raised:
        hello.enrol("collab-alice")
    assert raised.value.status == status
    with pytest.raises(error):
        hello.sign("collab-alice", b"challenge")


def test_signing_sends_exactly_the_challenge_and_returns_the_signature(fake):
    fake.plan(sign={"status": "Success",
                    "signature": base64.b64encode(REAL_SIGNATURE).decode()})
    assert hello.sign("collab-alice", REAL_MESSAGE) == REAL_SIGNATURE
    sent = fake.calls()[-1]["req"]
    assert base64.b64decode(sent["challenge"]) == REAL_MESSAGE
    fake.plan(sign={"status": "NotFound"})
    with pytest.raises(hello.NotEnrolled):
        hello.sign("collab-alice", REAL_MESSAGE)


def test_nothing_the_caller_supplies_reaches_powershell_as_code(fake):
    """The script is fixed and passed encoded; the name and the challenge go
    on stdin as base64 — never in the command line. A name that is not a
    plain key name is refused before any process starts."""
    with pytest.raises(ValueError):
        hello.sign("x'; Remove-Item C:\\ -Recurse; '", b"c")
    with pytest.raises(ValueError):
        hello.enrol("a\nb")
    with pytest.raises(ValueError):
        hello.sign("ok", b"")
    assert fake.calls() == [], "refused before any process"
    fake.plan(sign={"status": "Success", "signature": base64.b64encode(b"s").decode()})
    hello.sign("collab-alice", b"SECRET-CHALLENGE")
    argv = " ".join(fake.calls()[-1]["argv"])
    assert "collab-alice" not in argv and "SECRET" not in argv
    assert "-EncodedCommand" in argv and "-NonInteractive" in argv


def test_a_banner_a_bom_and_crlf_do_not_hide_the_answer(fake):
    fake.plan(supported={"bom": True, "answer": {"status": "Success", "supported": True}})
    assert hello.available() is True


def test_an_answer_that_is_not_one_is_refused(fake):
    fake.plan(pubkey="garbage")
    with pytest.raises(hello.HelloError) as raised:
        hello.public_key("collab-alice")
    assert raised.value.status == "BadAnswer"
    fake.plan(pubkey={"status": "Success", "spki": "not base64!!"})
    with pytest.raises(hello.HelloError):
        hello.public_key("collab-alice")


# --- bounded, whatever the far side does ----------------------------------------------

def test_a_silent_call_ends_at_its_deadline_with_its_whole_group(fake, monkeypatch):
    """A prompt nobody answers, or a PowerShell that hangs: the deadline is
    enforced while nothing arrives, and the call's own process group — what it
    started included — is ended, not just the handle collab holds."""
    monkeypatch.setattr(hello, "QUICK", 1.5)
    fake.plan(pubkey="silent")
    began = time.monotonic()
    with pytest.raises(hello.HelloError) as raised:
        hello.public_key("collab-alice")
    took = time.monotonic() - began
    assert raised.value.status == "Timeout"
    assert took < 4, f"{took:.1f} s"
    child = int(fake.child_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_a_flood_is_cut_at_the_output_cap_cheaply(fake):
    """A far side printing without end: cut at `OUTPUT_CAP`, ended, and
    measured — the caller's CPU for the whole exchange and its memory."""
    fake.plan(pubkey="flood")
    before = resource.getrusage(resource.RUSAGE_SELF)
    began = time.monotonic()
    with pytest.raises(hello.HelloError) as raised:
        hello.public_key("collab-alice")
    after = resource.getrusage(resource.RUSAGE_SELF)
    assert raised.value.status == "OutputTooLarge"
    assert time.monotonic() - began < 5
    cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    assert cpu < 0.5, f"{cpu:.3f} s of CPU"


# --- the approval window ----------------------------------------------------------------

def _request(**extra):
    base = {"request_id": "q_1", "requester_id": "p_bob", "requester_name": "bob",
            "subject": "remote prompt", "destination": "alice's agent",
            "authority": "run one prompt", "digest_hex": "ab" * 32,
            "review_text": "please run: make test", "cred_id": "collab-alice",
            "challenge_b64": base64.b64encode(REAL_MESSAGE).decode()}
    base.update(extra)
    return base


def test_approve_signs_the_challenge_and_only_approve_does(fake):
    """The click is the front door; the gesture is the lock. Approve leads to
    one signature over exactly the challenge; Decline and «use a passkey» to
    none."""
    sig = base64.b64encode(REAL_SIGNATURE).decode()
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "Success", "signature": sig})
    result = hello.open(_request())
    assert result == {"choice": "approve", "method": "hello", "cred_id": "collab-alice",
                      "signature_b64": sig}
    signed = [c["req"] for c in fake.calls() if c["req"]["op"] == "sign"]
    assert len(signed) == 1 and base64.b64decode(signed[0]["challenge"]) == REAL_MESSAGE
    for choice, expected in (("decline", "decline"), ("passkey", "passkey"),
                             ("anything", "decline")):
        fake.plan(window={"status": "Success", "choice": choice})
        before = len([c for c in fake.calls() if c["req"]["op"] == "sign"])
        assert hello.open(_request())["choice"] == expected
        assert len([c for c in fake.calls() if c["req"]["op"] == "sign"]) == before


def test_a_cancelled_gesture_after_approve_is_a_decline(fake):
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "UserCanceled"})
    assert hello.open(_request()) == {"choice": "decline", "reason": "Refused"}


def test_a_window_that_breaks_or_hangs_approves_nothing(fake, monkeypatch):
    monkeypatch.setattr(hello, "HUMAN", 1.5)
    fake.plan(window="silent")
    assert hello.open(_request())["choice"] == "decline"
    fake.plan(window="garbage")
    assert hello.open(_request())["choice"] == "decline"
    monkeypatch.setattr(hello, "_powershell", lambda: None)
    assert hello.open(_request()) == {"choice": "unavailable", "reason": "NoPowerShell"}


def test_no_key_enrolled_is_unavailable_so_the_dispatcher_can_offer_a_passkey(fake):
    fake.plan(window={"status": "Success", "choice": "approve"}, sign={"status": "NotFound"})
    assert hello.open(_request()) == {"choice": "unavailable", "reason": "NotEnrolled"}


def test_the_window_shows_the_review_as_untrusted_text_with_a_bounded_summary(fake):
    """The requester chose every one of these strings. The summary lines carry
    no control characters or newlines (a second «summary» line drawn by the
    requester would be the redraw this window must not invite), the review is
    marked untrusted and bounded, and it travels on stdin, not the command
    line."""
    fake.plan(window={"status": "Success", "choice": "decline"})
    hello.open(_request(requester_name="bob\nAPPROVED BY ADMIN\x1b[2J",
                        review_text="x" * (hello.REVIEW_MAX + 500)))
    call = fake.calls()[-1]
    sent = call["req"]
    assert "\n" not in sent["who"] and "\x1b" not in sent["who"]
    assert sent["review"].startswith("UNTRUSTED")
    assert len(sent["review"]) < hello.REVIEW_MAX + 200
    assert "xxxx" not in " ".join(call["argv"])
    assert "digest abababababababab" in sent["summary"]
