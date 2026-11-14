"""One loopback proxy, one tunnel, every session on the machine behind a path.

The fault it removes: each hub ran its own ngrok, a free account has one
endpoint, and the second session on a machine went local-only with
ERR_NGROK_334 (2026-11-13). These tests drive real processes — a hub, the
proxy, a client — because the property that matters is on the wire: that a
client handed `https://<domain>/s/<id>#CODE` reaches that session's hub and
nothing else.
"""

from __future__ import annotations

import os
import resource
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from collab import peers, proxy

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    return tmp_path / "peers"


def _announce(session_id: str, local_url: str, *, role: str = "host",
              pid: int | None = None) -> None:
    peers.announce(session_id=session_id, name="h", role=role, url=local_url,
                   repo="/r", home="/h", local_url=local_url, pid=pid or os.getpid())


# ── routing rules, no network ────────────────────────────────────────────────

def test_a_routed_path_names_its_session_and_keeps_the_rest():
    assert proxy.split_target("/s/s_abc/ext/collab/v1/events", "") == (
        "s_abc", "/ext/collab/v1/events")
    assert proxy.split_target("/s/s_abc", "") == ("s_abc", "/")


def test_a_bare_path_goes_to_the_primary_and_to_nobody_without_one():
    """Every link handed out before the proxy is a bare-domain link."""
    assert proxy.split_target("/a2a", "s_old") == ("s_old", "/a2a")
    assert proxy.split_target("/a2a", "") is None
    assert proxy.split_target("/s/", "s_old") is None


def test_only_live_loopback_host_records_are_routes(registry):
    """A guest's record names a hub elsewhere, and a non-loopback address in a
    file would make the proxy a relay to wherever the file said."""
    _announce("s_host", "http://127.0.0.1:41000")
    _announce("s_guest", "http://127.0.0.1:41001", role="guest")
    _announce("s_lan", "http://192.168.1.50:41002")

    assert proxy.Routes(ttl=0).table() == {"s_host": "http://127.0.0.1:41000"}


def test_a_host_without_a_tunnel_is_routed_by_its_shared_loopback_url(registry):
    """2.1.2 with no tunnel leaves `local_url` empty: its `url` IS the loopback
    address. Found live — the session was a 404 behind the proxy."""
    peers.announce(session_id="s_old", name="h", role="host",
                   url="http://127.0.0.1:40920", repo="/r", home="/h", pid=os.getpid())
    peers.announce(session_id="s_pub", name="h", role="host",
                   url="https://x.ngrok.app", repo="/r", home="/h", pid=os.getpid())
    assert proxy.Routes(ttl=0).table() == {"s_old": "http://127.0.0.1:40920"}


def test_hop_by_hop_headers_never_cross(registry):
    out = proxy.forward_headers({"Connection": "keep-alive", "Host": "x",
                                 "Authorization": "Bearer t", "TE": "trailers"},
                                "1.2.3.4", "https")
    assert "Authorization" in out, "the hub must still see the bearer token"
    assert not {"Connection", "Host", "TE"} & set(out)
    assert out["x-forwarded-for"] == "1.2.3.4"


# ── the wire: a real upstream, the real proxy app, a real client ─────────────

UPSTREAM = r'''
import sys, asyncio, gzip
from starlette.applications import Starlette
from starlette.responses import StreamingResponse, Response, JSONResponse
from starlette.routing import Route
import uvicorn

async def events(request):
    async def gen():
        for i in range(3):
            yield f"data: {i}\n\n".encode()
            await asyncio.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream")

async def echo(request):
    body = await request.body()
    return JSONResponse({"path": request.url.path, "raw": request.scope["raw_path"].decode(),
                         "query": request.scope["query_string"].decode(),
                         "len": len(body), "auth": request.headers.get("authorization", ""),
                         "fwd": request.headers.get("x-forwarded-for", "")})

async def zipped(request):
    data = gzip.compress(b"x" * 10000)
    return Response(data, headers={"content-encoding": "gzip"})

async def forever(request):
    async def gen():
        while True:
            yield b"data: tick\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")

app = Starlette(routes=[Route("/ext/events", events), Route("/ext/forever", forever),
                        Route("/zipped", zipped),
                        Route("/{p:path}", echo, methods=["GET", "POST"])])
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
'''


