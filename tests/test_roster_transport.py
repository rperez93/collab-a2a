"""Save roster traffic without making a changed or private roster disappear."""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time

import pytest
from starlette.requests import Request

from collab.protocol import EXT_PREFIX, Envelope
from collab.server import roster_transport as transport


def request(**headers):
    return Request({"type": "http", "method": "GET", "path": "/participants",
                    "headers": [(key.replace("_", "-").encode(), value.encode())
                                for key, value in headers.items()]})


def test_only_the_server_clock_can_change_without_invalidating_the_tag(client, host_headers):
    """A timer alone must not retransmit the whole task board every three seconds."""
    first = client.get(f"{EXT_PREFIX}/participants", headers=host_headers)
    assert first.status_code == 200
    tag = first.headers["etag"]
    assert tag.startswith('W/"')
    second = client.get(f"{EXT_PREFIX}/participants",
                        headers={**host_headers, "If-None-Match": tag})
    assert second.status_code == 304 and second.content == b""
    assert second.headers["etag"] == tag
    assert second.headers["cache-control"] == "private, no-cache"
    assert set(second.headers["vary"].split(", ")) == {"Authorization", "Accept-Encoding"}
    unconditional = client.get(f"{EXT_PREFIX}/participants", headers=host_headers)
    assert unconditional.status_code == 200
    assert unconditional.json()["server_time"] >= first.json()["server_time"]


@pytest.mark.parametrize("change", ["last_seen", "connectivity", "seq", "task", "identity"])
def test_every_visible_roster_change_invalidates_the_tag(client, session, host_headers, change):
    """Keep liveness, chat counts and task state current even without a presence event."""
    first = client.get(f"{EXT_PREFIX}/participants", headers=host_headers)
    person = session["store"].participants()[0]
    if change == "last_seen":
        session["store"].touch(person.id)
    elif change == "connectivity":
        session["app"].state.hub._subs[person.id] = [object()]
    elif change == "seq":
        session["store"].append(Envelope(kind="chat", sender="alice", sender_id=person.id,
                                        text="a new message"))
    elif change == "task":
        made = client.post(f"{EXT_PREFIX}/tasks", headers=host_headers,
                           json={"action": "propose", "title": "new work"})
        assert made.status_code == 200, made.text
    else:
        session["store"].rename(person.id, "renamed")
    response = client.get(f"{EXT_PREFIX}/participants",
                          headers={**host_headers, "If-None-Match": first.headers["etag"]})
    assert response.status_code == 200
    assert response.headers["etag"] != first.headers["etag"]


def test_tags_are_viewer_specific_and_never_skip_authentication(client, session, host_headers):
    """A validator from Alice must not make Bob reuse Alice's identity or private feed."""
    bob = session["store"].add_participant("bob", "bob-token")
    first = client.get(f"{EXT_PREFIX}/participants", headers=host_headers)
    tag = first.headers["etag"]
    headers = {**host_headers, "Authorization": "Bearer bob-token", "If-None-Match": tag}
    other = client.get(f"{EXT_PREFIX}/participants", headers=headers)
    assert other.status_code == 200 and other.json()["you_id"] == bob.id
    assert other.headers["etag"] != tag
    denied = client.get(f"{EXT_PREFIX}/participants", headers={"If-None-Match": tag})
    assert denied.status_code == 401


def test_weak_and_strong_validator_lists_follow_get_comparison_rules(client, host_headers):
    """Proxy-added validators must not force retransmission of an unchanged roster."""
    first = client.get(f"{EXT_PREFIX}/participants", headers=host_headers)
    tag = first.headers["etag"]
    for offer in (tag, tag[2:], '"unrelated", ' + tag, "*"):
        result = client.get(f"{EXT_PREFIX}/participants",
                            headers={**host_headers, "If-None-Match": offer})
        assert result.status_code == 304


@pytest.mark.parametrize("accept,compressed", [
    ("gzip", True), ("br, gzip", True), ("GZip; q=0.5", True),
    ("identity", False), ("gzip;q=0", False), ("*;q=1,gzip;q=0", False),
    ("gzip;q=0.5,identity;q=1", False), ("gzip;q=1,identity;q=0", True),
    ("*;q=0.3", True), ("notgzip", False), ("gzip;q=nan", False),
    ("gzip;q=invalid", False), ("", False),
])
def test_compression_is_negotiated_and_never_implied_by_a_substring(accept, compressed):
    """Old identity-only clients and explicit q=0 refusals must still receive plain JSON."""
    payload = {"participants": [{"name": "alice", "activity": "checking the task board"}] * 50}
    response = transport.roster_response(payload, request(accept_encoding=accept))
    assert (response.headers.get("content-encoding") == "gzip") is compressed
    body = gzip.decompress(response.body) if compressed else response.body
    assert json.loads(body) == payload
    assert int(response.headers["content-length"]) == len(response.body)


