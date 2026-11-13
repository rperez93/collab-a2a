"""A local address on another machine must never inherit the user's secrets."""
import asyncio
import base64
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlencode

import httpx
import pytest

from collab import routes
from collab.client import onboard
from collab.client.daemon import Daemon
from collab.client.hub_client import HubError
from collab.server.local_tls import LocalTLS, read_certificate, server_context


@pytest.fixture
async def secure(live_server, tmp_path):
    context = await asyncio.to_thread(server_context, tmp_path / "cert", "127.0.0.1")
    if context is None:
        pytest.skip("openssl certificate generation unavailable")
    forwarder = LocalTLS(live_server["base"], "127.0.0.1", context)
    await forwarder.start()
    try:
        yield {**live_server, "url": f"https://127.0.0.1:{forwarder.port}",
               "pem": read_certificate(tmp_path / "cert"), "forwarder": forwarder}
    finally:
        await forwarder.close()


def link(base, secure, sid="s_test", invite="invite"):
    return base + "?" + urlencode({"collab_session": sid, "collab_local": secure["url"],
        "collab_cert": base64.urlsafe_b64encode(secure["pem"].encode()).decode()}) + "#" + invite


async def test_a_pinned_local_route_joins_the_same_hub_without_using_the_tunnel(secure, tmp_path, monkeypatch):
    """TLS authenticates every secret-bearing request, including CLI calls after join."""
    from collab.cli import _client
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "guest"))
    original = "https://unused.invalid"
    raw = link(original, secure, invite=secure["invite"])
    profile, snapshot, _ = await asyncio.to_thread(onboard.join_session, raw, name="bob", start_daemon=False)
    assert profile.url == secure["url"]
    assert profile.session_id == snapshot["session_id"] == "s_test"
    saved = routes.load(profile)
    assert saved["original_user_url"] == original
    def read():
        with _client(profile) as client:
            return client.participants()
    assert (await asyncio.to_thread(read))["you"] == "bob"


async def test_a_wrong_certificate_falls_back_before_join_sends_any_secret(secure, tmp_path, monkeypatch):
    """A service relaying the old nonce proof cannot impersonate the TLS certificate."""
    await asyncio.to_thread(server_context, tmp_path / "other", "127.0.0.1")
    wrong = {**secure, "pem": read_certificate(tmp_path / "other")}
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "guest"))
    raw = link(secure["base"], wrong, invite=secure["invite"])
    profile, _, _ = await asyncio.to_thread(onboard.join_session, raw, name="bob", start_daemon=False)
    assert profile.url == secure["base"]
    assert len(secure["store"].participants()) == 2


def test_hint_queries_preserve_legacy_fragment_and_certificate_roots(secure):
    """Old clients ignore query hints and still spend the same original invite."""
    raw = link("https://public.invalid", secure)
    assert onboard.split_join_url(raw) == ("https://public.invalid", "invite")
    parsed = routes.hints(raw)
    context = routes.tls_context(parsed["local_certificate"])
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs(binary_form=True)) <= 1
    with pytest.raises((ValueError, ssl.SSLError)):
        routes.tls_context(secure["pem"] + secure["pem"])


def test_an_uncertain_join_post_is_never_retried_on_another_route(tmp_path, monkeypatch):
    """A timed-out POST may already have created membership; retrying rotates its token."""
    monkeypatch.setattr(routes, "join_route", lambda *args: ("http://selected.invalid", {}))
    calls = []
    class Client:
        def __init__(self, *args, **kwargs):
            calls.append(args[0])
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def join(self, *args):
            raise HubError("join response was lost")
    monkeypatch.setattr(onboard, "HubClient", Client)
    with pytest.raises(HubError, match="response was lost"):
        onboard.join_session("https://original.invalid#invite", start_daemon=False)
    assert calls == ["http://selected.invalid"]


async def test_route_changes_preserve_original_authority_and_use_a_new_pool(profile, secure, monkeypatch):
    """A new certificate cannot widen trust through a connection pooled under the old pin."""
    original = "https://original.invalid"
    metadata = dict(session_id=profile.session_id, secure_local_url=secure["url"],
                    local_certificate=secure["pem"], public_url=original,
                    local_url="http://127.0.0.1:9")
    routes.remember(profile, metadata, original_url=original)
    profile.url = secure["url"]
    first = routes.options(profile)["verify"]
    assert routes.remember(profile, metadata, original_url="http://wrong.invalid") is False
    assert routes.load(profile)["original_user_url"] == original
    assert routes.options(profile)["verify"] is not first
    # A response that started before switching URL/token/pin has no authority
    # to repopulate routes, even when it says the same session id.
    old = (profile.url, profile.token)
    profile.token = "replacement"
    assert routes.remember(profile, metadata, expected=old) is False


async def test_discovered_plain_http_routes_never_become_legacy_fallback(profile, monkeypatch):
    """A remote hub's localhost refers to this guest, not to the original host."""
    routes.remember(profile, {"session_id": profile.session_id,
        "public_url": "http://attacker.invalid", "local_url": "http://127.0.0.1:9"},
        original_url="https://original.invalid")
    monkeypatch.setattr(routes, "trusted_local", lambda *args: "")
    tried = []
    async def healthy(candidate, *args, **kwargs):
        tried.append(candidate)
        return True
    monkeypatch.setattr(routes, "healthy", healthy)
    assert await routes.select(profile) == "https://original.invalid"
    assert tried == ["https://original.invalid"]


