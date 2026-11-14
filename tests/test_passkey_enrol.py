"""Synthetic wire contracts exercise enrolment; no fixture is a human ceremony."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from collab.client import passkey
from collab.client.passkey_adapter import ApprovalTransport
from collab.client.passkey_enrol import EnrolService
from collab.config import SessionProfile


def encoded(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class WireJSON(httpx.AsyncByteStream):
    def __init__(self, value):
        self.raw = json.dumps(value).encode()

    async def __aiter__(self):
        yield self.raw


class SyntheticHub:
    def __init__(self):
        self.available = True
        self.keys = []
        self.calls = []
        self.origin = None
        self.finish_started = asyncio.Event()
        self.finish_wait = None
        self.lose_finish = False
        self.commit_before_loss = False

    def metadata(self, value):
        now = time.time()
        return {"credential_id": value["credential_id"], "method": "passkey",
            "algorithm": value["algorithm"], "fingerprint": hashlib.sha256(base64.urlsafe_b64decode(
                value["public_key_spki"] + "=" * (-len(value["public_key_spki"]) % 4))).hexdigest(),
            "trust": "tofu-unverified", "state": "active", "created_at": now, "usable_at": now + 600}

    async def __call__(self, request):
        route = request.url.path.split("/approval/")[1]
        body = json.loads(request.content) if request.method == "POST" else None
        self.calls.append((request.method, route, deepcopy(body)))
        result = {"api_version": 1}
        if route == "capabilities":
            result.update(available=self.available, methods=["passkey"] if self.available else [],
                          algorithms={"passkey": [-7, -257]})
        elif route == "keys":
            result["credentials"] = deepcopy(self.keys)
        elif route == "enrol/begin":
            self.origin = body["origin"]
            assert body == {"method": "passkey", "origin": self.origin}
            result.update(challenge_id="challenge_enrol", challenge=encoded(b"h" * 32),
                expires_at=int(time.time()) + 90, method="passkey", origin=self.origin,
                rp_id="localhost", bootstrap="tofu-unverified", algorithms=[-7, -257])
        elif route == "enrol/finish":
            self.finish_started.set()
            if self.finish_wait:
                await self.finish_wait.wait()
            metadata = self.metadata(body)
            if not self.lose_finish or self.commit_before_loss:
                self.keys = [metadata]
            if self.lose_finish:
                raise httpx.ReadError("synthetic lost response")
            result["credential"] = metadata
        else:
            raise AssertionError(route)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=WireJSON(result))


@pytest.fixture
async def subject(tmp_path):
    profile = SessionProfile(session_id="s_enrol", name="Original label", host_name="synthetic-host", participant_id="p_original",
        token="synthetic-bearer", url="http://synthetic-hub", home=str(tmp_path / "profile"))
    hub = SyntheticHub()
    transport = ApprovalTransport(profile, transport=httpx.MockTransport(hub))
    urls = []
    page = passkey.PasskeyPage(opener=lambda value: urls.append(value) or True)
    service = EnrolService(page, transport)
    await service.human_start()
    flow = service.flow
    root = f"/enrol/{flow.identity}"
    headers = {"X-Collab-Approval-Token": flow.token, "Origin": page.origin}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=page._guard,
            client=("127.0.0.1", 1234)), base_url=page.origin) as client:
        try:
            yield SimpleNamespace(profile=profile, hub=hub, transport=transport, page=page,
                service=service, flow=flow, root=root, headers=headers, client=client, urls=urls)
        finally:
            if hub.finish_wait:
                hub.finish_wait.set()
            await page.stop()
            await transport.close()


async def options(subject):
    response = await subject.client.post(subject.root + "/options", headers=subject.headers, json={})
    assert response.status_code == 200, response.text
    return response.json()


def proof(subject, issued):
    # Deliberately synthetic public-key/signature bytes. These fixtures check
    # byte-preserving transport, not cryptographic acceptance at the real core.
    client_data = b'{"type":"webauthn.get", "origin":' + json.dumps(subject.page.origin).encode() + \
        b',"challenge":' + json.dumps(issued["publicKey"]["challenge"]).encode() + b',"untouched":true}'
    identity = encoded(b"new-credential")
    return {"challenge_id": issued["challenge_id"],
        "created_key": {"credential_id": identity, "algorithm": -7,
                        "public_key_spki": encoded(b"synthetic-not-a-real-SPKI")},
        "credential": {"id": identity, "rawId": identity, "type": "public-key",
            "response": {"clientDataJSON": encoded(client_data), "authenticatorData": encoded(b"a" * 37),
                         "signature": encoded(b"synthetic-signature")}}}


async def test_enrolment_preserves_the_principal_and_forwards_only_the_possession_proof(subject):
    state = (await subject.client.get(subject.root + "/status", headers=subject.headers)).json()
    assert state["participant_id"] == "p_original" and state["display_name"] == "Original label"
    issued = await options(subject)
    assert issued["publicKey"]["user"]["id"] == encoded(b"p_original")
    assert issued["publicKey"]["attestation"] == "none"
    assert issued["publicKey"]["authenticatorSelection"]["userVerification"] == "required"
    body = proof(subject, issued)
    response = await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "enrolled"
    sent = [value for method, path, value in subject.hub.calls if path == "enrol/finish"]
    assert len(sent) == 1 and set(sent[0]) == {
        "challenge_id", "credential_id", "algorithm", "public_key_spki", "proof"}
    assert sent[0]["proof"] == {"type": "webauthn.get",
        "client_data_json": body["credential"]["response"]["clientDataJSON"],
        "authenticator_data": body["credential"]["response"]["authenticatorData"],
        "signature": body["credential"]["response"]["signature"]}
    assert "receipt" not in response.text and "synthetic-bearer" not in response.text
    metadata = response.json()["credential"]
    assert metadata["usable_at"] - metadata["created_at"] >= 600
    assert metadata["trust"] == "tofu-unverified"
    assert (await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)).status_code == 409


async def test_request_and_enrolment_capabilities_do_not_authorize_one_another(subject):
    request_url = await subject.page.window_url("request_one")
    request_token = urlsplit(request_url).fragment
    wrong = {**subject.headers, "X-Collab-Approval-Token": request_token}
    assert (await subject.client.get(subject.root + "/status", headers=wrong)).status_code == 403
    assert (await subject.client.get("/consent/request_one/review", headers=subject.headers)).status_code == 403
    request_headers = {**subject.headers, "X-Collab-Approval-Token": request_token}
    assert (await subject.client.post("/consent/request_one/enrol/options", headers=request_headers, json={})).status_code == 404
    assert (await subject.client.post(subject.root + "/approve/result", headers=subject.headers, json={})).status_code == 404
    assert len(subject.page.app.router.routes) == 6
    with pytest.raises(RuntimeError):
        EnrolService(subject.page, subject.transport)
    assert len(subject.page.app.router.routes) == 6


async def test_unavailable_core_prevents_a_window_and_any_enrolment_request(subject):
    subject.service.invalidate()
    subject.hub.available = False
    urls = len(subject.urls)
    with pytest.raises(passkey.NotReady):
        await subject.service.human_start()
    assert subject.service.flow is None and len(subject.urls) == urls
    assert not any(path.startswith("enrol/") for _, path, _ in subject.hub.calls)


async def test_an_existing_key_does_not_turn_into_a_bootstrap_replacement(subject):
    subject.hub.keys = [{"credential_id": "existing", "state": "frozen"}]
    response = await subject.client.get(subject.root + "/status", headers=subject.headers)
    assert response.json()["status"] == "exists"
    assert (await subject.client.post(subject.root + "/options", headers=subject.headers, json={})).status_code == 409
    assert not any(path.startswith("enrol/") for _, path, _ in subject.hub.calls)


@pytest.mark.parametrize("fault", ["other-id", "other-origin", "other-challenge", "signature-limit", "spki-limit", "create-only"])
async def test_mismatched_or_oversized_evidence_never_reaches_finish(subject, fault):
    issued = await options(subject)
    body = proof(subject, issued)
    if fault == "other-id":
        body["created_key"]["credential_id"] = encoded(b"somebody-else")
    elif fault in ("other-origin", "other-challenge"):
        data = json.loads(base64.urlsafe_b64decode(body["credential"]["response"]["clientDataJSON"] + "=" *
            (-len(body["credential"]["response"]["clientDataJSON"]) % 4)))
        data["origin" if fault == "other-origin" else "challenge"] = "https://wrong.invalid" if fault == "other-origin" else encoded(b"x" * 32)
        body["credential"]["response"]["clientDataJSON"] = encoded(json.dumps(data).encode())
    elif fault == "signature-limit":
        body["credential"]["response"]["signature"] = encoded(b"x" * 4097)
    elif fault == "spki-limit":
        body["created_key"]["public_key_spki"] = encoded(b"x" * 4097)
    else:
        body["credential"]["response"] = {"attestationObject": encoded(b"create-is-not-proof")}
    response = await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)
    assert response.status_code != 200
    assert not any(path == "enrol/finish" for _, path, _ in subject.hub.calls)


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_finish_is_reconciled_only_by_the_exact_registered_key(subject, committed):
    body = proof(subject, await options(subject))
    subject.hub.lose_finish = True
    subject.hub.commit_before_loss = committed
    response = await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)
    assert response.status_code == 502 and response.json()["status"] == "unknown"
    assert subject.service.held is not None
    assert (await subject.client.post(subject.root + "/options", headers=subject.headers, json={})).status_code == 409
    state = (await subject.client.get(subject.root + "/status", headers=subject.headers)).json()
    assert state["status"] == ("enrolled" if committed else "uncertain")
    assert len([path for _, path, _ in subject.hub.calls if path == "enrol/finish"]) == 1
    if not committed:
        await subject.service.human_start()
        current = subject.service.flow
        headers = {**subject.headers, "X-Collab-Approval-Token": current.token}
        assert (await subject.client.post(f"/enrol/{current.identity}/options", headers=headers, json={})).status_code == 409


async def test_stop_revokes_the_capability_and_late_finish_cannot_report_success(subject):
    body = proof(subject, await options(subject))
    subject.hub.finish_wait = asyncio.Event()
    task = asyncio.create_task(subject.client.post(subject.root + "/finish", headers=subject.headers, json=body))
    await asyncio.wait_for(subject.hub.finish_started.wait(), 1)
    await subject.page.stop()
    assert subject.service.flow is None
    subject.hub.finish_wait.set()
    response = await asyncio.wait_for(task, 1)
    assert response.status_code == 502 and response.json()["status"] == "unknown"
    assert subject.service.held is not None
    assert (await subject.client.get(subject.root + "/status", headers=subject.headers)).status_code == 403


async def test_enrol_routes_use_the_existing_guard_and_cannot_mint_a_capability(subject):
    for headers in ({**subject.headers, "Host": "127.0.0.1:1"},
                    {**subject.headers, "Origin": "null"},
                    {**subject.headers, "X-Collab-Approval-Token": "q" * 43}):
        assert (await subject.client.post(subject.root + "/options", headers=headers, json={})).status_code == 403
    assert (await subject.client.get(subject.root + "/status?token=" + subject.flow.token, headers=subject.headers)).status_code == 403
    assert (await subject.client.post("/enrol/start", headers=subject.headers, json={})).status_code != 200
    assert (await subject.client.post(subject.root + "/human_start", headers=subject.headers, json={})).status_code == 404
    assert not any(path.startswith("enrol/") for _, path, _ in subject.hub.calls)


async def test_status_does_not_reuse_a_prior_confirmation_after_the_key_disappears(subject):
    body = proof(subject, await options(subject))
    assert (await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)).status_code == 200
    subject.hub.keys = []
    state = (await subject.client.get(subject.root + "/status", headers=subject.headers)).json()
    assert state["status"] == "uncertain" and state["credential"] is None
    assert subject.service.held is not None


async def test_duplicate_json_and_expired_ceremonies_never_forward_a_finish(subject):
    issued = await options(subject)
    response = await subject.client.post(subject.root + "/finish", headers={**subject.headers, "Content-Type": "application/json"},
        content=b'{"challenge_id":"a","challenge_id":"b"}')
    assert response.status_code != 200
    subject.flow.expires = int(time.time()) - 1
    assert (await subject.client.post(subject.root + "/finish", headers=subject.headers, json=proof(subject, issued))).status_code == 409
    assert not any(path == "enrol/finish" for _, path, _ in subject.hub.calls)


async def test_expiry_during_a_slow_body_cannot_start_a_hub_ceremony(subject):
    class ExpiringBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            subject.flow.deadline = time.monotonic() - 1
            await asyncio.sleep(.01)
            yield b"}"
    response = await subject.client.post(subject.root + "/options", content=ExpiringBody(),
        headers={**subject.headers, "Content-Type": "application/json"})
    assert response.status_code != 200
    assert not any(path.startswith("enrol/") for _, path, _ in subject.hub.calls)


async def test_cancelling_an_inflight_finish_keeps_its_key_and_refuses_a_second_finish(subject):
    body = proof(subject, await options(subject))
    subject.hub.finish_wait = asyncio.Event()
    task = asyncio.create_task(subject.client.post(subject.root + "/finish", headers=subject.headers, json=body))
    await asyncio.wait_for(subject.hub.finish_started.wait(), 1)
    concurrent = await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)
    assert concurrent.status_code == 409
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert subject.service.held is not None and not subject.flow.busy
    assert (await subject.client.get(subject.root + "/status", headers=subject.headers)).json()["status"] == "uncertain"
    assert (await subject.client.post(subject.root + "/options", headers=subject.headers, json={})).status_code == 409
    assert len([path for _, path, _ in subject.hub.calls if path == "enrol/finish"]) == 1


async def test_a_late_cancelled_opener_cannot_revoke_a_replacement_enrolment_window(subject):
    import threading
    entered, release = threading.Event(), threading.Event()
    def opener(_):
        entered.set()
        assert release.wait(2)
        return True
    subject.page.opener = opener
    task = asyncio.create_task(subject.service.human_start())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert subject.service.flow is None
        subject.page.opener = lambda _: True
        await subject.service.human_start()
        replacement = subject.service.flow
        release.set()
        await asyncio.sleep(.02)
        assert subject.service.flow is replacement
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_changed_authority_blocks_the_flow_before_any_network_request(subject):
    calls = deepcopy(subject.hub.calls)
    subject.profile.token = "another-authority"
    assert (await subject.client.get(subject.root + "/status", headers=subject.headers)).status_code != 200
    assert subject.hub.calls == calls


async def test_a_mismatched_fingerprint_does_not_resolve_an_uncertain_finish(subject):
    body = proof(subject, await options(subject))
    subject.hub.lose_finish = subject.hub.commit_before_loss = True
    assert (await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)).status_code == 502
    subject.hub.keys[0]["fingerprint"] = "f" * 64
    state = (await subject.client.get(subject.root + "/status", headers=subject.headers)).json()
    assert state["status"] == "uncertain" and state["credential"] is None
    assert subject.service.held is not None


async def test_duplicate_capability_headers_and_frames_are_not_authorized(subject):
    headers = list(subject.headers.items()) + [("X-Collab-Approval-Token", "q" * 43)]
    assert (await subject.client.get(subject.root + "/status", headers=headers)).status_code == 403
    page = await subject.client.get(subject.root)
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert subject.flow.token not in page.text and subject.profile.token not in page.text


async def test_stopping_during_readiness_cannot_restart_the_listener_or_mint_a_window(subject):
    entered, release = asyncio.Event(), asyncio.Event()
    original = subject.transport.ready
    async def held_ready():
        entered.set()
        await release.wait()
        await original()
    subject.transport.ready = held_ready
    opened = len(subject.urls)
    task = asyncio.create_task(subject.service.human_start())
    await asyncio.wait_for(entered.wait(), 1)
    await subject.page.stop()
    release.set()
    with pytest.raises(RuntimeError, match="stopped"):
        await task
    assert subject.service.flow is None and subject.page._task is None
    assert len(subject.urls) == opened


async def test_enrolment_does_not_require_the_python_311_timeout_context_manager(subject, monkeypatch):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    body = proof(subject, await options(subject))
    response = await subject.client.post(subject.root + "/finish", headers=subject.headers, json=body)
    assert response.status_code == 200, response.text


async def test_a_start_already_queued_before_stop_cannot_mint_after_lock_admission(subject):
    entered, release = asyncio.Event(), asyncio.Event()
    original = subject.transport.ready
    async def held_ready():
        entered.set()
        await release.wait()
        await original()
    subject.transport.ready = held_ready
    opened = len(subject.urls)
    first = asyncio.create_task(subject.service.human_start())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(subject.service.human_start())
    await asyncio.sleep(0)
    await subject.page.stop()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert subject.service.flow is None and subject.page._task is None
    assert len(subject.urls) == opened


async def test_a_shared_page_cannot_silently_switch_to_another_approval_principal(subject):
    subject.page.adapter = SimpleNamespace(transport=SimpleNamespace(identity=("s_other", "p_other", ())))
    calls = deepcopy(subject.hub.calls)
    response = await subject.client.get(subject.root + "/status", headers=subject.headers)
    assert response.status_code != 200 and subject.hub.calls == calls


async def test_python_310_deadline_exceptions_get_a_structured_refusal(subject, monkeypatch):
    class LegacyAsyncioTimeout(Exception):
        pass
    monkeypatch.setattr(asyncio, "TimeoutError", LegacyAsyncioTimeout)
    async def expired_body(_):
        raise LegacyAsyncioTimeout
    monkeypatch.setattr(subject.service, "_body", expired_body)
    response = await subject.client.post(subject.root + "/options", headers=subject.headers, json={})
    assert response.status_code == 502 and response.json()["status"] == "refused"
    assert not any(path.startswith("enrol/") for _, path, _ in subject.hub.calls)
