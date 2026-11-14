"""Production adapter contract tests; MockTransport positives are not human/crypto evidence."""
import asyncio
import base64
from dataclasses import asdict, replace
import hashlib
import json
import time

import httpx
import pytest

from collab.client.passkey import NotReady, _review_payload
from collab.client.passkey_adapter import (
    AdapterError, ApprovalBinding, ApprovalTransport, PasskeyHubAdapter, MAX_RESPONSE, strict_json,
)
from collab.config import SessionProfile


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def reply(value, *, raw=None, headers=None):
    return httpx.Response(200, headers={"Content-Type": "application/json", **(headers or {})},
        stream=httpx.ByteStream(raw if raw is not None else json.dumps(value).encode()))


class ContractHub:
    """Synthetic available:true endpoint contract only; never overrides production readiness."""
    def __init__(self):
        self.now = int(time.time())
        self.origin = "http://localhost:12345"
        self.credential_id = encode(b"explicit selected credential")
        self.challenge = encode(b"c" * 32)
        self.operation = {"api_version": 1, "session_id": "s_test", "requester_id": "p_requester",
            "target_id": "p_target", "subject": "Accept peer skill request", "destination": "participant:p_target",
            "authority": "skill-request:accept", "payload": {"source_seq": 42, "args": "untrusted full argument"}}
        self.canonical = json.dumps(self.operation, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        self.binding = ApprovalBinding("s_test", "p_target", "p_requester", "ar_request",
            hashlib.sha256(self.canonical).hexdigest(), self.now + 300,
            "participant:p_target", "skill-request:accept", 42)
        self.calls = []
        self.overrides = {}

    async def handle(self, request):
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["accept-encoding"] == "identity"
        path = request.url.path.removeprefix("/ext/collab/v1/approval/")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path in self.overrides:
            value = self.overrides[path]
            return await value(request) if callable(value) else reply(value)
        if path == "capabilities":
            data = {"api_version": 1, "available": True, "methods": ["passkey"], "algorithms": {"passkey": [-7, -257]}}
        elif path == "keys":
            data = {"api_version": 1, "credentials": [{"credential_id": self.credential_id, "method": "passkey",
                "algorithm": -7, "fingerprint": "f" * 64, "created_at": self.now - 601,
                "usable_at": self.now - 1, "state": "active", "trust": "tofu-unverified"}]}
        elif path.endswith("/review"):
            data = {"api_version": 1, "request_id": "ar_request", "review": self.operation,
                "canonical_review": encode(self.canonical), "digest": self.binding.digest,
                "state": "pending", "required_strength": "strong"}
        elif path.endswith("/challenge"):
            assert body == {"credential_id": self.credential_id, "origin": self.origin}
            data = {"api_version": 1, "request_id": "ar_request", "challenge_id": "ac_challenge",
                "credential_id": self.credential_id, "method": "passkey", "challenge": self.challenge,
                "expires_at": self.now + 90, "digest": self.binding.digest, "origin": self.origin, "rp_id": "localhost"}
        elif path.endswith("/approve"):
            data = {"api_version": 1, "request_id": "ar_request", "state": "approved",
                    "receipt": "private-receipt-never-in-browser", "expires_at": self.now + 60,
                    "credential_trust": "tofu-unverified"}
        elif path.endswith("/decline"):
            assert body == {}
            data = {"api_version": 1, "request_id": "ar_request", "state": "declined"}
        else:
            raise AssertionError(path)
        return reply(data)

    def assertion(self):
        original = json.dumps({"origin": self.origin, "type": "webauthn.get",
            "challenge": self.challenge, "crossOrigin": False}, indent=2).encode() + b" \n"
        return {"id": self.credential_id, "rawId": self.credential_id, "type": "public-key", "response": {
            "clientDataJSON": encode(original), "authenticatorData": encode(b"a" * 37), "signature": encode(b"s" * 64)}}


@pytest.fixture
def adapter_case(tmp_path):
    hub = ContractHub()
    profile = SessionProfile(session_id="s_test", url="https://owned-hub.example", name="target",
        host_name="host", token="synthetic-token", home=str(tmp_path), participant_id="p_target")
    profile.dir.mkdir(parents=True, exist_ok=True)
    adapter = PasskeyHubAdapter(profile, credential_id=hub.credential_id, bindings=[hub.binding],
        transport=httpx.MockTransport(hub.handle))
    return hub, profile, adapter


async def begin(hub, adapter):
    review = await adapter.review(hub.binding.request_id)
    ceremony = await adapter.begin(hub.binding.request_id, "approve", hub.origin, review.digest)
    return review, ceremony


async def test_requester_label_is_display_only_and_never_substitutes_for_the_canonical_id(adapter_case):
    hub, profile, original = adapter_case
    await original.close()
    label = "Untrusted <img src=x onerror=bad> label"
    binding = replace(hub.binding, requester_name=label)
    adapter = PasskeyHubAdapter(profile, credential_id=hub.credential_id, bindings=[binding],
        transport=httpx.MockTransport(hub.handle))
    try:
        review, ceremony = await begin(hub, adapter)
        assert review.requester.display_name == label
        assert review.requester.participant_id == "p_requester"
        assert review.canonical == hub.canonical and review.digest.hex() == hub.binding.digest
        await adapter.complete("ar_request", "approve", ceremony.ceremony_id,
            hub.assertion(), hub.origin, review.digest)
        with pytest.raises(AdapterError):
            adapter.take_receipt(replace(binding, requester_id="p_other"))
        assert adapter.take_receipt(replace(binding, requester_name="Renamed label")).binding.requester_id == "p_requester"
    finally:
        await adapter.close()


async def test_a_long_lived_adapter_admits_new_trusted_bindings_without_replacing_an_existing_one(adapter_case):
    hub, _, adapter = adapter_case
    try:
        new = replace(hub.binding, request_id="ar_next")
        adapter.register_binding(new)
        assert adapter.states["ar_next"].binding == new
        adapter.register_binding(replace(new, requester_name="Renamed display label"))
        assert len(adapter.states) == 2
        with pytest.raises(AdapterError):
            adapter.register_binding(replace(new, digest="b" * 64))
        with pytest.raises(AdapterError):
            adapter.register_binding(replace(new, target_id="p_other", request_id="ar_foreign"))
        assert len(adapter.states) == 2
    finally:
        await adapter.close()


async def test_binding_admission_is_bounded_and_prunes_only_expired_unlocked_states(adapter_case):
    hub, _, adapter = adapter_case
    try:
        for index in range(1, 16):
            adapter.register_binding(replace(hub.binding, request_id=f"ar_{index}"))
        with pytest.raises(AdapterError):
            adapter.register_binding(replace(hub.binding, request_id="ar_overflow"))
        first = adapter.states["ar_request"]
        first.binding = replace(first.binding, expires_at=int(time.time()) - 1)
        await first.lock.acquire()
        with pytest.raises(AdapterError):
            adapter.register_binding(replace(hub.binding, request_id="ar_while_locked"))
        first.lock.release()
        adapter.register_binding(replace(hub.binding, request_id="ar_after_expiry"))
        assert len(adapter.states) == 16 and "ar_request" not in adapter.states
        assert "ar_after_expiry" in adapter.states
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_mocked_v1_contract_preserves_signed_octets_and_keeps_receipt_private(adapter_case):
    hub, _, adapter = adapter_case
    try:
        review, ceremony = await begin(hub, adapter)
        assert review.canonical == hub.canonical and review.digest.hex() == hub.binding.digest
        assert review.expires == hub.binding.expires_at and review.requester.participant_id == "p_requester"
        assert ceremony.public_key["rpId"] == "localhost" and ceremony.public_key["userVerification"] == "required"
        credential = hub.assertion()
        outcome = await adapter.complete("ar_request", "approve", ceremony.ceremony_id,
                                         credential, hub.origin, review.digest)
        sent = next(body for method, path, body in hub.calls if path.endswith("/approve"))
        assert sent == {"challenge_id": "ac_challenge", "credential_id": hub.credential_id,
            "proof": {"type": "webauthn.get", "client_data_json": credential["response"]["clientDataJSON"],
                "authenticator_data": credential["response"]["authenticatorData"],
                "signature": credential["response"]["signature"]}}
        assert "private-receipt" not in json.dumps(asdict(outcome))
        assert "private-receipt" not in json.dumps(_review_payload("ar_request", review))
        with pytest.raises(AdapterError):
            adapter.take_receipt(replace(hub.binding, source_seq=43))
        receipt = adapter.take_receipt(hub.binding)
        assert receipt.receipt == "private-receipt-never-in-browser"
        assert receipt.canonical_review == hub.canonical and receipt.challenge_id == ceremony.ceremony_id
        assert receipt.credential_id == hub.credential_id and "private-receipt" not in repr(receipt)
        with pytest.raises(AdapterError):
            adapter.take_receipt(hub.binding)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["review", "begin", "complete", "decline"])
