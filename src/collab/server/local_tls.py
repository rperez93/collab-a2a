"""A pinned-certificate TLS entrance to the hub's existing HTTP listener.

TLS must protect the credential exchange itself. A public HMAC challenge can
be relayed by another machine's loopback service and therefore cannot make a
plaintext local route safe. This forwarder keeps one ASGI event loop/Hub while
giving remote LAN clients an encrypted, certificate-pinned channel.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
# Eight participants at eight pooled sockets each use 64 slots. 256 leaves room
# for CLI bursts and long-lived SSE while keeping handshake/forwarding memory
# finite. Overflow closes promptly so a client can fall back to its public URL.
MAX_CONNECTIONS = 256
CHUNK_BYTES = 64 * 1024
# The hub emits SSE keepalives every 15s. A 60s idle deadline tolerates delayed
# keepalives without putting a lifetime limit on a healthy, long-lived feed.
IDLE_SECONDS = 60.0
MAX_CERT_BYTES = 8192


class UnsafeTLSRecord(OSError):
    """An existing TLS path must be left untouched and local TLS disabled."""


def _read_record(path: Path) -> bytes | None:
    """Open without following/blocking, then inspect the descriptor before reading.

    Path.exists/is_file followed by SSL loading the name left a FIFO able to
    block hub startup forever. The nonblocking open, regular-file fstat and
    fixed-size read cover both records. Identity checks also cover platforms
    without O_NOFOLLOW; SSL later sees private snapshots, never these names.
    """
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CERT_BYTES:
        raise UnsafeTLSRecord("TLS record must be a regular file of at most 8 KiB")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode) \
                or not os.path.samestat(before, opened) or not os.path.samestat(current, opened):
            raise UnsafeTLSRecord("TLS record changed while opening")
        if opened.st_size > MAX_CERT_BYTES:
            raise UnsafeTLSRecord("TLS record exceeds 8 KiB")
        raw = bytearray()
        while len(raw) <= MAX_CERT_BYTES:
            chunk = os.read(fd, MAX_CERT_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_CERT_BYTES:
            raise UnsafeTLSRecord("TLS record exceeds 8 KiB")
        return bytes(raw)
    finally:
        os.close(fd)


def _load_snapshot(directory: Path, certificate: bytes, key: bytes) -> ssl.SSLContext:
    """Only validated bytes in a private directory reach SSL's blocking file API."""
    with tempfile.TemporaryDirectory(prefix="tls-load-", dir=directory) as temporary:
        cert_path, key_path = certificate_paths(Path(temporary))
        for path, raw in ((cert_path, certificate), (key_path, key)):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(raw)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        # CPython's certificate decoder is also used by ssl's own tests. It
        # receives only our private bounded snapshot, so it cannot re-open an
        # attacker-substituted FIFO. If unavailable, TLS fails closed and the
        # ordinary public route remains usable.
        decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
        if decode is None:
            raise ssl.SSLError("certificate validity inspection is unavailable")
        details = decode(str(cert_path))
        now = time.time()
        if not ssl.cert_time_to_seconds(details["notBefore"]) <= now \
                < ssl.cert_time_to_seconds(details["notAfter"]):
            raise ssl.SSLError("local TLS certificate is outside its validity period")
        return context


def certificate_paths(directory: Path) -> tuple[Path, Path]:
    return directory / "local-tls.crt", directory / "local-tls.key"


def read_certificate(directory: Path) -> str:
    """The public certificate only, with a record-size bound for join metadata."""
    cert, _ = certificate_paths(directory)
    try:
        raw = _read_record(cert)
        if raw is None:
            return ""
        pem = raw.decode("ascii")
        # Never publish a combined certificate/private-key PEM as metadata.
        if re.fullmatch(r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+"
                        r"-----END CERTIFICATE-----\r?\n?", pem) is None:
            return ""
        return pem
    except (OSError, UnicodeError):
        return ""


