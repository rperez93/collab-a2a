"""Local transport tests use an injected fake, never a real approval receipt."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import time
from urllib.parse import urlsplit

import httpx
import pytest

from collab.client import passkey as module


def encoded(value=b"synthetic bytes"):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class FakeAdapter:
    """Models the future trusted adapter interface, not signature verification."""
    def __init__(self):
        canonical = b'{"payload":"<script>untrusted()</script>","subject":"test"}'
        self.current = module.Review(canonical, hashlib.sha256(canonical).digest(),
            "Synthetic request", module.Requester("p_fixture_peer", "fixture-peer"), "fixture-target", "fixture operation",
            time.time() + 300, enrolled=True)
        self.calls = []
        self.finish_wait = False
        self.finish_started = asyncio.Event()

    async def review(self, request_id):
        self.calls.append(("review", request_id))
        return self.current

    async def begin(self, request_id, operation, origin, digest):
        self.calls.append(("begin", request_id, operation, origin, digest))
        options = {"challenge": encoded(b"c" * 32), "timeout": 60000}
        if operation == "enrol":
            options.update(rp={"id": "localhost", "name": "Collab"},
                           user={"id": encoded(b"participant"), "name": "fixture", "displayName": "Fixture"})
        else:
            options.update(rpId="localhost", allowCredentials=[{"type": "public-key", "id": encoded(b"credential")}])
        return module.Ceremony("ceremony_" + str(len(self.calls)), options, time.time() + 60, digest)

    async def complete(self, *args):
        self.calls.append(("complete", *args))
        self.finish_started.set()
        if self.finish_wait: await asyncio.Event().wait()
        return module.Outcome("enrolled" if args[1] == "enrol" else "approved", "Synthetic adapter result only")

    async def decline(self, *args):
        self.calls.append(("decline", *args))
        if self.finish_wait: await asyncio.Event().wait()
        return module.Outcome("declined", "Synthetic adapter result only")


@pytest.fixture
async def subject():
    adapter = FakeAdapter()
    page = module.PasskeyPage(adapter, opener=lambda url: True)
    url = await page.window_url("request_1")
    token = urlsplit(url).fragment
    headers = {"X-Collab-Approval-Token": token, "Origin": page.origin}
    transport = httpx.ASGITransport(app=page._guard, client=("127.0.0.1", 4242))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=page.origin) as client:
            yield page, adapter, client, headers, url
    finally:
        await page.stop()


def credential(operation):
    response = {"clientDataJSON": encoded(b'{"type":"synthetic-not-a-proof","untouched":true}')}
    if operation == "enrol":
        response.update(attestationObject=encoded(), publicKey=encoded(b"not-a-real-SPKI"), publicKeyAlgorithm=-7)
    else:
        response.update(authenticatorData=encoded(), signature=encoded())
    return {"id": encoded(b"credential"), "rawId": encoded(b"credential"), "type": "public-key", "response": response}


async def ready(subject, operation="approve"):
    _, _, client, headers, _ = subject
    review = await client.get("/consent/request_1/review", headers=headers)
    assert review.status_code == 200
    options = await client.post(f"/consent/request_1/{operation}/options", headers=headers, json={})
    assert options.status_code == 200, options.text
    return {"ceremony_id": options.json()["ceremony_id"], "credential": credential(operation)}


async def test_a_window_never_puts_its_capability_or_review_in_static_html(subject):
    """A fragment is not sent in Referer, and private payload needs its header."""
    page, adapter, client, headers, url = subject
    parsed = urlsplit(url)
    assert parsed.hostname == "localhost" and not parsed.query and len(parsed.fragment) == 43
    response = await client.get(parsed.path)
    assert response.status_code == 200
    assert parsed.fragment not in response.text and adapter.current.canonical.decode() not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    denied = await client.get(parsed.path + "/review")
    assert denied.status_code == 403 and adapter.calls == []
    review = await client.get(parsed.path + "/review", headers=headers)
    assert review.json()["canonical_review"].encode() == adapter.current.canonical
    assert review.json()["summary"]["digest"] == hashlib.sha256(adapter.current.canonical).hexdigest()
    assert page.origin == f"http://localhost:{page.port}"


@pytest.mark.parametrize("change", ["host", "origin", "missing-origin", "query", "remote-client", "nonascii-token", "duplicate-host"])
async def test_another_origin_or_ambiguous_header_cannot_reach_the_adapter(subject, change):
    """Localhost is not permission for every origin, port or malformed capability."""
    page, adapter, client, headers, _ = subject
    headers = dict(headers)
    path = "/consent/request_1/approve/options"
    if change == "host": headers["Host"] = "localhost:1"
    elif change == "origin": headers["Origin"] = "http://localhost:1"
    elif change == "missing-origin": headers.pop("Origin")
    elif change == "query": path += "?capability=wrong"
    elif change == "nonascii-token":
        headers = [(key.encode(), value.encode()) for key, value in headers.items()]
        headers = [(key, b"\xff" * 43 if key.lower() == b"x-collab-approval-token" else value) for key, value in headers]
    elif change == "duplicate-host": headers = [*headers.items(), ("Host", urlsplit(page.origin).netloc), ("Host", "localhost:1")]
    if change == "remote-client":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=page._guard, client=("192.0.2.1", 1)), base_url=page.origin) as other:
            response = await other.post(path, headers=headers, json={})
    else:
        response = await client.post(path, headers=headers, json={})
    assert response.status_code == 403
    assert adapter.calls == []


@pytest.mark.parametrize("operation", ["approve"])
async def test_exact_credential_bytes_are_forwarded_with_the_bound_origin_and_digest(subject, operation):
    """The page transports proof fields; it never parses or signs clientDataJSON."""
    page, adapter, client, headers, _ = subject
    data = await ready(subject, operation)
    response = await client.post(f"/consent/request_1/{operation}/result", headers=headers, json=data)
    assert response.status_code == 200
    call = next(call for call in adapter.calls if call[0] == "complete")
    assert call[1:] == ("request_1", operation, data["ceremony_id"], data["credential"], page.origin, adapter.current.digest)
    replay = await client.post(f"/consent/request_1/{operation}/result", headers=headers, json=data)
    assert replay.status_code == 409
    assert len([call for call in adapter.calls if call[0] == "complete"]) == 1


async def test_a_changed_review_digest_or_relying_party_is_refused_before_a_ceremony(subject):
    """The browser must not sign a challenge for bytes it has not been shown."""
    _, adapter, client, headers, _ = subject
    await client.get("/consent/request_1/review", headers=headers)
    original = adapter.begin
    async def wrong(*args): return replace(await original(*args), review_digest=b"x" * 32)
    adapter.begin = wrong
    response = await client.post("/consent/request_1/approve/options", headers=headers, json={})
    assert response.status_code == 400
    assert not any(call[0] == "complete" for call in adapter.calls)


@pytest.mark.parametrize("case", ["review-digest", "review-size", "ceremony-expired", "challenge-size", "rp", "oversized-signature", "client-attested-flag", "raw-id"])
async def test_malformed_or_expired_evidence_never_reaches_completion(subject, case):
    _, adapter, client, headers, _ = subject
    if case == "review-digest": adapter.current = replace(adapter.current, digest=b"x" * 32)
    if case == "review-size":
        raw = b"x" * (module.MAX_REVIEW + 1)
        adapter.current = replace(adapter.current, canonical=raw, digest=hashlib.sha256(raw).digest())
    review = await client.get("/consent/request_1/review", headers=headers)
    if case.startswith("review-"):
        assert review.status_code == 502
        return
    original = adapter.begin
    async def changed(*args):
        value = await original(*args)
        if case == "ceremony-expired": return replace(value, expires=time.time() - 1)
        if case == "challenge-size": value.public_key["challenge"] = encoded(b"short")
        if case == "rp": value.public_key["rpId"] = "attacker.invalid"
        return value
    adapter.begin = changed
    options = await client.post("/consent/request_1/approve/options", headers=headers, json={})
    if case in ("ceremony-expired", "challenge-size", "rp"):
        assert options.status_code == 400
        return
    data = {"ceremony_id": options.json()["ceremony_id"], "credential": credential("approve")}
    if case == "oversized-signature": data["credential"]["response"]["signature"] = encoded(b"x" * 8193)
    if case == "client-attested-flag": data["credential"]["response"]["attested"] = True
    if case == "raw-id": data["credential"]["id"] = encoded(b"different")
    response = await client.post("/consent/request_1/approve/result", headers=headers, json=data)
    assert response.status_code == 400
    assert not any(call[0] == "complete" for call in adapter.calls)


async def test_oversized_or_trickling_body_is_bounded_before_any_forwarding(subject, monkeypatch):
    """Even a timely stream cannot hold the local operation or grow its record forever."""
    _, adapter, client, headers, _ = subject
    await ready(subject)
    response = await client.post("/consent/request_1/approve/result",
        headers={**headers, "Content-Type": "application/json"}, content=b" " * (module.MAX_REQUEST + 1))
    assert response.status_code == 413
    monkeypatch.setattr(module, "BODY_SECONDS", .03)
    async def trickle():
        for _ in range(100):
            yield b" "
            await asyncio.sleep(.01)
    response = await client.post("/consent/request_1/approve/result", headers={**headers, "Content-Type": "application/json"}, content=trickle())
    assert response.status_code == 408
    assert not any(call[0] == "complete" for call in adapter.calls)


@pytest.mark.parametrize("operation", ["approve", "decline"])
async def test_uncertain_forwarding_never_retries_or_claims_local_cancellation(subject, monkeypatch, operation):
    """A timeout can follow a hub commit; only a fresh hub read can reconcile it."""
    page, adapter, client, headers, url = subject
    data = await ready(subject, "enrol" if operation == "enrol" else "approve")
    adapter.finish_wait = True
    monkeypatch.setattr(module, "BACKEND_SECONDS", .03)
    path = "/consent/request_1/" + ("decline" if operation == "decline" else operation + "/result")
    response = await client.post(path, headers=headers, json={} if operation == "decline" else data)
    assert response.status_code == 502 and response.json()["status"] == "unknown"
    assert await page.window_url("request_1") == url
    denied = await client.post("/consent/request_1/approve/options", headers=headers, json={})
    assert denied.status_code == 409
    assert len([call for call in adapter.calls if call[0] in ("complete", "decline")]) == 1
    adapter.current = replace(adapter.current, status="unknown")
    await client.get("/consent/request_1/review", headers=headers)
    assert (await client.post("/consent/request_1/approve/options", headers=headers, json={})).status_code == 409
    adapter.current = replace(adapter.current, status="pending")
    await client.get("/consent/request_1/review", headers=headers)
    assert (await client.post("/consent/request_1/approve/options", headers=headers, json={})).status_code == 200


async def test_the_unconfigured_adapter_reports_not_ready_instead_of_fake_approval(subject):
    page, _, client, headers, _ = subject
    page.adapter = module.UnsupportedAdapter()
    response = await client.get("/consent/request_1/review", headers=headers)
    assert response.status_code == 503 and response.json()["status"] == "unsupported"
    assert "No approval was submitted" in response.json()["message"]


async def test_silent_and_unterminated_headers_are_closed_before_asgi(subject, monkeypatch):
    """ASGI-only limits miss connections which never finish their headers."""
    page, _, _, _, _ = subject
    monkeypatch.setattr(module, "BODY_SECONDS", .03)
    reader, writer = await asyncio.open_connection("127.0.0.1", page.port)
    try:
        assert await asyncio.wait_for(reader.read(), .5) == b""
    finally:
        writer.close(); await writer.wait_closed()
    reader, writer = await asyncio.open_connection("127.0.0.1", page.port)
    try:
        writer.write(b"GET / HTTP/1.1\r\nX-Big: " + b"x" * 9000)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), .5) == b""
    finally:
        writer.close(); await writer.wait_closed()


async def test_window_and_socket_counts_are_bounded_without_a_process_scan(subject):
    page, _, _, _, _ = subject
    for index in range(module.MAX_WINDOWS - 1): await page.window_url("extra_" + str(index))
    with pytest.raises(RuntimeError, match="Too many"):
        await page.window_url("overflow")
    sockets = [await asyncio.open_connection("127.0.0.1", page.port) for _ in range(module.MAX_CONNECTIONS)]
    extra_reader, extra_writer = await asyncio.open_connection("127.0.0.1", page.port)
    try:
        assert await asyncio.wait_for(extra_reader.read(), .5) == b""
    finally:
        extra_writer.close(); await extra_writer.wait_closed()
        for _, writer in sockets:
            writer.close(); await writer.wait_closed()


@pytest.mark.parametrize("action", ["approve/options", "approve/result", "decline"])
@pytest.mark.parametrize("expiry", ["review", "window"])
async def test_expiry_during_body_collection_never_authorizes_a_later_action(subject, monkeypatch, action, expiry):
    """A valid capability at header time cannot survive a slow body past expiry."""
    page, adapter, client, headers, _ = subject
    result = await ready(subject)
    window = page._windows["request_1"]
    if expiry == "review":
        window.review = replace(window.review, expires=time.time() + .02)
    else:
        window.deadline = time.monotonic() + .02
    original = page._body
    async def slow(request):
        data = await original(request)
        await asyncio.sleep(.06)
        return data
    monkeypatch.setattr(page, "_body", slow)
    before = len(adapter.calls)
    response = await client.post("/consent/request_1/" + action, headers=headers,
                                 json=result if action.endswith("/result") else {})
    assert response.status_code == 410
    assert len(adapter.calls) == before


def test_adapter_response_traversal_rejects_cycles_and_excess_queued_nodes():
    """A malformed trusted adapter cannot grow traversal storage before refusal."""
    cyclic = [None] * 4096
    cyclic[0] = cyclic
    with pytest.raises(ValueError, match="limit"):
        module._json(cyclic)
    child = [None] * 4096
    repeated = {str(i): child for i in range(4096)}
    with pytest.raises(ValueError, match="object"):
        module._json(repeated)


async def test_cancelled_opener_revokes_only_its_own_window(subject):
    """A cancelled desktop launch must revoke its late page, never a replacement."""
    import threading
    page, _, _, _, _ = subject
    started, release = threading.Event(), threading.Event()
    def opener(url):
        started.set()
        release.wait(2)
        return True
    page.opener = opener
    pending = asyncio.create_task(page.open("cancel_fixture"))
    try:
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
        assert "cancel_fixture" not in page._windows
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_failed_old_opener_cannot_revoke_a_replacement(subject):
    """Two desktop launches may finish out of order without cross-revocation."""
    import threading
    page, _, _, _, _ = subject
    started, release = threading.Event(), threading.Event()
    def opener(url):
        started.set()
        release.wait(2)
        return False
    page.opener = opener
    pending = asyncio.create_task(page.open("replacement_fixture"))
    try:
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        await page.window_url("replacement_fixture")
        replacement = page._windows["replacement_fixture"]
        release.set()
        with pytest.raises(RuntimeError): await pending
        assert page._windows["replacement_fixture"] is replacement
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("path", ["enrol/options", "enrol/result"])
async def test_a_request_capability_never_authorizes_key_enrolment(subject, path):
    """Even a stolen valid request URL cannot issue or finish a registration."""
    _, adapter, client, headers, _ = subject
    await client.get("/consent/request_1/review", headers=headers)
    before = len(adapter.calls)
    response = await client.post("/consent/request_1/"+path, headers=headers, json={})
    assert response.status_code == 404
    assert len(adapter.calls) == before
    html = (await client.get("/consent/request_1")).text
    assert 'id="enrol"' not in html


async def test_a_wrong_valid_length_capability_never_reaches_the_adapter(subject):
    """Format validation alone cannot authorize a different 43-character token."""
    _, adapter, client, headers, _ = subject
    supplied = headers["X-Collab-Approval-Token"]
    wrong = ("B" if supplied[0] != "B" else "C") + supplied[1:]
    response = await client.get("/consent/request_1/review", headers={**headers,"X-Collab-Approval-Token":wrong})
    assert response.status_code == 403
    assert not adapter.calls


async def test_expiry_during_credential_validation_refuses_forwarding(subject, monkeypatch):
    """The final guard must survive expiry after body validation already passed."""
    page, adapter, client, headers, _ = subject
    data = await ready(subject)
    original = module._credential
    def cross_deadline(value, operation):
        result = original(value, operation)
        page._windows["request_1"].deadline = time.monotonic()-1
        return result
    monkeypatch.setattr(module,"_credential",cross_deadline)
    response = await client.post("/consent/request_1/approve/result",headers=headers,json=data)
    assert response.status_code == 410
    assert not any(c[0] == "complete" for c in adapter.calls)


@pytest.mark.parametrize("algorithm", [-7, -257, -8, True, None, "-7"])
def test_exported_registration_evidence_accepts_only_the_agreed_spki_algorithms(algorithm):
    """The future separate enrolment helper cannot silently admit another key type."""
    value = credential("enrol")
    value["response"]["publicKeyAlgorithm"] = algorithm
    if type(algorithm) is int and algorithm in (-7,-257):
        assert module._credential(value,"enrol") == value
    else:
        with pytest.raises(ValueError): module._credential(value,"enrol")


@pytest.mark.parametrize("who", ["label-only", module.Requester("", "name"), module.Requester("p_1", "x"*257)])
async def test_a_review_without_both_requester_identity_fields_is_refused(subject, who):
    """Names alone must not be presented as the authenticated requesting principal."""
    _, adapter, client, headers, _ = subject
    adapter.current = replace(adapter.current, requester=who)
    response = await client.get("/consent/request_1/review",headers=headers)
    assert response.status_code == 502