async def test_actual_unavailable_capability_contract_never_proceeds_to_a_mutation(adapter_case, operation):
    hub, _, adapter = adapter_case
    hub.overrides["capabilities"] = {"api_version": 1, "available": False, "methods": [],
        "supported_methods": ["hello", "passkey"], "reason": "operation_integration_pending"}
    try:
        with pytest.raises(NotReady):
            if operation == "review": await adapter.review("ar_request")
            elif operation == "begin": await adapter.begin("ar_request", "approve", hub.origin, bytes.fromhex(hub.binding.digest))
            elif operation == "decline": await adapter.decline("ar_request", hub.origin, bytes.fromhex(hub.binding.digest))
            else: await adapter.complete("ar_request", "approve", "ac_challenge", hub.assertion(), hub.origin, bytes.fromhex(hub.binding.digest))
        assert [path for _, path, _ in hub.calls] == ["capabilities"]
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session_id", "target_id", "requester_id", "destination", "authority", "source_seq"])
async def test_a_review_must_match_every_trusted_dispatcher_binding(adapter_case, change):
    hub, profile, adapter = adapter_case
    if change == "source_seq": hub.operation["payload"][change] = 43
    else: hub.operation[change] = "different"
    hub.canonical = json.dumps(hub.operation, sort_keys=True, separators=(",", ":")).encode()
    # Make the byte digest agree so this independently exercises semantic
    # dispatcher binding, rather than merely failing the earlier hash check.
    await adapter.close()
    hub.binding = replace(hub.binding, digest=hashlib.sha256(hub.canonical).hexdigest())
    adapter = PasskeyHubAdapter(profile, credential_id=hub.credential_id, bindings=[hub.binding],
                               transport=httpx.MockTransport(hub.handle))
    try:
        with pytest.raises(AdapterError): await adapter.review("ar_request")
        assert not adapter.states["ar_request"].canonical
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_request_windows_never_use_the_transports_separate_enrollment_routes(adapter_case):
    hub, _, adapter = adapter_case
    try:
        with pytest.raises(NotReady): await adapter.begin("ar_request", "enrol", hub.origin, b"d"*32)
        assert not hub.calls
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "rawId", "clientDataJSON", "signature"])
async def test_changed_or_oversized_evidence_is_refused_before_forwarding(adapter_case, field):
    hub, _, adapter = adapter_case
    try:
        review, ceremony = await begin(hub, adapter)
        credential = hub.assertion()
        if field in ("id", "rawId"):credential[field] = encode(b"replacement")
        else:credential["response"][field] = encode(b"x" * (8193 if field == "clientDataJSON" else 4097))
        with pytest.raises(ValueError):
            await adapter.complete("ar_request", "approve", ceremony.ceremony_id, credential, hub.origin, review.digest)
        assert not any(path.endswith("/approve") for _, path, _ in hub.calls)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_cancelled_approval_is_uncertain_and_never_automatically_reposts(adapter_case):
    hub, _, adapter = adapter_case
    entered = asyncio.Event()
    async def silent(_):
        entered.set(); await asyncio.Event().wait()
    hub.overrides["requests/ar_request/approve"] = silent
    try:
        review, ceremony = await begin(hub, adapter)
        task = asyncio.create_task(adapter.complete("ar_request", "approve", ceremony.ceremony_id,
                                                  hub.assertion(), hub.origin, review.digest))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        with pytest.raises(AdapterError):
            await adapter.complete("ar_request", "approve", ceremony.ceremony_id, hub.assertion(), hub.origin, review.digest)
        assert adapter.states["ar_request"].uncertain
        assert len([path for _, path, _ in hub.calls if path.endswith("/approve")]) == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_a_profile_change_during_a_read_cannot_cache_an_obsolete_review(adapter_case):
    hub, profile, adapter = adapter_case
    original = hub.handle
    async def changed(request):
        response = await original(request)
        if request.url.path.endswith("/review"):profile.token = "replacement-token"
        return response
    adapter.transport.client._transport = httpx.MockTransport(changed)
    try:
        with pytest.raises(AdapterError, match="route changed"):await adapter.review("ar_request")
        assert not adapter.states["ar_request"].canonical
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["url_token", "pin"])
async def test_a_profile_change_in_the_scheduling_gap_sends_no_credentials(adapter_case, monkeypatch, change):
    """A mutable profile must not pair its new bearer with a captured old destination."""
    hub, profile, adapter = adapter_case
    sent = []
    if change == "pin":
        # A scoped synthetic route-file generation, without reading/writing
        # any certificate or machine peer registry.
        observed = ["old-pin"]
        original = adapter.transport.identity
        monkeypatch.setattr("collab.client.passkey_adapter.routes.generation", lambda _: observed[0])
        adapter.transport.identity = (original[0], original[1], "old-pin")

    async def observe(request):
        sent.append((str(request.url), request.headers.get("authorization")))
        return reply({"api_version": 1, "available": False, "methods": []})

    adapter.transport.client._transport = httpx.MockTransport(observe)
    # Python versions differ in whether wait_for schedules its awaitable or
    # runs it inline. Hold precisely that admission boundary on either host.
    entered = asyncio.Event()
    release = asyncio.Event()
    original_wait_for = asyncio.wait_for
    async def paused_wait_for(awaitable, timeout):
        entered.set()
        await release.wait()
        return await original_wait_for(awaitable, timeout)
    monkeypatch.setattr("collab.client.passkey_adapter.asyncio.wait_for", paused_wait_for)
    try:
        task = asyncio.create_task(adapter.transport.request("GET", "capabilities"))
        await entered.wait()
        if change == "pin":observed[0] = "new-pin"
        else:
            profile.url = "https://different-owned.example"
            profile.token = "synthetic-new-token"
        release.set()
        with pytest.raises(AdapterError, match="route changed"):await task
        assert sent == []
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_the_whole_review_has_one_budget_including_its_three_network_reads(adapter_case, monkeypatch):
    """Per-read timeouts alone let several slow phases multiply the page deadline."""
    hub, _, adapter = adapter_case
    original = hub.handle
    async def slow(request):
        await asyncio.sleep(.03)
        return await original(request)
    adapter.transport.client._transport = httpx.MockTransport(slow)
    monkeypatch.setattr("collab.client.passkey_adapter.SECONDS", .06)
    try:
        with pytest.raises(AdapterError):await adapter.review("ar_request")
        assert not adapter.states["ar_request"].canonical
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_a_full_core_challenge_is_locally_capped_by_a_nearly_expired_request(adapter_case, monkeypatch):
    """Core8257 signs a120s challenge; a30s operation remains usable only for those30s."""
    hub, profile, adapter = adapter_case
    await adapter.close()
    hub.binding = replace(hub.binding, expires_at=hub.now + 30)
    adapter = PasskeyHubAdapter(profile, credential_id=hub.credential_id, bindings=[hub.binding],
                               transport=httpx.MockTransport(hub.handle))
    hub.overrides["requests/ar_request/challenge"] = {"api_version": 1, "request_id": "ar_request",
        "challenge_id": "ac_short_request", "credential_id": hub.credential_id, "method": "passkey",
        "challenge": hub.challenge, "expires_at": hub.now + 120, "digest": hub.binding.digest,
        "origin": hub.origin, "rp_id": "localhost"}
    try:
        review, ceremony = await begin(hub, adapter)
        assert ceremony.expires == hub.now + 30
        assert 0 < ceremony.public_key["timeout"] <= 30_000
        assert ceremony.public_key["challenge"] == hub.challenge
        monkeypatch.setattr("collab.client.passkey_adapter.time.time", lambda: hub.now + 30)
        with pytest.raises(AdapterError):
            await adapter.complete("ar_request", "approve", ceremony.ceremony_id,
                                   hub.assertion(), hub.origin, review.digest)
        assert not any(path.endswith("/approve") for _, path, _ in hub.calls)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["encoded", "flood", "duplicate", "nonfinite", "deep", "silent"])