def server_context(directory: Path, hostname: str) -> ssl.SSLContext | None:
    """Load or mint the session's private certificate without unbounded output.

    Openssl writes into already-created 0600 files; its stdout/stderr go to
    DEVNULL, so neither a noisy nor silent tool can retain output or exceed
    the 3s deadline. Only the process group this call creates is terminated.
    Missing tooling leaves the legacy public route usable.
    """
    cert, key = certificate_paths(directory)
    try:
        existing_cert, existing_key = _read_record(cert), _read_record(key)
    except OSError:
        logger.warning("local TLS unavailable: unsafe certificate/key file; using the public route")
        return None
    if existing_cert is not None and existing_key is not None:
        try:
            return _load_snapshot(directory, existing_cert, existing_key)
        except (OSError, ValueError, KeyError):
            logger.warning("local TLS certificate is unusable; creating a replacement")
    executable = shutil.which("openssl")
    if not executable:
        logger.warning("local TLS unavailable: openssl is missing; using the public route")
        return None
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    proc = None
    try:
        for prefix in ("tls-cert-", "tls-key-"):
            fd, name = tempfile.mkstemp(prefix=prefix, suffix=".pem", dir=directory)
            os.close(fd)
            paths.append(Path(name))
        names = ["DNS:localhost", "IP:127.0.0.1", "IP:::1"]
        try:
            address = ipaddress.ip_address(hostname)
            names.append("IP:" + str(address))
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname):
                names.append("DNS:" + hostname)
        proc = subprocess.Popen([executable, "req", "-x509", "-newkey", "ed25519",
            "-nodes", "-days", "365", "-subj", "/CN=collab-local",
            "-addext", "subjectAltName=" + ",".join(names),
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature",
            "-addext", "extendedKeyUsage=serverAuth",
            "-keyout", str(paths[1]), "-out", str(paths[0])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt")
        if proc.wait(timeout=3) != 0:
            raise OSError("certificate generation failed")
        generated_cert, generated_key = _read_record(paths[0]), _read_record(paths[1])
        if generated_cert is None or generated_key is None:
            raise OSError("certificate tool did not create both records")
        context = _load_snapshot(directory, generated_cert, generated_key)
        # Never replace an existing unsafe path even if another process
        # substituted it while the external certificate tool was running.
        _read_record(cert)
        _read_record(key)
        paths[1].replace(key)
        paths[0].replace(cert)
        return context
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        logger.warning("local TLS unavailable: certificate generation failed; using the public route")
        return None
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        for path in paths:
            with contextlib.suppress(OSError):
                path.unlink()


class LocalTLS:
    """Bounded raw TCP forwarding; HTTP/SSE framing and gzip remain opaque."""

    def __init__(self, target: str, bind: str, context: ssl.SSLContext,
                 *, port: int = 0) -> None:
        parsed = urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("local TLS target must be the configured HTTP listener")
        self.target_host = parsed.hostname
        self.target_port = parsed.port or 80
        self.bind, self.port, self.context = bind, port, context
        self._socket: socket.socket | None = None
        self._accept_task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self._closing = False

    async def start(self) -> None:
        family = socket.AF_INET6 if ":" in self.bind else socket.AF_INET
        self._socket = socket.create_server((self.bind, self.port), family=family,
                                            backlog=MAX_CONNECTIONS)
        self._socket.setblocking(False)
        self.port = self._socket.getsockname()[1]
        self._accept_task = asyncio.create_task(self._accept_loop())

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            connection, _ = await loop.sock_accept(self._socket)
            # Admission happens BEFORE TLS. start_server(ssl=...) calls the
            # application only after handshaking, leaving a silent raw-socket
            # flood outside the apparent connection cap.
            if len(self.tasks) >= MAX_CONNECTIONS:
                connection.close()
                continue
            task = asyncio.create_task(self._from_socket(connection))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _from_socket(self, connection: socket.socket) -> None:
        try:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(limit=CHUNK_BYTES)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await asyncio.wait_for(loop.connect_accepted_socket(
                lambda: protocol, connection, ssl=self.context,
                ssl_handshake_timeout=3), timeout=3.1)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            await self._forward(reader, writer)
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            connection.close()
        except asyncio.CancelledError:
            connection.close()
            raise

    async def _forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        pipes: list[asyncio.Task] = []
        try:
            remote, upstream = await asyncio.wait_for(asyncio.open_connection(
                self.target_host, self.target_port, limit=CHUNK_BYTES), timeout=1)
            # Each direction gets a fixed read buffer and honors write-side
            # backpressure. FIRST_COMPLETED also closes an abandoned opposite
            # direction instead of leaving an idle task after disconnect.
            last_activity = asyncio.get_running_loop().time()
            async def copy(source, destination):
                nonlocal last_activity
                while True:
                    chunk = await source.read(CHUNK_BYTES)
                    if not chunk:
                        return
                    last_activity = asyncio.get_running_loop().time()
                    destination.write(chunk)
                    await asyncio.wait_for(destination.drain(), timeout=IDLE_SECONDS)
            async def idle_deadline():
                # A quiet request half is NORMAL for SSE. Activity in either
                # direction resets the idle budget, so keepalives can hold a
                # healthy stream for hours without a total lifetime limit.
                while True:
                    remaining = IDLE_SECONDS - (asyncio.get_running_loop().time() - last_activity)
                    if remaining <= 0:
                        return
                    await asyncio.sleep(remaining)
            pipes = [asyncio.create_task(copy(reader, upstream)),
                     asyncio.create_task(copy(remote, writer)),
                     asyncio.create_task(idle_deadline())]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            pass
        finally:
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            for connection in (upstream, writer):
                if connection is not None:
                    connection.close()
                    with contextlib.suppress(OSError, asyncio.TimeoutError, ssl.SSLError):
                        await asyncio.wait_for(connection.wait_closed(), timeout=2)

    async def close(self) -> None:
        self._closing = True
        if self._socket is not None:
            self._socket.close()
        if self._accept_task is not None:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # An already-finished task's done callback can still be queued when
        # gather returns immediately. close() has reaped these exact tasks;
        # retire their bookkeeping now rather than relying on another loop turn.
        self.tasks.difference_update(pending)