def test_small_or_huge_responses_never_pay_compression_work(monkeypatch):
    """A tiny roster wastes CPU; a huge board must not cause an extra large gzip allocation."""
    def forbidden(*args, **kwargs):
        raise AssertionError("compression should not have run")
    monkeypatch.setattr(transport.gzip, "compress", forbidden)
    for payload in ({"participants": []}, {"detail": "x" * transport.MAX_GZIP_BYTES}):
        response = transport.roster_response(payload, request(accept_encoding="gzip"))
        assert "content-encoding" not in response.headers


@pytest.mark.parametrize("offer", ["identity;q=0,gzip;q=0", "*;q=0",
                                  "*;q=1,identity;q=0,gzip;q=0"])
def test_refusing_every_available_encoding_returns_an_empty_406(offer):
    """An identity refusal cannot silently become a plain JSON response."""
    response = transport.roster_response({"detail": "x" * 2000},
                                         request(accept_encoding=offer))
    assert response.status_code == 406 and response.body == b""


@pytest.mark.parametrize("payload", [{"participants": []},
                                    {"detail": "x" * transport.MAX_GZIP_BYTES}])
def test_size_caps_do_not_send_identity_when_only_gzip_is_allowed(payload):
    """Compression limits still apply when the peer forbids the fallback encoding."""
    response = transport.roster_response(payload,
        request(accept_encoding="gzip,identity;q=0"))
    assert response.status_code == 406 and response.body == b""


def test_explicit_identity_permission_overrides_a_zero_quality_wildcard():
    """The wildcard default must not override an explicit acceptable encoding."""
    response = transport.roster_response({"participants": []},
        request(accept_encoding="*;q=0,identity;q=1"))
    assert response.status_code == 200 and "content-encoding" not in response.headers