async def test_the_shared_transport_bounds_untrusted_json_and_total_exchange(adapter_case, monkeypatch, mode):
    hub, _, adapter = adapter_case
    iterations = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                iterations.append(True);yield b"x" * 65536
    async def hostile(_):
        if mode == "silent":await asyncio.Event().wait()
        if mode in ("encoded", "flood"):
            return httpx.Response(200, stream=Stream(), headers={"content-type": "application/json",
                **({"content-encoding": "gzip"} if mode == "encoded" else {})})
        raw = {'duplicate':b'{"api_version":1,"api_version":1}',
               'nonfinite':b'{"api_version":1,"value":NaN}',
               'deep':b'{"api_version":1,"value":'+b'['*20+b'0'+b']'*20+b'}'}[mode]
        return reply(None, raw=raw)
    hub.overrides["capabilities"] = hostile
    monkeypatch.setattr("collab.client.passkey_adapter.SECONDS", .05)
    try:
        with pytest.raises(AdapterError):await adapter.transport.request("GET", "capabilities")
        if mode == "encoded":assert not iterations
        if mode == "flood":assert len(iterations) == 5
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_bindings_do_not_infer_expiry_or_identity_from_labels(adapter_case):
    hub, _, adapter = adapter_case
    try:
        with pytest.raises(AdapterError):replace(hub.binding, expires_at=0).validate()
        with pytest.raises(AdapterError):replace(hub.binding, digest=hub.binding.digest.upper()).validate()
        with pytest.raises(AdapterError):replace(hub.binding, source_seq=True).validate()
        with pytest.raises(AdapterError):replace(hub.binding, target_id="target-display-name").validate()
    finally:
        await adapter.close()