def _wait_port(port: int, deadline: float = 10.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"nothing listening on {port}")


@pytest.fixture
def upstream(tmp_path):
    port = _free_port()
    script = tmp_path / "upstream.py"
    script.write_text(UPSTREAM)
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        _wait_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def proxied(registry, upstream, tmp_path):
    """The proxy process itself, with no tunnel, routing to `upstream`."""
    _announce("s_one", upstream)
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": SRC, "COLLAB_PEERS_DIR": str(registry),
           "COLLAB_CONFIG": str(tmp_path / "cfg.json")}
    proc = subprocess.Popen([sys.executable, "-m", "collab.proxy_main", "--no-tunnel",
                             "--port", str(port), "--idle-exit", "3"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_port(port)
        yield f"http://127.0.0.1:{port}", proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_a_request_reaches_its_session_with_path_query_body_and_token(proxied):
    base, _ = proxied
    r = httpx.post(f"{base}/s/s_one/a2a?x=1", content=b"y" * 5000,
                   headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert r.json() == {"path": "/a2a", "raw": "/a2a", "query": "x=1", "len": 5000,
                        "auth": "Bearer tok", "fwd": "127.0.0.1"}


def test_an_unknown_session_is_a_404_not_a_guess(proxied):
    base, _ = proxied
    assert httpx.get(f"{base}/s/s_nobody/a2a").status_code == 404
    assert httpx.get(f"{base}/a2a").status_code == 404, "no primary: bare paths go nowhere"


def test_the_event_stream_arrives_event_by_event_not_at_the_end(proxied):
    """Buffering would hold every event until the stream closed — a feed that
    delivers nothing while the session is alive."""
    base, _ = proxied
    arrivals = []
    started = time.monotonic()
    with httpx.stream("GET", f"{base}/s/s_one/ext/events", timeout=10) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                arrivals.append(time.monotonic() - started)
    assert len(arrivals) == 3
    assert arrivals[0] < 0.25, f"first event held back: {arrivals}"
    assert arrivals[2] - arrivals[0] > 0.45, f"events bunched: {arrivals}"


def test_a_compressed_body_passes_through_undecoded(proxied):
    base, _ = proxied
    r = httpx.get(f"{base}/s/s_one/zipped")
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == b"x" * 10000


def test_a_second_proxy_defers_to_the_first(proxied, registry, tmp_path):
    env = {**os.environ, "PYTHONPATH": SRC, "COLLAB_PEERS_DIR": str(registry),
           "COLLAB_CONFIG": str(tmp_path / "cfg.json")}
    out = subprocess.run([sys.executable, "-m", "collab.proxy_main", "--no-tunnel"],
                         env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0
    assert "already running" in out.stdout


def test_the_proxy_leaves_once_no_session_is_behind_it(proxied, registry):
    """A proxy with nothing to serve must not keep holding the account's one
    ngrok endpoint."""
    _, proc = proxied
    peers.withdraw("s_one", pid=os.getpid())
    assert proc.wait(timeout=15) == 0
    assert proxy.ProxyState.load().pid == 0, "a dead proxy's pid must not be left behind"


def test_a_one_mebibyte_upload_costs_the_proxy_little_memory(proxied):
    """Measured, per CLAUDE.md: bodies are streamed, so the proxy's resident
    size must not grow by the size of what passes through it."""
    base, proc = proxied

    def rss_kb(pid: int) -> int:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
        return 0

    if not Path(f"/proc/{proc.pid}/status").exists():
        pytest.skip("no /proc here")
    httpx.post(f"{base}/s/s_one/a2a", content=b"z" * 1024)        # warm
    before = rss_kb(proc.pid)
    for _ in range(8):
        r = httpx.post(f"{base}/s/s_one/a2a", content=b"z" * (1 << 20))
        assert r.json()["len"] == 1 << 20
    grown = rss_kb(proc.pid) - before
    assert grown < 6 * 1024, f"proxy RSS grew {grown} KB over 8 MiB of uploads"


# ── review of #111 ───────────────────────────────────────────────────────────

def test_the_proxy_state_survives_a_pruning_read_of_the_registry(registry):
    """It lived beside the records as `proxy.json`, which `discover(prune=True)`
    deletes once it is older than STALE_AFTER — 90 s after start."""
    state = proxy.ProxyState(primary="s_abc", pid=1)
    state.save()
    old = time.time() - 600
    os.utime(proxy.state_path(), (old, old))
    peers.discover()
    assert proxy.ProxyState.load().primary == "s_abc"


def test_a_stale_or_foreign_host_record_is_never_a_route(registry, tmp_path):
    """A dead hub's port may be reused by anything; a record from another
    machine names a port on that machine, not this one."""
    import json as jsonlib

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _announce("s_dead", "http://127.0.0.1:41010", pid=dead.pid)
    _announce("s_far", "http://127.0.0.1:41011")
    far = next(registry.glob("s_far-*.json"))
    data = jsonlib.loads(far.read_text())
    data["machine_id"] = "m_somewhere_else"
    far.write_text(jsonlib.dumps(data))

    assert proxy.Routes(ttl=0).table() == {}


@pytest.mark.parametrize("raw", ["/s/s_one/../../etc/passwd", "/s/s_one/./a2a"])
def test_dot_segments_are_refused_not_resolved(proxied, raw):
    """`/s/<id>/../../etc/passwd` reached the hub as `/etc/passwd`."""
    base, _ = proxied
    with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1]))) as s:
        s.sendall(f"GET {raw} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        head = s.recv(200).decode("latin-1")
    assert " 400 " in head.splitlines()[0]


def test_the_hub_receives_the_path_exactly_as_it_was_sent(proxied):
    """Decoded, `%3F` became a query and `%2F` a separator."""
    base, _ = proxied
    r = httpx.get(f"{base}/s/s_one/ext/x%3Fy=1%2Fz?q=2")
    assert r.json()["raw"] == "/ext/x%3Fy=1%2Fz"
    assert r.json()["query"] == "q=2"


def test_many_open_feeds_do_not_starve_an_ordinary_request(proxied):
    """httpx's shared default of 100 connections: 105 open SSE feeds across
    all sessions made every further request wait 10 s and fail 502."""
    base, _ = proxied
    held = []
    try:
        for _ in range(110):
            s = socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])))
            # A feed that NEVER ends: one that closed after a second let the
            # request simply wait its turn, and the test passed with httpx's
            # default pool of 100 restored.
            s.sendall(b"GET /s/s_one/ext/forever HTTP/1.1\r\nHost: x\r\n\r\n")
            held.append(s)
        time.sleep(0.5)
        started = time.monotonic()
        r = httpx.get(f"{base}/s/s_one/a2a", timeout=8)
        assert r.status_code == 200
        assert time.monotonic() - started < 3
    finally:
        for s in held:
            s.close()


