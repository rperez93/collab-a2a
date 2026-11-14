"""Keep route hints separate from the authority that permits credentials.

A remote host's loopback address names this machine when pasted here. A nonce
proof can be relayed from the real hub, so it cannot authorize a plaintext
connection to that address. Automatic LAN routes require an exclusive TLS
certificate; local HTTP is selected only from this user's private registry.
"""
from __future__ import annotations

import asyncio
import ipaddress
from itertools import islice
import json
import os
import re
import ssl
import stat
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .atomic import discard, scratch
from .protocol import EXT_PREFIX

# A route document contains three URLs and one small Ed25519 certificate.
# This bound also applies to corrupt local files and hostile health responses.
MAX_ROUTE_BYTES = 16 * 1024
LOCAL_DEADLINE = 0.75
PUBLIC_DEADLINE = 5.0
MAX_REGISTRY_ENTRIES = 1024


def secure_local(value):
    """Authenticated metadata may name numeric loopback, private LAN or ULA TLS.

    DNS and arbitrary public addresses are not automatic local candidates.
    Keeping this numeric also avoids DNS rebinding changing the meaning of a
    local hint between its health probe and the credential-bearing connection.
    """
    candidate = url(value)
    if not candidate.startswith("https://"):
        return ""
    try:
        address = ipaddress.ip_address(urlsplit(candidate).hostname)
        address = getattr(address, "ipv4_mapped", None) or address
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        if address.is_loopback or any(address in ipaddress.ip_network(network) for network in networks):
            return candidate
    except ValueError:
        pass
    return ""