@pytest.mark.parametrize("mode", ["flood", "silence"])
def test_fresh_process_transport_budget_is_measured_under_a_hostile_exchange(tmp_path, mode):
    """Measure bounded synthetic I/O with post-exec process memory, never inherited ru_maxrss."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    if not Path("/proc/self/status").exists():
        pytest.skip("Linux post-exec memory is unavailable")
    code = r'''
import asyncio,json,os,sys,time
from pathlib import Path
from tempfile import TemporaryDirectory
with TemporaryDirectory(prefix='owned-approval-budget-') as home:
 os.environ.update(HOME=home,COLLAB_CONFIG=home+'/config',COLLAB_AGENT_CONFIG=home+'/agent',COLLAB_HOME=home+'/state',COLLAB_PEERS_DIR=home+'/peers',COLLAB_NO_WORKER='1')
 import httpx
 from collab.config import SessionProfile
 from collab.client import passkey_adapter as adapter
 def memory():
  return {r.split(':')[0]:int(r.split()[1]) for r in Path('/proc/self/status').read_text().splitlines() if r.startswith(('VmRSS:','VmHWM:'))}
 async def run():
  adapter.SECONDS=.05;reads=[];closed=[];cancelled=[]
  class Flood(httpx.AsyncByteStream):
   async def __aiter__(self):
    for _ in range(100):reads.append(True);yield b'x'*65536
   async def aclose(self):closed.append(True)
  async def hostile(request):
   if sys.argv[1]=='silence':
    try:await asyncio.Event().wait()
    finally:cancelled.append(True)
   return httpx.Response(200,headers={'content-type':'application/json'},stream=Flood())
  profile=SessionProfile(session_id='s_test',participant_id='p_target',name='target',host_name='host',token='synthetic',url='https://owned.example',home=home)
  profile.dir.mkdir(parents=True,exist_ok=True)
  transport=adapter.ApprovalTransport(profile,transport=httpx.MockTransport(hostile))
  before=memory();start=time.monotonic();cpu=time.process_time();refused=False
  try:await transport.request('GET','capabilities')
  except adapter.AdapterError:refused=True
  after=memory();result={'mode':sys.argv[1],'refused':refused,'wall_seconds':time.monotonic()-start,'cpu_seconds':time.process_time()-cpu,'before_kib':before,'after_kib':after,'chunks':len(reads),'closed':bool(closed),'cancelled':bool(cancelled)}
  await transport.close();print(json.dumps(result))
 asyncio.run(run())
'''
    done = subprocess.run([sys.executable, "-c", code, mode], capture_output=True, text=True, timeout=5,
        env={**os.environ, "COLLAB_NO_WORKER": "1", "PYTHONNOUSERSITE": "1",
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    print(json.dumps(result, sort_keys=True))
    assert result["refused"] and result["wall_seconds"] < 1 and result["cpu_seconds"] < .5
    assert result["after_kib"]["VmHWM"] - result["before_kib"]["VmHWM"] < 8*1024
    assert result["after_kib"]["VmHWM"] < 128*1024
    if mode == "flood":assert result["chunks"] == 5 and result["closed"]
    else:assert result["cancelled"]