def test_an_orphaned_ngrok_is_stopped_by_its_stamp_and_only_then(registry):
    """A SIGKILLed proxy leaves its ngrok holding the account's endpoint and
    forwarding the domain to a port anything may take next."""
    from collab import proxy_main
    from collab.client import exclusive

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        state = proxy.ProxyState(ngrok_pid=child.pid,
                                 ngrok_stamp=exclusive.stamp_for(child.pid).encode())
        # a stamp for another process with the same number must not be obeyed
        forged = proxy.ProxyState(ngrok_pid=child.pid, ngrok_stamp=exclusive.Stamp(
            pid=child.pid, started="1", boot="other-boot").encode())
        assert proxy_main._stop_orphaned_ngrok(forged) is False
        assert child.poll() is None
        assert proxy_main._stop_orphaned_ngrok(state) is True
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_a_clean_exit_leaves_no_pids_behind(proxied, registry):
    _, proc = proxied
    peers.withdraw("s_one", pid=os.getpid())
    assert proc.wait(timeout=15) == 0
    left = proxy.ProxyState.load()
    assert (left.pid, left.ngrok_pid, left.stamp, left.ngrok_stamp) == (0, 0, "", "")


# ── in-process proxy, for the timing of a stalled body ───────────────────────

@pytest.fixture
def inproc(registry, upstream, monkeypatch):
    """The proxy app in this process, so BODY_IDLE and BODY_GRACE can be set
    to fractions of a second instead of half a minute."""
    import threading

    import uvicorn

    monkeypatch.setattr(proxy, "BODY_IDLE", 1.0)
    monkeypatch.setattr(proxy, "BODY_GRACE", 1.0)
    monkeypatch.setattr(proxy, "BODY_MIN_RATE", 1000.0)
    _announce("s_one", upstream)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(proxy.build_app(proxy.Routes(ttl=0)),
                                           host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _post_head(port: int, length: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"POST /s/s_one/a2a HTTP/1.1\r\nHost: x\r\nContent-Length: {length}"
              "\r\nConnection: close\r\n\r\n".encode())
    return s


def test_a_body_that_stops_arriving_is_cut_with_408(inproc):
    s = _post_head(inproc, 1000)
    s.sendall(b"x" * 10)
    s.settimeout(5)
    assert b" 408 " in s.recv(200)


def test_a_body_dripping_below_the_minimum_rate_is_cut_with_408(inproc):
    """One byte just inside BODY_IDLE reset the idle timer for ever."""
    s = _post_head(inproc, 1000)
    s.settimeout(0.05)
    started = time.monotonic()
    head = b""
    while time.monotonic() - started < 6 and not head:
        s.sendall(b"x")
        try:
            head = s.recv(200)
        except socket.timeout:
            time.sleep(0.3)
    assert b" 408 " in head


def test_a_stamped_process_that_is_not_a_group_leader_is_left_alone(registry):
    """The recorded ngrok was started in its own session; anything else with
    that stamp is not ours to signal as a group."""
    from collab import proxy_main
    from collab.client import exclusive

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state = proxy.ProxyState(ngrok_pid=child.pid,
                                 ngrok_stamp=exclusive.stamp_for(child.pid).encode())
        assert proxy_main._stop_orphaned_ngrok(state) is False
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def _fake_ngrok(tmp_path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "ngrok"
    exe.write_text('#!/bin/sh\necho \'t=1 lvl=info msg="started tunnel" url=https://fake.ngrok.app\'\n'
                   "exec sleep 300\n")
    exe.chmod(0o755)
    return bin_dir


def test_the_proxy_stops_an_orphan_at_start_and_clears_its_own_on_exit(registry, upstream, tmp_path):
    """End to end through `collab.proxy_main`: a stamped orphan from a proxy
    that died hard is ended at start; the ngrok this proxy starts is recorded
    while it runs and is gone, with its record, after a clean exit."""
    from collab.client import exclusive

    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              start_new_session=True)
    proxy.ProxyState(ngrok_pid=orphan.pid,
                     ngrok_stamp=exclusive.stamp_for(orphan.pid).encode()).save()
    _announce("s_one", upstream)
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": SRC, "COLLAB_PEERS_DIR": str(registry),
           "COLLAB_CONFIG": str(tmp_path / "cfg.json"),
           "PATH": f"{_fake_ngrok(tmp_path)}:{os.environ['PATH']}"}
    proc = subprocess.Popen([sys.executable, "-m", "collab.proxy_main", "--port", str(port),
                             "--idle-exit", "2"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_port(port, deadline=30)
        assert orphan.wait(timeout=10) is not None, "the orphan was not stopped"
        running = proxy.ProxyState.load()
        assert running.ngrok_pid > 0 and running.ngrok_stamp
        ngrok = exclusive.decode(running.ngrok_stamp)
        peers.withdraw("s_one", pid=os.getpid())
        assert proc.wait(timeout=30) == 0
        left = proxy.ProxyState.load()
        assert (left.ngrok_pid, left.ngrok_stamp) == (0, "")
        assert not ngrok.alive(), "the proxy left its own ngrok running"
    finally:
        for p in (orphan, proc):
            if p.poll() is None:
                p.kill()