def url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.username is not None or parsed.password is not None \
                or parsed.query or parsed.fragment:
            return ""
        port = parsed.port
        if port is not None and not 0 < port < 65536:
            return ""
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parsed.scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def tls_context(pem):
    """Trust one supplied certificate, without importing the system CA roots."""
    if not isinstance(pem, str) or len(pem) > 8192 or not re.fullmatch(
            r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+\r?\n-----END CERTIFICATE-----\s*", pem):
        raise ValueError("the local route needs exactly one certificate")
    # A CA bundle would silently widen authority to unrelated servers. The
    # full-match above accepts one PEM object, and this context starts empty.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=pem)
    return context


def hints(raw):
    """Read an expected session id, never destination/certificate authority.

    Query parameters are outside the visible URL authority. Pairing an
    attacker-chosen destination with its own certificate only authenticates
    the attacker. The first join must use the original visible URL or the
    private same-machine registry; only its authenticated response supplies
    local TLS metadata for subsequent traffic.
    """
    if len(raw) > MAX_ROUTE_BYTES:
        raise ValueError("join link is too long")
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        query = parse_qs(parsed.query, max_num_fields=16)
        sid = query.get("collab_session", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid):
            return {}
        return {"session_id": sid}
    except ValueError:
        return {}


def load(profile):
    try:
        fd = os.open(profile.dir / "routes.json", os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            raw = stream.read(MAX_ROUTE_BYTES + 1)
        data = json.loads(raw) if len(raw) <= MAX_ROUTE_BYTES else {}
        if not isinstance(data, dict) or data.get("session_id") != profile.session_id:
            return {}
        normalized = {"session_id": profile.session_id}
        for name in ("original_user_url", "public_url", "local_url", "secure_local_url"):
            normalized[name] = url(data.get(name))
        normalized["secure_local_url"] = secure_local(data.get("secure_local_url"))
        pem = data.get("local_certificate")
        normalized["local_certificate"] = pem if isinstance(pem, str) and len(pem) <= 8192 else ""
        return normalized if normalized["original_user_url"] else {}
    except (OSError, ValueError, RecursionError):
        return {}


def remember(profile, metadata, *, original_url=None, expected=None):
    """Adopt only a current response; preserve the original user's authority."""
    if expected is not None and expected != (profile.url, profile.token):
        return False
    if not isinstance(metadata, dict) or metadata.get("session_id") != profile.session_id:
        return False
    prior = load(profile)
    original = url(prior.get("original_user_url") or original_url or "")
    if not original:
        return False
    saved = {"session_id": profile.session_id, "original_user_url": original}
    for name in ("local_url", "secure_local_url", "public_url"):
        saved[name] = url(metadata.get(name))
    pem = metadata.get("local_certificate", "")
    try:
        tls_context(pem)
        saved["secure_local_url"] = secure_local(saved["secure_local_url"])
        if not saved["secure_local_url"]:
            raise ValueError("not a TLS route")
        saved["local_certificate"] = pem
    except (ValueError, ssl.SSLError):
        saved["secure_local_url"], saved["local_certificate"] = "", ""
    if not saved["secure_local_url"] and url(profile.url) == prior.get("secure_local_url"):
        saved["secure_local_url"] = prior["secure_local_url"]
        saved["local_certificate"] = prior["local_certificate"]
    if saved == prior:
        return False
    profile.dir.mkdir(parents=True, exist_ok=True)
    destination = profile.dir / "routes.json"
    temporary = scratch(destination)
    try:
        temporary.write_text(json.dumps(saved))
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        discard(temporary)
    return True


def options(profile):
    """CLI and daemon must authenticate the same selected TLS route."""
    state = load(profile)
    if url(profile.url) == state.get("secure_local_url"):
        return {"verify": tls_context(state.get("local_certificate"))}
    return {}


def generation(profile):
    state = load(profile)
    return (profile.url, profile.token, state.get("secure_local_url"), state.get("local_certificate"))


def private_records(session_id=""):
    """A shared registry can describe peers but cannot authorize plaintext auth.

    Machine/user labels are public and forgeable. The authorization is the
    private directory and record ownership, checked before bounded reads; a
    custom group-writable registry is discovery-only for this feature.
    """
    from . import peers
    if not hasattr(os, "getuid"):
        return []
    directory = peers.peers_dir()
    directory_fd = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return []
        found = []
        # Glob may collect directory entries before filtering, and a cap on
        # matching .json names does nothing against millions of other names.
        # Bound the streaming iterator BEFORE suffix filtering, with no
        # lookahead entry. The checked directory fd also keeps subsequent
        # record opens anchored if the path is substituted during discovery.
        with os.scandir(directory_fd) as entries:
            for entry in islice(entries, MAX_REGISTRY_ENTRIES):
                if not entry.name.endswith(".json"):
                    continue
                try:
                    fd = os.open(entry.name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=directory_fd)
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                            continue
                        raw = stream.read(MAX_ROUTE_BYTES + 1)
                    if len(raw) > MAX_ROUTE_BYTES:
                        continue
                    data = json.loads(raw)
                    if not isinstance(data, dict) or session_id and data.get("session_id") != session_id:
                        continue
                    peer = peers.Peer(**{k: v for k, v in data.items() if k in peers.Peer.__dataclass_fields__})
                    if peer.alive:
                        found.append(peer)
                except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                    continue
        return found
    except OSError:
        return []
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def trusted_local(session_id, *, invite=None):
    """Use only an unambiguous host record in the private machine registry."""
    from . import peers
    here, user = peers.machine_id(), peers.current_user()
    matches = [p for p in private_records(session_id)
               if p.role == "host" and p.machine_id == here and p.user == user
               and (invite is None or p.invite == invite) and peers.is_loopback(p.local_url)]
    addresses = {url(p.local_url) for p in matches}
    return addresses.pop() if len(addresses) == 1 else ""


def join_route(raw, original, invite):
    """Select before POST /join; an uncertain POST is never retried elsewhere."""
    from . import peers
    advertised = hints(raw)
    sid = advertised.get("session_id", "")
    here, user = peers.machine_id(), peers.current_user()
    records = [p for p in private_records(sid)
               if p.role == "host" and p.machine_id == here and p.user == user
               and p.invite == invite and peers.is_loopback(p.local_url)
               and (sid or url(p.url) == url(original))]
    choices = {(p.session_id, url(p.local_url)) for p in records}
    local = next(iter(choices)) if len(choices) == 1 else None
    if not local and not advertised:
        return original, advertised
    async def choose():
        if local and await healthy(local[1], local[0]):
            return local[1]
        expected = sid or (local[0] if local else "")
        if expected and not await healthy(original, expected, timeout=PUBLIC_DEADLINE):
            raise ValueError("no reachable route serves the invited session; no invite was submitted")
        return original
    # The CLI is synchronous. If an embedding caller already has an event
    # loop, run the bounded probe in a separate loop rather than nesting it.
    return _synchronous(choose), advertised


def _synchronous(operation):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation())
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(operation())).result()


