"""A local hint must protect credentials against an active loopback relay."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from collab.server import local_tls as tls
from collab.server.session import HubConfig, join_line


@pytest.fixture
def context(tmp_path):
    """Use the actual TLS tool; skip only when openssl itself is absent."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    result = tls.server_context(tmp_path, "127.0.0.1")
    assert result is not None
    return result


def pinned(certificate):
    # No system roots: only this session's certificate can authenticate the
    # route. Hostname checking remains enabled for the configured LAN/loopback.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=certificate)
    return context


async def connect(port, certificate):
    return await asyncio.open_connection("127.0.0.1", port, ssl=pinned(certificate),
                                         server_hostname="127.0.0.1")


@pytest.mark.parametrize('local_available', [True, False])
async def test_saved_guest_resumes_pinned_tls_or_its_trusted_public_fallback(
        context, tmp_path, live_server, monkeypatch, local_available):
    from collab import cli, routes
    from collab.config import SessionProfile
    forwarder = tls.LocalTLS(live_server['base'], '127.0.0.1', context)
    await forwarder.start()
    secure = f'https://127.0.0.1:{forwarder.port}'
    home = tmp_path / 'guest'
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'isolated-peers'))
    profile = SessionProfile(session_id='s_test', name='alice', host_name='alice',
                             token=live_server['host_token'], url=secure,
                             home=str(home), working_path=str(tmp_path))
    profile.save()
    assert routes.remember(profile, {'session_id': 's_test',
        'secure_local_url': secure, 'local_certificate': tls.read_certificate(tmp_path),
        'public_url': live_server['base']}, original_url=live_server['base'])
    if not local_available:
        await forwarder.close()
    try:
        result = await asyncio.to_thread(cli.main,
            ['resume', 's_test', '--home', str(home), '--no-daemon'])
        assert result == 0
        saved = SessionProfile.load_from(profile.dir)
        assert saved.url == (secure if local_available else live_server['base'])
        assert saved.token == profile.token
    finally:
        await forwarder.close()


async def close_writer(writer):
    writer.close()
    with contextlib.suppress(OSError, ssl.SSLError):
        await writer.wait_closed()