async def test_a_stale_response_cannot_replace_the_new_route_snapshot(profile):
    """The old route may answer after a reconnect has selected another authority."""
    daemon = Daemon(profile)
    async def handle(request):
        profile.url = "https://new.invalid"
        return httpx.Response(200, json={"participants": [], "routes": {
            "session_id": profile.session_id, "public_url": "https://old.invalid"}})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await daemon._refresh_snapshot(client)
        assert daemon.snapshot == {}
        assert routes.load(profile) == {}
    finally:
        daemon.inbox.close()


def test_corrupt_or_nonregular_route_files_are_bounded(profile):
    """A FIFO must not freeze reconnect; an oversized document must not allocate freely."""
    file = profile.dir / "routes.json"
    file.write_bytes(b"x" * (routes.MAX_ROUTE_BYTES + 1))
    assert routes.load(profile) == {}
    file.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(file)
        assert routes.load(profile) == {}


async def test_malformed_candidate_types_preserve_the_valid_original_fallback(profile, monkeypatch):
    """A matching session id is not evidence that a local JSON record is well formed."""
    (profile.dir / "routes.json").write_text(json.dumps({"session_id": profile.session_id,
        "original_user_url": "https://original.invalid", "public_url": {"bad": 1},
        "secure_local_url": ["bad"], "local_certificate": 42}))
    monkeypatch.setattr(routes, "trusted_local", lambda *args: "")
    async def healthy(*args, **kwargs):
        return True
    monkeypatch.setattr(routes, "healthy", healthy)
    assert await routes.select(profile) == "https://original.invalid"


def test_shared_registry_permissions_cannot_authorize_plaintext_credentials(profile, tmp_path, monkeypatch):
    """Public machine/user strings do not prevent a planted loopback service record."""
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX permission check")
    from collab import peers
    directory = tmp_path / "peers"
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(directory))
    record = peers.announce(session_id=profile.session_id, name="host", role="host",
        url="https://public.invalid", local_url="http://127.0.0.1:43210",
        repo="/unused", home="/unused", invite="invite")
    assert routes.trusted_local(profile.session_id, invite="invite") == "http://127.0.0.1:43210"
    directory.chmod(0o777)
    assert routes.trusted_local(profile.session_id, invite="invite") == ""
    directory.chmod(0o700)
    record.chmod(0o666)
    assert routes.trusted_local(profile.session_id, invite="invite") == ""


async def test_a_hinted_public_fallback_checks_session_before_the_join_post(monkeypatch):
    """A stale tunnel now serving another session must not receive the old invite."""
    monkeypatch.setattr(routes, "private_records", lambda *args: [])
    async def wrong_session(*args, **kwargs):
        return False
    monkeypatch.setattr(routes, "healthy", wrong_session)
    with pytest.raises(ValueError, match="no invite was submitted"):
        await asyncio.to_thread(routes.join_route,
            "https://public.invalid?collab_session=s_expected#invite", "https://public.invalid", "invite")


async def test_a_pin_change_closes_the_old_daemon_pool_and_restarts_the_stream(profile, secure, tmp_path, monkeypatch):
    """Creating a new SSLContext is insufficient while an SSE pool stays open."""
    from collab.client import daemon as module
    await asyncio.to_thread(server_context, tmp_path / "replacement", "127.0.0.1")
    original = "https://public.invalid"
    profile.url = secure["url"]
    metadata = dict(session_id=profile.session_id, secure_local_url=profile.url,
                    local_certificate=secure["pem"], public_url=original)
    routes.remember(profile, metadata, original_url=original)
    daemon = Daemon(profile)
    entered, cancelled, restarted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    clients = []
    real_client = httpx.AsyncClient
    def handle(request):
        if request.url.path.endswith("compatibility"):
            return httpx.Response(200, stream=httpx.ByteStream(
                json.dumps({"protocol_major": 2, "version": "2.1.2"}).encode()))
        return httpx.Response(200, json={"participants": [], "routes": metadata})
    def factory(*args, **kwargs):
        client = real_client(*args, **kwargs, transport=httpx.MockTransport(handle))
        clients.append(client)
        return client
    async def select(*args):
        return profile.url
    async def stream(client):
        if len(clients) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            restarted.set()
            daemon._stop.set()
    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(module, "BACKOFF_START", .001)
    monkeypatch.setattr(routes, "select", select)
    monkeypatch.setattr(daemon, "_stream_once", stream)
    monkeypatch.setattr(daemon, "_revive_hub_if_host", lambda: None)
    monkeypatch.setattr(daemon, "_follow_url_change", lambda: None)
    task = asyncio.create_task(daemon._connect_forever())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        metadata["local_certificate"] = read_certificate(tmp_path / "replacement")
        await daemon._refresh_snapshot(daemon._http)
        await asyncio.wait_for(restarted.wait(), 1)
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set() and clients[0].is_closed
        assert len(clients) == 2
    finally:
        daemon._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        daemon.inbox.close()


@pytest.mark.parametrize("candidate", ["http://user:pass@host", "file:///tmp/secret", "https://host/#secret",
    "http://host/?token=secret", "http://host:0", "http://host\n.invalid", "http://host:99999"])
def test_route_urls_cannot_smuggle_credentials_or_request_targets(candidate):
    assert routes.url(candidate) == ""
