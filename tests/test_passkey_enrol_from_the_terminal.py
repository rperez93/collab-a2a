"""`collab consent enrol` off Windows: the terminal opens the local passkey page
and waits for the page's own outcome.

Before this, the command refused with «passkey enrolment needs the passkey
page, which is not part of this build» while passkey review shipped — so a
strong request to a macOS or Linux participant could never be approved (gate
review of #181). These drive the REAL local page over loopback, with a
synthetic hub behind the bounded transport and a scripted «browser».
"""
import json
import threading
from urllib.parse import urlsplit

import httpx
import pytest

from collab.client import passkey_enrol
from collab.config import SessionProfile
from test_passkey_enrol import SyntheticHub, encoded


def _profile(tmp_path):
    return SessionProfile(session_id="s_enrol", name="Original label", host_name="synthetic-host",
                          participant_id="p_original", token="synthetic-bearer",
                          url="http://synthetic-hub", home=str(tmp_path / "profile"))


def _browser(url, *, finish=True):
    """What the page's script does: read status, take options, hand back a proof."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    headers = {"X-Collab-Approval-Token": parts.fragment, "Origin": origin}
    with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
        assert client.get(parts.path + "/status", headers=headers).status_code == 200
        issued = client.post(parts.path + "/options", headers=headers, json={}).json()
        if not finish:
            return
        client_data = (b'{"type":"webauthn.get", "origin":' + json.dumps(origin).encode()
                       + b',"challenge":' + json.dumps(issued["publicKey"]["challenge"]).encode() + b"}")
        identity = encoded(b"new-credential")
        body = {"challenge_id": issued["challenge_id"],
                "created_key": {"credential_id": identity, "algorithm": -7,
                                "public_key_spki": encoded(b"synthetic-not-a-real-SPKI")},
                "credential": {"id": identity, "rawId": identity, "type": "public-key",
                               "response": {"clientDataJSON": encoded(client_data),
                                            "authenticatorData": encoded(b"a" * 37),
                                            "signature": encoded(b"synthetic-signature")}}}
        assert client.post(parts.path + "/finish", headers=headers, json=body).status_code == 200


def test_the_terminal_enrols_a_passkey_through_the_local_page(tmp_path, monkeypatch):
    _not_windows(monkeypatch)
    hub = SyntheticHub()
    opened = []

    def opener(url):
        opened.append(url)
        threading.Thread(target=_browser, args=(url,), daemon=True).start()
        return True

    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opener,
                                               transport=httpx.MockTransport(hub))
    assert outcome["status"] == "enrolled"
    assert outcome["credential"]["credential_id"] == encoded(b"new-credential")
    assert len(opened) == 1 and opened[0].startswith("http://localhost:")
    assert [path for _, path, _ in hub.calls].count("enrol/finish") == 1


def _not_windows(monkeypatch):
    from collab.client import approval
    monkeypatch.setattr(approval, "_native_hello_platform", lambda: False)


def test_a_page_that_cannot_open_enrols_nothing(tmp_path, monkeypatch):
    """A headless or SSH box: webbrowser returns False. A plain refusal, never
    a traceback, and nothing asked of the hub (gate review of #185)."""
    _not_windows(monkeypatch)
    hub = SyntheticHub()
    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=lambda url: False,
                                               transport=httpx.MockTransport(hub))
    assert outcome == {"status": "page-unopened", "credential": None}
    assert not [c for c in hub.calls if c[1] == "enrol/finish"]


@pytest.mark.parametrize("marker", ["WSL_DISTRO_NAME", "WSL_INTEROP"])
def test_windows_and_wsl_never_enrol_a_passkey(tmp_path, monkeypatch, marker):
    """A passkey enrolled on Windows/WSL could never approve there, and
    replacing it needs that unusable key: the runner refuses before any page."""
    monkeypatch.setenv(marker, "Ubuntu" if marker == "WSL_DISTRO_NAME" else "/run/WSL/1_interop")
    opened = []
    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opened.append,
                                               transport=httpx.MockTransport(SyntheticHub()))
    assert outcome == {"status": "native-hello-required", "credential": None} and not opened


def test_after_a_timeout_the_page_and_its_capability_are_gone(tmp_path, monkeypatch):
    """The gate's own check: once the terminal stops waiting, the local page no
    longer answers — neither the server nor the capability outlives it."""
    from urllib.parse import urlsplit
    _not_windows(monkeypatch)
    monkeypatch.setattr(passkey_enrol, "ENROL_WAIT_SECONDS", 1.5)
    seen = []

    def opener(url):
        seen.append(url)
        threading.Thread(target=_browser, args=(url,), kwargs={"finish": False}, daemon=True).start()
        return True
    out = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opener,
                                           transport=httpx.MockTransport(SyntheticHub()))
    assert out["status"] == "expired"
    p = urlsplit(seen[0])
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{p.scheme}://{p.netloc}{p.path}/status", timeout=2,
                  headers={"X-Collab-Approval-Token": p.fragment}, trust_env=False)


def test_a_browser_that_stops_before_the_proof_times_out_without_a_key(tmp_path, monkeypatch):
    _not_windows(monkeypatch)
    monkeypatch.setattr(passkey_enrol, "ENROL_WAIT_SECONDS", 2.0)
    hub = SyntheticHub()

    def opener(url):
        threading.Thread(target=_browser, args=(url,), kwargs={"finish": False}, daemon=True).start()
        return True

    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opener,
                                               transport=httpx.MockTransport(hub))
    assert outcome == {"status": "expired", "credential": None}
    assert not [c for c in hub.calls if c[1] == "enrol/finish"]


def test_a_lost_finish_answer_is_reported_as_uncertain(tmp_path, monkeypatch):
    """The hub may have committed the key when its answer was lost: the runner
    says «uncertain», never «expired» (gate re-review of #185, mutant n4)."""
    _not_windows(monkeypatch)
    monkeypatch.setattr(passkey_enrol, "ENROL_WAIT_SECONDS", 3.0)
    hub = SyntheticHub()
    hub.lose_finish = True

    def browser(url):
        try:
            _browser(url)
        except AssertionError:
            pass                      # the page reports the lost answer as not-OK

    def opener(url):
        threading.Thread(target=browser, args=(url,), daemon=True).start()
        return True
    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opener,
                                               transport=httpx.MockTransport(hub))
    assert outcome == {"status": "uncertain", "credential": None}