def after_join(profile):
    """The successful original join response can now authorize a local route."""
    return _synchronous(lambda: _preferred_local(profile, load(profile)))


async def healthy(candidate, session_id, *, verify=True, timeout=LOCAL_DEADLINE):
    """Probe without credentials; TLS authenticates the channel before health."""
    import httpx
    from .compatibility import request_headers
    async def read():
        async with httpx.AsyncClient(verify=verify, timeout=timeout,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=2)) as client:
            async with client.stream("GET", candidate + EXT_PREFIX + "/health",
                    headers=request_headers(extra={"Accept-Encoding": "identity"}),
                    follow_redirects=False) as response:
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                    return False
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > MAX_ROUTE_BYTES:
                        return False
                    data.extend(chunk)
                value = json.loads(data)
                return isinstance(value, dict) and value.get("session_id") == session_id
    try:
        return await asyncio.wait_for(read(), timeout)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, RecursionError, ssl.SSLError):
        return False


async def _preferred_local(profile, state):
    # One 750ms budget for ALL local candidates, not 750ms per address.
    deadline = asyncio.get_running_loop().time() + LOCAL_DEADLINE
    local = trusted_local(profile.session_id)
    remaining = deadline - asyncio.get_running_loop().time()
    if local and remaining > 0 and await healthy(local, profile.session_id, timeout=remaining):
        return local
    secure = state.get("secure_local_url")
    remaining = deadline - asyncio.get_running_loop().time()
    if secure and remaining > 0:
        try:
            context = tls_context(state.get("local_certificate"))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0 and await healthy(secure, profile.session_id, verify=context,
                             timeout=remaining):
                return secure
        except (ValueError, ssl.SSLError):
            pass
    return ""


async def select(profile):
    """Spend at most 750 ms on local probes, then 5 s on all public fallbacks."""
    state = load(profile)
    if not state:
        # A legacy profile still knows its session id. A reused port must not
        # receive its bearer merely because there is no newer route sidecar.
        candidate = url(profile.url)
        if candidate and await healthy(candidate, profile.session_id, timeout=PUBLIC_DEADLINE):
            return profile.url
        return ""
    local = await _preferred_local(profile, state)
    if local:
        return local
    # A discovered plaintext URL never inherits the original user's trust.
    # The public fallback can be changed by the authenticated hub only when it
    # remains HTTPS; the original explicit URL retains legacy HTTP behavior.
    candidates = [state.get("public_url"), state.get("original_user_url")]
    seen = set()
    viable = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate != state.get("original_user_url") and not candidate.startswith("https://"):
            continue
        viable.append(candidate)
    deadline = asyncio.get_running_loop().time() + PUBLIC_DEADLINE
    for index, candidate in enumerate(viable):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        # Share the finite public budget so a silent new address cannot use
        # it all before the original explicit URL gets a chance to recover.
        timeout = remaining / (len(viable) - index)
        if await healthy(candidate, profile.session_id, timeout=timeout):
            return candidate
    return ""
