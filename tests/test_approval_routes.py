"""HTTP contract, bounds and authenticated state transitions."""
import asyncio
import json
import threading

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from collab.server import approval_routes as routes
from collab.server import approval_crypto as crypto
from test_approval_store import signer, proof, spki

P = routes.PREFIX


def post(client, headers, path, body):
    response = client.post(P + path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def enrol(client, session, headers, signer):
    c = post(client, headers, "/enrol/begin", {"method": "hello"})
    return post(client, headers, "/enrol/finish", {"challenge_id": c["challenge_id"],
        "credential_id": "http.key", "algorithm": -257, "public_key_spki": spki(signer), "proof": proof(signer, c)})


def test_http_enrollment_review_approval_and_internal_dispatch(client, session, host_headers, signer):
    now = [1000]
    core = session["app"].state.approvals
    core.clock = lambda: now[0]
    enrolled = enrol(client, session, host_headers, signer)
    assert enrolled["credential"]["trust"] == "tofu-unverified"
    token = session["host_token"]
    owner = session["store"].participant_for_token(token)
    req = core.create_request(token, target_id=owner.id, operation={
        "subject": "review", "destination": "local:skill", "authority": "install-skill", "payload": {"text": "untrusted"}})
    path = "/requests/" + req["request_id"]
    review = client.get(P + path + "/review", headers=host_headers)
    assert review.status_code == 200 and review.json()["digest"] == req["digest"]
    denied = client.post(P + path + "/challenge", headers=host_headers, json={"credential_id": "http.key"})
    assert denied.status_code == 409
    now[0] += 600
    req = core.create_request(token, target_id=owner.id, operation={
        "subject": "review", "destination": "local:skill", "authority": "install-skill", "payload": {}})
    path = "/requests/" + req["request_id"]
    challenge = post(client, host_headers, path + "/challenge", {"credential_id": "http.key"})
    body = {"challenge_id": challenge["challenge_id"], "credential_id": "http.key", "proof": proof(signer, challenge)}
    result = post(client, host_headers, path + "/approve", body)
    assert client.post(P + path + "/approve", headers=host_headers, json=body).status_code == 409
    assert client.post(P + path + "/consume", headers=host_headers, json={"receipt": result["receipt"]}).status_code == 404
    assert core.consume(token, req["request_id"], receipt=result["receipt"], digest=req["digest"])["strength"] == "strong"


def test_capabilities_report_strong_approval_live(client, host_headers):
    """2.2.0: Windows Hello and passkey are offered; attestation is still not verified."""
    response = client.get(P + "/capabilities", headers=host_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True and body["methods"] == ["hello", "passkey"] and "reason" not in body
    assert response.json()["attestation_verification"] is False
    assert client.post(P + "/requests", headers=host_headers, json={}).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}, {"Authorization": "Basic secret"}])
