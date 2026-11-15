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


def test_the_terminal_enrols_a_passkey_through_the_local_page(tmp_path):
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


def test_a_page_that_cannot_open_enrols_nothing(tmp_path):
    hub = SyntheticHub()
    try:
        passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=lambda url: False,
                                         transport=httpx.MockTransport(hub))
    except RuntimeError as exc:
        assert "could not be opened" in str(exc)
    else:
        raise AssertionError("an unopened page must not report an enrolment")
    assert not [c for c in hub.calls if c[1] == "enrol/finish"]


def test_a_browser_that_stops_before_the_proof_times_out_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(passkey_enrol, "ENROL_WAIT_SECONDS", 2.0)
    hub = SyntheticHub()

    def opener(url):
        threading.Thread(target=_browser, args=(url,), kwargs={"finish": False}, daemon=True).start()
        return True

    outcome = passkey_enrol.enrol_with_passkey(_profile(tmp_path), opener=opener,
                                               transport=httpx.MockTransport(hub))
    assert outcome == {"status": "expired", "credential": None}
    assert not [c for c in hub.calls if c[1] == "enrol/finish"]
