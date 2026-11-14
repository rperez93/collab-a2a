"""The original visible URL establishes authority before any automatic route."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from collab import peers, routes
from collab.client import onboard
from collab.client.daemon import Daemon
from collab.server.local_tls import read_certificate, server_context
from collab.server.session import HubConfig


@pytest.mark.parametrize("encrypted", [False, True])
async def test_a_forged_destination_with_its_own_certificate_never_receives_credentials(
        live_server, tmp_path, monkeypatch, encrypted):
    """A valid attacker-controlled pin in a query cannot redirect the first join POST."""
    captured = []
    context = None
    pem = ""
    if encrypted:
        context = await asyncio.to_thread(server_context, tmp_path / "evil", "127.0.0.1")
        assert context is not None
        pem = read_certificate(tmp_path / "evil")
    async def malicious(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        body = await reader.readexactly(length) if length else b""
        captured.append(headers + body)
        path = headers.split(b" ")[1]
        if path.endswith(b"/compatibility"):
            value = {"protocol_major": 2, "version": "2.1.2"}
        elif path.endswith(b"/health"):
            value = {"session_id": "s_test"}
        else:
            value = {"session_id": "s_test", "name": "stolen", "token": "evil-token",
                     "id": "p_evil", "snapshot": {}}
        raw = json.dumps(value).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(raw)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(malicious, "127.0.0.1", 0, ssl=context)
    candidate = ("https" if encrypted else "http") + f"://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    original = live_server["base"]
    raw = original + "?" + urlencode({"collab_session": "s_test", "collab_local": candidate,
        "collab_cert": base64.urlsafe_b64encode(pem.encode()).decode()}) + "#" + live_server["invite"]
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "guest"))
    monkeypatch.setattr(routes, "private_records", lambda *args: [])
    try:
        profile, _, _ = await asyncio.to_thread(onboard.join_session, raw,
            name="real-guest", start_daemon=False)
        assert profile.name == "real-guest" and profile.url == original
        assert routes.load(profile)["original_user_url"] == original
        assert captured == [], "even a credential-free probe would treat query data as a destination"
        assert len(live_server["store"].participants()) == 2
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("destination", ["https://evil.example", "https://8.8.8.8",
    "https://192.168.1.50", "https://localhost.evil.com", "http://127.0.0.1:9"])
def test_destination_hints_are_ignored_even_when_the_session_hint_is_valid(destination):
    """Neither DNS, public IP, LAN nor loopback query destinations gain pre-join authority."""
    raw = "https://original.example?" + urlencode({"collab_session": "s_expected",
        "collab_local": destination, "collab_cert": "attacker-certificate"}) + "#invite"
    assert routes.hints(raw) == {"session_id": "s_expected"}


@pytest.mark.parametrize("candidate", ["https://127.0.0.1:9", "https://192.168.1.50",
    "https://10.0.0.5", "https://172.16.1.5", "https://[::1]", "https://[fd12::123]"])
def test_authenticated_numeric_local_tls_candidates_keep_lan_support(candidate):
    """Explicitly bound private LAN and ULA entrances remain eligible after authentication."""
    assert routes.secure_local(candidate) == candidate


@pytest.mark.parametrize("candidate", ["https://evil.example", "https://8.8.8.8",
    "https://localhost", "https://localhost.evil.com", "http://192.168.1.50",
    "https://0.0.0.0", "https://[::]", "https://[2001:4860:4860::8888]"])
def test_discovered_secure_routes_cannot_be_arbitrary_public_or_dns_destinations(candidate):
    assert routes.secure_local(candidate) == ""


async def test_private_machine_route_is_selected_before_authenticated_tls_or_public(profile, tmp_path, monkeypatch):
    """Removing trusted_local preference must make this invariant fail."""
    directory = tmp_path / "peers"
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(directory))
    peers.announce(session_id=profile.session_id, name="host", role="host",
        url="https://public.example", local_url="http://127.0.0.1:12345",
        repo="/unused", home="/unused", invite="invite")
    routes.remember(profile, {"session_id": profile.session_id, "public_url": "https://public.example"},
                    original_url="https://original.example")
    tried = []
    async def healthy(candidate, *args, **kwargs):
        tried.append(candidate)
        return True
    monkeypatch.setattr(routes, "healthy", healthy)
    assert await routes.select(profile) == "http://127.0.0.1:12345"
    assert tried == ["http://127.0.0.1:12345"]


async def test_even_a_private_registry_does_not_authorize_plaintext_lan(profile, tmp_path, monkeypatch):
    """Deleting the loopback guard must not silently send a bearer across the LAN."""
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    peers.announce(session_id=profile.session_id, name="host", role="host",
        url="https://public.example", local_url="http://192.168.1.50:12345",
        repo="/unused", home="/unused", invite="invite")
    assert routes.trusted_local(profile.session_id, invite="invite") == ""
    assert routes.join_route("https://original.example#invite", "https://original.example", "invite")[0] \
        == "https://original.example"
    routes.remember(profile, {"session_id": profile.session_id}, original_url="https://original.example")
    tried = []
    async def healthy(candidate, *args, **kwargs):
        tried.append(candidate)
        return True
    monkeypatch.setattr(routes, "healthy", healthy)
    assert await routes.select(profile) == "https://original.example"
    assert tried == ["https://original.example"]


async def test_wrong_session_health_is_rejected_before_any_invite_or_bearer(live_server, monkeypatch):
    """A port reused by another session is not the requested session just because it answers."""
    assert not await routes.healthy(live_server["base"], "s_other", timeout=2)
    raw = live_server["base"] + "?collab_session=s_other#" + live_server["invite"]
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            pytest.fail("an invite-bearing client was constructed after wrong-session health")
    monkeypatch.setattr(onboard, "HubClient", ForbiddenClient)
    with pytest.raises(ValueError, match="no invite was submitted"):
        await asyncio.to_thread(onboard.join_session, raw, start_daemon=False)
    assert len(live_server["store"].participants()) == 1


async def test_legacy_profile_without_route_metadata_also_checks_session_health(profile, live_server):
    """An old profile is not permission to send its bearer to another hub at a reused address."""
    profile.url = live_server["base"]
    profile.session_id = "s_other"
    assert routes.load(profile) == {}
    assert await routes.select(profile) == ""
    profile.session_id = "s_test"
    assert await routes.select(profile) == live_server["base"]


def test_host_recovery_does_not_flip_its_local_profile_back_to_the_public_tunnel(profile):
    """Host reconnects must preserve their own listener even while the tunnel flaps."""
    cfg = HubConfig(session_id=profile.session_id, host_name="host", port=12345,
        bind="127.0.0.1", invite="invite", host_token="token", home=profile.home,
        public_url="https://public.example")
    cfg.save()
    daemon = Daemon(profile)
    try:
        for current in (cfg.local_url, cfg.public_url, cfg.local_url):
            profile.url = current
            daemon._follow_url_change()
            assert profile.url == cfg.local_url
    finally:
        daemon.inbox.close()


async def test_all_route_probes_share_a_finite_selection_budget(profile, monkeypatch):
    """Two silent public routes must not multiply the documented five-second budget."""
    routes.remember(profile, {"session_id": profile.session_id, "public_url": "https://new.example"},
                    original_url="https://original.example")
    monkeypatch.setattr(routes, "trusted_local", lambda *args: "http://127.0.0.1:9")
    monkeypatch.setattr(routes, "LOCAL_DEADLINE", .04)
    monkeypatch.setattr(routes, "PUBLIC_DEADLINE", .08)
    timeouts = []
    async def silent(candidate, *args, timeout, **kwargs):
        timeouts.append(timeout)
        await asyncio.sleep(timeout)
        return False
    monkeypatch.setattr(routes, "healthy", silent)
    began = asyncio.get_running_loop().time()
    assert await routes.select(profile) == ""
    assert asyncio.get_running_loop().time() - began < .2
    assert len(timeouts) == 3 and sum(timeouts) < .13


async def test_new_secure_metadata_replaces_a_public_daemon_pool(profile, tmp_path, monkeypatch):
    """A working public feed must return to local TLS when the hub advertises its restart."""
    from collab.client import daemon as module
    assert await asyncio.to_thread(server_context, tmp_path / "certificate", "127.0.0.1")
    pem = read_certificate(tmp_path / "certificate")
    original, local = "https://public.example", "https://127.0.0.1:12345"
    profile.url = original
    metadata = {"session_id": profile.session_id, "public_url": original}
    routes.remember(profile, metadata, original_url=original)
    daemon = Daemon(profile)
    token = profile.token
    entered, cancelled, restarted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    clients, options = [], []
    real_client = httpx.AsyncClient
    def handler(request):
        if request.url.path.endswith("/compatibility"):
            return httpx.Response(200, stream=httpx.ByteStream(
                json.dumps({"protocol_major": 2, "version": "2.1.2"}).encode()))
        assert request.headers["Authorization"] == "Bearer " + token
        return httpx.Response(200, json={"participants": [], "routes": dict(metadata)})
    def factory(*args, **kwargs):
        options.append(kwargs)
        result = real_client(*args, **kwargs, transport=httpx.MockTransport(handler))
        clients.append(result)
        return result
    async def healthy(*args, **kwargs):
        return True
    async def stream(client):
        if len(clients) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            assert profile.url == local
            restarted.set()
            daemon._stop.set()
    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(module, "BACKOFF_START", .001)
    monkeypatch.setattr(routes, "healthy", healthy)
    monkeypatch.setattr(routes, "trusted_local", lambda *args: "")
    monkeypatch.setattr(daemon, "_stream_once", stream)
    monkeypatch.setattr(daemon, "_revive_hub_if_host", lambda: None)
    monkeypatch.setattr(daemon, "_follow_url_change", lambda: None)
    task = asyncio.create_task(daemon._connect_forever())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        metadata.update(secure_local_url=local, local_certificate=pem)
        await daemon._refresh_snapshot(daemon._http)
        await asyncio.wait_for(restarted.wait(), 2)
        await asyncio.wait_for(task, 2)
        assert cancelled.is_set() and clients[0].is_closed
        assert len(clients) == 2 and profile.token == token
        assert "verify" not in options[0] and isinstance(options[1]["verify"], ssl.SSLContext)
    finally:
        daemon._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        daemon.inbox.close()


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_route_change_cancellation_finishes_the_already_recorded_local_delivery(
        profile, monkeypatch, cancellations):
    """Advancing the cursor before cancelling broadcast must not make an event invisible forever."""
    from contextlib import asynccontextmanager
    from collab.client import daemon as module
    from collab.protocol import Envelope
    daemon = Daemon(profile)
    event = Envelope(seq=1, kind="chat", sender="peer", text="deliver this exactly once")
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []
    async def broadcast(env):
        entered.set()
        await release.wait()
        delivered.append(env.seq)
    class Source:
        response = httpx.Response(200, request=httpx.Request("GET", "http://hub/events"))
        async def aiter_sse(self):
            yield SimpleNamespace(event="collab", data=json.dumps(event.to_dict()))
            await asyncio.Event().wait()
    @asynccontextmanager
    async def source(*args, **kwargs):
        yield Source()
    monkeypatch.setattr(module, "aconnect_sse", source)
    monkeypatch.setattr(daemon.bridge, "broadcast", broadcast)
    monkeypatch.setattr(daemon, "_schedule_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "write_status", lambda: None)
    task = asyncio.create_task(daemon._stream_once(object()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert daemon.inbox.last_seq() == 1
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert delivered == [event.seq]
        assert daemon.inbox.last_seq() == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        daemon.inbox.close()


def test_real_join_cli_prints_the_selected_route_without_starting_a_worker(live_server, tmp_path):
    """The human must see the actual destination, not infer it from the pasted link."""
    configuration = tmp_path / "guest-config.json"
    configuration.write_text(json.dumps({"worker_auto_start": False, "stats_auto_setup": False}))
    environment = dict(os.environ, COLLAB_CONFIG=str(configuration),
        COLLAB_HOME=str(tmp_path / "guest-home"), COLLAB_STATE_DIR=str(tmp_path / "guest-state"),
        COLLAB_PEERS_DIR=str(tmp_path / "guest-peers"), COLLAB_AGENT_ID="isolated-route-test")
    result = subprocess.run([sys.executable, "-m", "collab.cli", "join",
        live_server["base"] + "#" + live_server["invite"], "--name", "cli-guest", "--no-daemon"],
        env=environment, capture_output=True, text=True, timeout=10, check=True)
    assert "using route " + live_server["base"] in result.stdout
    assert "joined s_test as cli-guest" in result.stdout
    assert len(live_server["store"].participants()) == 2


def test_registry_work_cap_counts_every_entry_before_filename_filtering(tmp_path, monkeypatch):
    """A flood of non-JSON names must not evade the fixed discovery work budget."""
    directory = tmp_path / "private-peers"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(peers, "peers_dir", lambda: directory)
    class Entries:
        count = 0
        closed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def __iter__(self):
            return self
        def __next__(self):
            self.count += 1
            if self.count > routes.MAX_REGISTRY_ENTRIES:
                raise AssertionError("discovery scanned beyond its all-entry budget")
            return SimpleNamespace(name=f"not-json-{self.count}")
    entries = Entries()
    monkeypatch.setattr(routes.os, "scandir", lambda *args, **kwargs: entries)
    assert routes.private_records("s") == []
    assert entries.count == routes.MAX_REGISTRY_ENTRIES and entries.closed


@pytest.mark.parametrize("discovery_seconds", [.03, .06])
async def test_local_probe_budget_subtracts_time_already_spent_discovering_registry(
        profile, monkeypatch, discovery_seconds):
    """Discovery must not restart the full 750ms deadline before the HTTP probe."""
    monkeypatch.setattr(routes, "LOCAL_DEADLINE", .05)
    def discovery(*args):
        time.sleep(discovery_seconds)
        return "http://127.0.0.1:12345"
    probes = []
    async def healthy(candidate, *args, timeout, **kwargs):
        probes.append(timeout)
        await asyncio.sleep(timeout)
        return False
    monkeypatch.setattr(routes, "trusted_local", discovery)
    monkeypatch.setattr(routes, "healthy", healthy)
    assert await routes._preferred_local(profile, {}) == ""
    if discovery_seconds < .05:
        assert len(probes) == 1 and 0 < probes[0] < .025
    else:
        assert probes == []


def test_nonmatching_registry_flood_has_bounded_cpu_and_absolute_rss():
    """Measure discovery against a real directory full of names that glob would skip."""
    code = r'''
import json,tempfile,time
from pathlib import Path
from collab import peers,routes
with tempfile.TemporaryDirectory() as temporary:
    directory=Path(temporary)/'peers'
    directory.mkdir(mode=0o700)
    for index in range(3000): (directory/('entry-'+str(index))).touch()
    peers.peers_dir=lambda:directory
    began=time.monotonic(); cpu=time.process_time()
    for _ in range(50): assert routes.private_records('s')==[]
    elapsed=time.monotonic()-began; cpu=time.process_time()-cpu
    rss=int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                 if line.startswith('VmHWM:')))
    print(json.dumps({'directory_entries':3000,'reads':50,'entry_cap':routes.MAX_REGISTRY_ENTRIES,
                      'wall_seconds':elapsed,'cpu_seconds':cpu,'peak_rss_kib':rss}))
'''
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=5, check=True)
    measured = json.loads(done.stdout)
    print("nonmatching registry flood " + json.dumps(measured, sort_keys=True))
    assert measured["wall_seconds"] < 1
    assert measured["cpu_seconds"] < .5
    assert measured["peak_rss_kib"] < 48 * 1024