def test_auth_failures_are_content_free_and_not_cached(client, headers):
    result = client.post(P + "/enrol/begin", headers=headers, content="not json")
    assert result.status_code == 401
    assert result.json() == {"api_version": 1, "error": {"code": "unauthorized", "message": "unauthorized"}}
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body,code", [
    ('{"method":"hello","method":"passkey"}', "invalid_json"),
    ('{"method":"hello","target_id":"someone-else"}', "invalid_fields"),
    ('{"method":true}', "invalid_field"),
    ('[]', "invalid_fields"),
    ('{"method":NaN}', "invalid_json"),
    ('{"method":1e309}', "invalid_json"),
    ('{"method":', "invalid_json"),
    ('{"method":' + '[' * 1100 + '0' + ']' * 1100 + '}', "invalid_json"),
], ids=["duplicate", "unknown", "boolean", "array", "nan", "overflow", "truncated", "deep"])
def test_malformed_and_unknown_fields_rejected(client, host_headers, body, code):
    result = client.post(P + "/enrol/begin", headers={**host_headers, "Content-Type": "application/json"}, content=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == code


def test_boolean_algorithm_is_not_an_integer(client, host_headers):
    result = client.post(P + "/enrol/finish", headers=host_headers, json={
        "challenge_id": "unknown", "credential_id": "key", "algorithm": True, "public_key_spki": "AA", "proof": {}})
    assert result.status_code == 400 and result.json()["error"]["code"] == "invalid_field"


def test_body_limits_encodings_and_query_rejection(client, host_headers):
    assert client.post(P + "/enrol/begin", headers=host_headers, content="{}").status_code == 415
    assert client.post(P + "/enrol/begin", headers={**host_headers, "Content-Encoding": "gzip"}, json={}).status_code == 415
    assert client.post(P + "/enrol/begin", headers=host_headers, content=b"x" * (routes.MAX_BODY + 1)).status_code == 415
    assert client.post(P + "/enrol/begin", headers={**host_headers, "Content-Type": "application/json"}, content=b"x" * (routes.MAX_BODY + 1)).status_code == 413
    assert client.get(P + "/keys?token=not-a-credential", headers=host_headers).status_code == 400


def test_private_registry_and_revoked_token(client, session, host_headers, signer):
    enrol(client, session, host_headers, signer)
    person = session["store"].add_participant("other", "test-other")
    headers = {**host_headers, "Authorization": "Bearer test-other"}
    assert client.get(P + "/keys", headers=headers).json()["credentials"] == []
    assert client.get(P + "/audit", headers=headers).json()["events"] == []
    assert client.post(P + "/keys/report", headers=headers, json={"credential_id": "http.key"}).status_code == 404
    assert session["store"].revoke(person.id)
    assert client.get(P + "/keys", headers=headers).status_code == 401


def test_chunked_body_enforces_bound_without_content_length(session, host_headers):
    async def exercise():
        async def chunks():
            yield b'{"method":"'
            yield b"x" * routes.MAX_BODY
            raise AssertionError("server read beyond size bound")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"]), base_url="http://test") as client:
            response = await client.post(P + "/enrol/begin", headers={**host_headers, "Content-Type": "application/json"}, content=chunks())
            assert response.status_code == 413
    asyncio.run(exercise())


def test_slow_body_times_out_and_releases_admission(session, host_headers, monkeypatch):
    monkeypatch.setattr(routes, "BODY_SECONDS", 0.01)
    async def exercise():
        async def chunks():
            yield b'{"method":"'
            await asyncio.sleep(1)
            yield b'hello"}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"]), base_url="http://test") as client:
            response = await client.post(P + "/enrol/begin", headers={**host_headers, "Content-Type": "application/json"}, content=chunks())
            assert response.status_code == 408
            assert (await client.get(P + "/keys", headers=host_headers)).status_code == 200
    asyncio.run(exercise())


def test_cancelled_body_closes_stream_without_mutation_and_releases_slot(session, host_headers, monkeypatch):
    from collab.server.app import create_app
    monkeypatch.setattr(routes, "MAX_INFLIGHT", 1)
    app = create_app(store=session["store"], session_id="s_test", host_name="alice", public_url="http://test")

    async def exercise():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def chunks():
            try:
                yield b'{"method":"'
                entered.set()
                await asyncio.Event().wait()
                yield b'hello"}'
            finally:
                closed.set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = asyncio.create_task(client.post(P + "/enrol/begin", headers={**host_headers, "Content-Type": "application/json"}, content=chunks()))
            await asyncio.wait_for(entered.wait(), 2)
            assert (await client.get(P + "/capabilities", headers=host_headers)).status_code == 503
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            assert (await client.get(P + "/capabilities", headers=host_headers)).status_code == 200
    asyncio.run(exercise())
    with session["store"]._lock:
        assert session["store"]._db.execute("SELECT COUNT(*) FROM approval_challenges").fetchone()[0] == 0


def test_http_key_change_requires_two_proofs_then_report_freezes(client, session, host_headers, signer):
    now = [1000]
    session["app"].state.approvals.clock = lambda: now[0]
    enrol(client, session, host_headers, signer)
    now[0] += 600
    candidate = ec.generate_private_key(ec.SECP256R1())
    identity = crypto.encode(b"http-new-credential")
    c = post(client, host_headers, "/key-changes/begin", {"kind": "add", "credential_id": identity,
        "method": "passkey", "algorithm": -7, "public_key_spki": spki(candidate), "origin": "http://localhost:49152"})
    change_path = "/key-changes/" + c["change_id"]
    req = post(client, host_headers, change_path + "/prove", {"challenge_id": c["challenge_id"], "proof": proof(candidate, c)})
    request_path = "/requests/" + req["request_id"]
    challenge = post(client, host_headers, request_path + "/challenge", {"credential_id": "http.key"})
    approved = post(client, host_headers, request_path + "/approve", {"challenge_id": challenge["challenge_id"],
        "credential_id": "http.key", "proof": proof(signer, challenge)})
    applied = post(client, host_headers, change_path + "/apply", {"receipt": approved["receipt"]})
    assert applied["credential"]["credential_id"] == identity
    assert client.get(P + change_path, headers=host_headers).json()["state"] == "applied"
    frozen = post(client, host_headers, "/keys/report", {"credential_id": identity})
    assert frozen["credential"]["state"] == "frozen"


def test_admission_rejects_without_reading_extra_body(session, host_headers):
    async def exercise():
        entered = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()
        async def held_body():
            nonlocal entered
            entered += 1
            if entered == routes.MAX_INFLIGHT:
                all_entered.set()
            await release.wait()
            yield b'{"method":"hello"}'
        async def forbidden_body():
            raise AssertionError("busy endpoint must not read request body")
            yield b""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"]), base_url="http://test") as client:
            headers = {**host_headers, "Content-Type": "application/json"}
            tasks = [asyncio.create_task(client.post(P + "/enrol/begin", headers=headers, content=held_body())) for _ in range(routes.MAX_INFLIGHT)]
            try:
                await asyncio.wait_for(all_entered.wait(), 2)
                response = await client.post(P + "/enrol/begin", headers=headers, content=forbidden_body())
                assert response.status_code == 503 and response.json()["error"]["code"] == "approval_busy"
            finally:
                release.set()
                await asyncio.gather(*tasks)
    asyncio.run(exercise())


def test_invalid_bodies_count_toward_ingress_rate(session, host_headers):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"]), base_url="http://test") as client:
            for _ in range(routes.HTTP_RATE):
                result = await client.post(P + "/enrol/begin", headers=host_headers, json={"unknown": "field"})
                assert result.status_code == 400
            result = await client.get(P + "/keys", headers=host_headers)
            assert result.status_code == 429 and result.json()["error"]["code"] == "http_rate_limit"
    asyncio.run(exercise())


def test_live_http_enrollment_delivers_durable_security_alarm(live_server, host_headers, signer):
    with httpx.Client(base_url=live_server["base"], headers=host_headers, timeout=5) as client:
        c = client.post(P + "/enrol/begin", json={"method": "hello"})
        assert c.status_code == 200
        challenge = c.json()
        signature = proof(signer, challenge)
        response = client.post(P + "/enrol/finish", json={"challenge_id": challenge["challenge_id"],
            "credential_id": "live.key", "algorithm": -257, "public_key_spki": spki(signer), "proof": signature})
        assert response.status_code == 200
        with client.stream("GET", "/ext/collab/v1/events?since=0") as feed:
            assert feed.status_code == 200
            for line in feed.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("body", {}).get("event") == "approval-security":
                    assert event["kind"] == "system" and event["text"].startswith("SECURITY:")
                    assert event["body"]["credential_trust"] == "tofu-unverified"
                    assert signature["signature"] not in line and spki(signer) not in line
                    durable = live_server["store"].since(0)
                    assert any(item.seq == event["seq"] for item in durable)
                    break
            else:
                pytest.fail("feed ended without the enrollment alarm")


def test_repeated_request_cancellation_keeps_slot_until_thread_exits(session, host_headers, monkeypatch):
    from collab.server.app import create_app
    monkeypatch.setattr(routes, "MAX_INFLIGHT", 1)
    app = create_app(store=session["store"], session_id="s_test", host_name="alice", public_url="http://test")
    entered, release = threading.Event(), threading.Event()
    def blocked_keys(token):
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(app.state.approvals, "keys", blocked_keys)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = asyncio.create_task(client.get(P + "/keys", headers=host_headers))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                response = await client.get(P + "/capabilities", headers=host_headers)
                assert response.status_code == 503 and not task.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert (await client.get(P + "/capabilities", headers=host_headers)).status_code == 200
    asyncio.run(exercise())


def test_revocation_during_body_read_is_rechecked_before_mutation(session, host_headers):
    person = session["store"].add_participant("revocable", "revoked-during-body")
    headers = {**host_headers, "Authorization": "Bearer revoked-during-body", "Content-Type": "application/json"}
    async def exercise():
        async def chunks():
            yield b'{"method":'
            assert session["store"].revoke(person.id)
            yield b'"hello"}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session["app"]), base_url="http://test") as client:
            result = await client.post(P + "/enrol/begin", headers=headers, content=chunks())
            assert result.status_code == 401 and result.json()["error"]["code"] == "unauthorized"
    asyncio.run(exercise())
    with session["store"]._lock:
        assert session["store"]._db.execute("SELECT COUNT(*) FROM approval_challenges").fetchone()[0] == 0


def test_a_body_that_stalls_is_refused_with_408_on_every_supported_python(monkeypatch):
    """A client that opens a request and never finishes its body must get a
    clean 408, not hold the handler or escape as a 500. The first version used
    asyncio.timeout(), which does not exist on Python 3.10 (the floor), and
    caught the builtin TimeoutError, which on 3.10 is not asyncio's."""
    monkeypatch.setattr(routes, "BODY_SECONDS", 0.05)

    class Stalled:
        headers = {"content-type": "application/json"}

        async def stream(self):
            yield b"{"
            await asyncio.sleep(10)
            yield b"}"

    async def call():
        await routes._body(Stalled(), (), ())
    with pytest.raises(routes.Rejected) as caught:
        asyncio.run(call())
    assert (caught.value.code, caught.value.status) == ("body_timeout", 408)