def test_both_finite_roster_routes_compress_and_ordinary_routes_do_not(client, session, host_headers):
    """Keep compression scoped to finite snapshots, away from streamed events and files."""
    for index in range(20):
        session["store"].add_participant(f"agent-{index}", f"token-{index}",
                                         meta={"focus": "checking a shared task board"})
    for path in ("participants", "snapshot"):
        result = client.get(f"{EXT_PREFIX}/{path}",
                            headers={**host_headers, "Accept-Encoding": "gzip"})
        assert result.status_code == 200
        assert result.headers["content-encoding"] == "gzip"
        assert len(result.json()["participants"]) == 21
    result = client.get(f"{EXT_PREFIX}/compatibility", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in result.headers


def test_real_http_preserves_compressed_bytes_and_delivers_sse_ready_immediately(live_server, host_headers):
    """Exercise raw wire bytes so automatic HTTP client decoding cannot fake a saving."""
    import httpx
    for index in range(20):
        live_server["store"].add_participant(f"agent-{index}", f"token-{index}")
    with httpx.Client(base_url=live_server["base"], timeout=3) as client:
        with client.stream("GET", f"{EXT_PREFIX}/participants",
                           headers={**host_headers, "Accept-Encoding": "gzip"}) as response:
            raw = b"".join(response.iter_raw())
            assert response.headers["content-encoding"] == "gzip"
            assert len(raw) == int(response.headers["content-length"])
            plain = gzip.decompress(raw)
            assert len(json.loads(plain)["participants"]) == 21
            assert len(raw) < len(plain) / 3
            tag = response.headers["etag"]
        with client.stream("GET", f"{EXT_PREFIX}/participants",
                           headers={**host_headers, "If-None-Match": tag}) as response:
            assert response.status_code == 304
            assert b"".join(response.iter_raw()) == b""
        started = time.monotonic()
        with client.stream("GET", f"{EXT_PREFIX}/events",
                           headers={**host_headers, "Accept-Encoding": "gzip"}) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "content-encoding" not in response.headers
            assert b"event: ready" in next(response.iter_raw())
            assert time.monotonic() - started < 2


# Each variant starts in its own interpreter. A warm pytest process can already
# have a larger high-water mark than either variant, making RSS deltas read zero
# while a request still allocates megabytes. Absolute child peak RSS measures
# imports, payload construction and all repeated requests instead.
_MEASURE_TRANSPORT = r"""
import json
from pathlib import Path
import resource
import sys
import time
from starlette.requests import Request
from starlette.responses import JSONResponse
from collab.server.roster_transport import roster_response

board, changing, variant = sys.argv[1:]
changing = changing == "changing"
count = 100 if board == "representative" else 20
seed = "Measure requests, bytes, CPU and RSS before shipping."
detail = seed if board == "representative" else (seed * 160)[:8000]
task_count = 80 if board == "representative" else 250
payload = {
    "server_time": 123.0, "seq": 1, "you": "alice", "you_id": "p-alice",
    "participants": [
        {"name": f"agent-{i}", "connected": True,
         "focus": "measuring the shared task board",
         "stats": {"tokens_in": 12000, "tokens_out": 500}}
        for i in range(40)],
    "tasks": [
        {"id": f"T_{i}", "title": "Verify communication efficiency",
         "detail": detail, "state": "working", "owner": f"agent-{i % 40}"}
        for i in range(task_count)],
}
payload_bytes = len(JSONResponse(payload).body)
body_bytes = responses_200 = responses_304 = 0
tag = ""
started = time.process_time()
for index in range(count):
    payload["server_time"] += 1
    if changing:
        payload["seq"] += 1
        payload["tasks"][index % task_count]["detail"] = detail[:7997] + str(index)
    if variant == "baseline":
        response = JSONResponse(payload)
    else:
        headers = [(b"accept-encoding", b"gzip")]
        if tag:
            headers.append((b"if-none-match", tag.encode()))
        request = Request({"type": "http", "method": "GET", "path": "/participants",
                           "headers": headers})
        response = roster_response(payload, request, conditional=True)
        tag = response.headers["etag"]
    body_bytes += len(response.body)
    responses_200 += response.status_code == 200
    responses_304 += response.status_code == 304
cpu = time.process_time() - started
if sys.platform.startswith("linux"):
    # getrusage can include the launcher's fork-time high water before exec;
    # VmHWM belongs to this interpreter's current address space instead.
    rss = int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                   if line.startswith("VmHWM:")))
else:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform == "darwin":
    rss //= 1024
print(json.dumps({"board": board, "state": "changing" if changing else "unchanged",
                  "variant": variant, "requests": count, "payload_bytes": payload_bytes,
                  "body_bytes": body_bytes, "responses_200": responses_200,
                  "responses_304": responses_304, "cpu_seconds": cpu,
                  "peak_rss_kib": rss}))
"""


def measure_transport(board, state, variant):
    """Bound the benchmark process itself so a regression cannot hang the suite."""
    done = subprocess.run([sys.executable, "-c", _MEASURE_TRANSPORT, board, state, variant],
                          capture_output=True, text=True, timeout=15, check=True)
    return json.loads(done.stdout)


@pytest.mark.parametrize("board", ["representative", "near_2mib"])
@pytest.mark.parametrize("state", ["unchanged", "changing"])
def test_roster_transport_savings_and_resources_in_fresh_processes(board, state):
    """Measure full-body savings and absolute CPU/RSS even when every board changes.

    Each pair renders 100 representative boards or 20 boards near the 2 MiB
    compression ceiling. Changing traffic increments seq and changes a task
    detail every request, forcing hashing, rendering and gzip instead of 304.
    The generous 5 s CPU / 96 MiB absolute RSS limits catch retained payloads
    and expensive compression without depending on a warm parent's RSS peak.
    """
    baseline = measure_transport(board, state, "baseline")
    optimized = measure_transport(board, state, "optimized")
    print("roster isolated benchmark " + json.dumps([baseline, optimized], sort_keys=True))
    assert baseline["requests"] == optimized["requests"]
    if board == "near_2mib":
        assert 1.5 * 1024 * 1024 < optimized["payload_bytes"] < 2 * 1024 * 1024
    if state == "unchanged":
        assert optimized["responses_200"] == 1
        assert optimized["responses_304"] == optimized["requests"] - 1
        assert optimized["body_bytes"] < baseline["body_bytes"] / 100
    else:
        assert optimized["responses_200"] == optimized["requests"]
        assert optimized["responses_304"] == 0
        assert optimized["body_bytes"] < baseline["body_bytes"] / 3
    for measured in (baseline, optimized):
        assert measured["cpu_seconds"] < 5
        assert measured["peak_rss_kib"] < 96 * 1024