def test_certificate_is_private_stable_and_bounded(context, tmp_path):
    """Restarting a hub must preserve its pin without exposing its key."""
    first = tls.read_certificate(tmp_path)
    assert first.startswith("-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY" not in first
    assert tls.server_context(tmp_path, "127.0.0.1") is not None
    assert tls.read_certificate(tmp_path) == first
    if os.name != "nt":
        for path in tls.certificate_paths(tmp_path):
            assert path.stat().st_mode & 0o777 == 0o600
    tls.certificate_paths(tmp_path)[0].write_bytes(b"x" * (tls.MAX_CERT_BYTES + 1))
    assert tls.read_certificate(tmp_path) == ""


def test_missing_or_failing_openssl_keeps_the_public_fallback(tmp_path, monkeypatch, caplog):
    """Optional TLS setup must not make a previously usable hub impossible to host."""
    monkeypatch.setattr(tls.shutil, "which", lambda _: None)
    assert tls.server_context(tmp_path, "127.0.0.1") is None
    assert "using the public route" in caplog.text
    monkeypatch.setattr(tls.shutil, "which", lambda _: "/nonexistent/openssl")
    assert tls.server_context(tmp_path, "127.0.0.1") is None
    assert not list(tmp_path.glob("tls-*.pem"))


def test_a_silent_certificate_tool_has_a_deadline_and_its_owned_process_is_reaped(tmp_path, monkeypatch):
    """A broken external TLS tool must not hold hub startup or survive its fallback."""
    if os.name == "nt":
        pytest.skip("executable script fixture requires POSIX")
    executable = tmp_path / "openssl"
    pid_file = tmp_path / "tool.pid"
    executable.write_text("#!" + sys.executable + "\nimport os,time\n"
        f"open({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n")
    executable.chmod(0o700)
    monkeypatch.setattr(tls.shutil, "which", lambda _: str(executable))
    started = time.monotonic()
    assert tls.server_context(tmp_path, "127.0.0.1") is None
    assert 2.5 < time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not list(tmp_path.glob("tls-*.pem"))


@pytest.mark.parametrize("unsafe", ["fifo", "symlink", "oversized"])
def test_unsafe_certificate_and_key_paths_fail_closed_in_a_fresh_process(unsafe):
    """Neither public metadata nor SSL loading may block on or overwrite an unsafe record."""
    if os.name == "nt" and unsafe in ("fifo", "symlink"):
        pytest.skip("POSIX FIFO/symlink fixture")
    code = r'''
import json,os,stat,tempfile,time
from pathlib import Path
from collab.server.local_tls import certificate_paths,read_certificate,server_context,MAX_CERT_BYTES
import sys
kind=sys.argv[1]
started=time.monotonic()
cpu=time.process_time()
with tempfile.TemporaryDirectory() as temporary:
    directory=Path(temporary)
    for position in (0,1):
        case=directory/str(position)
        case.mkdir()
        cert,key=certificate_paths(case)
        path=(cert,key)[position]
        foreign=case/"foreign"
        foreign.write_text("leave this alone")
        if kind=="fifo":
            os.mkfifo(path)
        elif kind=="symlink":
            path.symlink_to(foreign)
        else:
            path.write_bytes(b"x"*(MAX_CERT_BYTES+1))
        original=path.lstat()
        assert read_certificate(case)==""
        assert server_context(case,"127.0.0.1") is None
        current=path.lstat()
        assert os.path.samestat(original,current)
        assert foreign.read_text()=="leave this alone"
        assert not list(case.glob("tls-load-*"))
        assert not list(case.glob("tls-*.pem"))
    elapsed=time.monotonic()-started
    cpu=time.process_time()-cpu
    rss=int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                 if line.startswith("VmHWM:")))
    print(json.dumps({"kind":kind,"wall_seconds":elapsed,"cpu_seconds":cpu,"peak_rss_kib":rss}))
'''
    done = subprocess.run([sys.executable, "-c", code, unsafe], capture_output=True,
                           text=True, timeout=3, check=True)
    measured = json.loads(done.stdout)
    print("unsafe TLS records " + json.dumps(measured, sort_keys=True))
    assert measured["wall_seconds"] < 1
    assert measured["cpu_seconds"] < .5
    assert measured["peak_rss_kib"] < 48 * 1024


def test_ssl_loading_uses_private_snapshots_despite_original_path_substitution(context, tmp_path, monkeypatch):
    """A checked source must never be reopened by SSL after becoming a FIFO."""
    if os.name == "nt":
        pytest.skip("POSIX FIFO fixture")
    original = ssl.SSLContext.load_cert_chain
    source_cert, source_key = tls.certificate_paths(tmp_path)
    opened = []
    def replace_before_load(self, certfile, keyfile, *args, **kwargs):
        opened.extend((Path(certfile), Path(keyfile)))
        assert Path(certfile).parent != tmp_path and Path(keyfile).parent != tmp_path
        assert Path(certfile).parent.stat().st_mode & 0o777 == 0o700
        source_cert.unlink()
        source_key.unlink()
        os.mkfifo(source_cert)
        os.mkfifo(source_key)
        return original(self, certfile, keyfile, *args, **kwargs)
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", replace_before_load)
    assert tls.server_context(tmp_path, "127.0.0.1") is not None
    assert tls.read_certificate(tmp_path) == ""
    assert not any(path.exists() for path in opened)


def test_combined_certificate_and_private_key_is_never_published(context, tmp_path):
    """A misplaced combined PEM must not expose the private key in route metadata."""
    cert, key = tls.certificate_paths(tmp_path)
    cert.write_bytes(cert.read_bytes() + key.read_bytes())
    assert tls.read_certificate(tmp_path) == ""


def test_expired_certificate_is_replaced_instead_of_advertised(context, tmp_path, monkeypatch):
    """A persisted old pin must not prevent local TLS recovery after its certificate expires."""
    original = ssl._ssl._test_decode_cert
    calls = []
    def expired_once(path):
        details = original(path)
        calls.append(path)
        if len(calls) == 1:
            details["notAfter"] = "Jan  1 00:00:00 2000 GMT"
        return details
    before = tls.read_certificate(tmp_path)
    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", expired_once)
    assert tls.server_context(tmp_path, "127.0.0.1") is not None
    assert len(calls) == 2 and tls.read_certificate(tmp_path) != before


async def test_tls_preserves_http_gzip_bytes_and_rejects_a_different_certificate(context, tmp_path):
    """Verify a real TLS exchange before sending the bearer header."""
    received = []
    body = gzip.compress(b'{"participants":["alice","bob"]}')
    response = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    async def upstream(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(response)
        await writer.drain()
        await close_writer(writer)
    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    forwarder = tls.LocalTLS(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                              "127.0.0.1", context)
    await forwarder.start()
    try:
        reader, writer = await connect(forwarder.port, tls.read_certificate(tmp_path))
        writer.write(b"GET /participants HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret\r\n\r\n")
        await writer.drain()
        assert await reader.read() == response
        await close_writer(writer)
        other = tmp_path / "different-session"
        assert tls.server_context(other, "127.0.0.1") is not None
        with pytest.raises(ssl.SSLCertVerificationError):
            await connect(forwarder.port, tls.read_certificate(other))
        assert len(received) == 1
    finally:
        await forwarder.close()
        server.close()
        await server.wait_closed()
    assert not forwarder.tasks


async def test_raw_active_relay_cannot_read_credentials_or_plaintext_response(context, tmp_path):
    """Relaying a genuine TLS handshake must not recreate the old plaintext-proof flaw."""
    observed, received = [], []
    async def upstream(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\n\r\nprivate-reply-data!")
        await writer.drain()
        await close_writer(writer)
    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    forwarder = tls.LocalTLS(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                              "127.0.0.1", context)
    await forwarder.start()
    relay_tasks = set()
    async def relay(reader, writer):
        remote, target = await asyncio.open_connection("127.0.0.1", forwarder.port)
        async def copy(source, destination):
            while chunk := await source.read(65536):
                observed.append(chunk)
                destination.write(chunk)
                await destination.drain()
        tasks = [asyncio.create_task(copy(reader, target)), asyncio.create_task(copy(remote, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await close_writer(target)
            await close_writer(writer)
    def accept(reader, writer):
        task = asyncio.create_task(relay(reader, writer))
        relay_tasks.add(task)
        task.add_done_callback(relay_tasks.discard)
    relay_server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        reader, writer = await connect(relay_server.sockets[0].getsockname()[1],
                                        tls.read_certificate(tmp_path))
        writer.write(b"GET / HTTP/1.1\r\nAuthorization: Bearer never-leak-this\r\n\r\n")
        await writer.drain()
        result = await reader.read()
        assert b"private-reply-data!" in result
        await close_writer(writer)
        assert b"never-leak-this" in received[0]
        assert b"never-leak-this" not in b"".join(observed)
        assert b"private-reply-data!" not in b"".join(observed)
    finally:
        relay_server.close()
        await relay_server.wait_closed()
        for task in list(relay_tasks):
            task.cancel()
        await asyncio.gather(*relay_tasks, return_exceptions=True)
        await forwarder.close()
        server.close()
        await server.wait_closed()


async def test_sse_keepalives_prevent_idle_timeout_despite_silent_request_direction(context, tmp_path, monkeypatch):
    """A shared idle timer must not impose a lifetime limit on a healthy SSE feed."""
    monkeypatch.setattr(tls, "IDLE_SECONDS", .08)
    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        for index in range(12):
            writer.write(b"event: keepalive\r\ndata: {}\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(.02)
        await close_writer(writer)
    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    forwarder = tls.LocalTLS(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                              "127.0.0.1", context)
    await forwarder.start()
    try:
        reader, writer = await connect(forwarder.port, tls.read_certificate(tmp_path))
        writer.write(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        started = time.monotonic()
        result = await reader.read()
        assert result.count(b"event: keepalive") == 12
        assert time.monotonic() - started > tls.IDLE_SECONDS * 2
        await close_writer(writer)
    finally:
        await forwarder.close()
        server.close()
        await server.wait_closed()


async def test_silent_handshakes_share_the_connection_cap_and_shutdown_reaps_them(context, monkeypatch):
    """A cap checked after TLS handshakes would miss every raw silent socket here."""
    monkeypatch.setattr(tls, "MAX_CONNECTIONS", 8)
    forwarder = tls.LocalTLS("http://127.0.0.1:1", "127.0.0.1", context)
    await forwarder.start()
    peers = []
    try:
        for _ in range(30):
            peers.append(await asyncio.open_connection("127.0.0.1", forwarder.port))
        await asyncio.sleep(.03)
        assert len(forwarder.tasks) == 8
        started = time.monotonic()
        await forwarder.close()
        assert time.monotonic() - started < 1
        assert not forwarder.tasks
    finally:
        await forwarder.close()
        for _, writer in peers:
            await close_writer(writer)


def test_public_join_line_is_short_and_does_not_put_destination_authority_in_queries(tmp_path, context):
    """The unchanged visible address/fragment is enough; TLS metadata arrives after joining."""
    cfg = HubConfig(session_id="s", host_name="h", port=12345, bind="127.0.0.1",
        invite="same-invite", host_token="host-token", home=str(tmp_path))
    assert join_line(cfg) == "collab join http://127.0.0.1:12345#same-invite"
    cfg.public_url = "https://public.example/session"
    cfg.secure_port = 54321
    cfg.dir.mkdir(parents=True)
    tls.certificate_paths(cfg.dir)[0].write_text(tls.read_certificate(tmp_path))
    words = shlex.split(join_line(cfg))
    assert len(words) == 3
    parsed = urlsplit(words[2])
    query = parse_qs(parsed.query)
    assert parsed.fragment == "same-invite"
    assert query == {}
    assert join_line(cfg) == "collab join https://public.example/session#same-invite"
    cfg.public_url = "https://public.example/s/s"
    parsed = urlsplit(shlex.split(join_line(cfg))[2])
    assert parse_qs(parsed.query) == {"collab_session": ["s"]}


def test_app_publishes_only_its_live_tls_entrance_and_cleans_up_saved_port(tmp_path):
    """The host's advertised certificate/port must name the listener that actually started."""
    from fastapi.testclient import TestClient
    from collab.server.app import create_app
    from collab.server.store import Store
    cfg = HubConfig(session_id="s", host_name="h", port=12345, bind="127.0.0.1",
        invite="invite", host_token="token", home=str(tmp_path))
    cfg.save()
    store = Store(cfg.db_path)
    app = create_app(store=store, session_id="s", host_name="h", public_url="https://public.example",
                     local_url=cfg.local_url, local_tls_config=cfg)
    with TestClient(app):
        loaded = HubConfig.load("s", tmp_path)
        assert app.state.local_tls._socket.getsockname()[0] == cfg.bind
        routes = app.state.routes()
        assert loaded.secure_port > 0
        assert routes["secure_local_url"] == loaded.secure_local_url
        assert routes["local_certificate"] == tls.read_certificate(cfg.dir)
        assert routes["public_url"] == "https://public.example"
    assert HubConfig.load("s", tmp_path).secure_port == 0
    store.close()


def test_silent_peer_flood_has_bounded_cpu_and_absolute_peak_rss():
    """Measure the handshake cap under real sockets, including cancellation of all owned work."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    code = r'''
import asyncio,json,tempfile,time
from pathlib import Path
from collab.server.local_tls import LocalTLS,MAX_CONNECTIONS,server_context
async def run(directory):
    context=server_context(Path(directory),"127.0.0.1")
    proxy=LocalTLS("http://127.0.0.1:1","127.0.0.1",context)
    await proxy.start()
    peers=[]
    before=time.process_time()
    for _ in range(400):
        peers.append(await asyncio.open_connection("127.0.0.1",proxy.port))
    await asyncio.sleep(.05)
    assert len(proxy.tasks)<=MAX_CONNECTIONS
    active=len(proxy.tasks)
    await proxy.close()
    assert not proxy.tasks
    for _,writer in peers:
        writer.close()
    await asyncio.gather(*(writer.wait_closed() for _,writer in peers),return_exceptions=True)
    cpu=time.process_time()-before
    rss=int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                 if line.startswith("VmHWM:")))
    print(json.dumps({"connections":400,"active_at_cap":active,"cpu_seconds":cpu,"peak_rss_kib":rss}))
with tempfile.TemporaryDirectory() as directory:
    asyncio.run(run(directory))
'''
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=15, check=True)
    measured = json.loads(done.stdout)
    print("local TLS flood " + json.dumps(measured, sort_keys=True))
    assert measured["active_at_cap"] == tls.MAX_CONNECTIONS
    assert measured["cpu_seconds"] < 5
    assert measured["peak_rss_kib"] < 128 * 1024
