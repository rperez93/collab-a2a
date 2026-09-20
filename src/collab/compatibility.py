"""The Collab session boundary, distinct from A2A transport versions."""
from __future__ import annotations

from typing import Any

# Version 2 changes session ownership and recovery semantics. A2A 0.3/1.0
# describes the transport only and cannot prove a peer understands those
# semantics. Both the protocol major and a stable package version are required:
# a guessed protocol field must not make a pre-v2 package look compatible.
PROTOCOL_MAJOR = 2
MINIMUM_VERSION = '2.0.0'
PROTOCOL_HEADER = 'Collab-Protocol-Major'
VERSION_HEADER = 'Collab-Version'


def request_headers(token: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Every request proves the running client, not the token's creation date.

    A resumed v1 database contains valid bearer tokens. Checking only /join
    would let its old daemons reconnect indefinitely without upgrading.
    """
    from . import __version__
    headers = {**(extra or {}), PROTOCOL_HEADER: str(PROTOCOL_MAJOR), VERSION_HEADER: __version__}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def header_advertisement(headers) -> dict[str, Any]:
    major = headers.get(PROTOCOL_HEADER)
    return {'protocol_major': int(major) if isinstance(major, str) and major == str(PROTOCOL_MAJOR) else None,
            'version': headers.get(VERSION_HEADER)}


async def check_host(client, base_url: str) -> None:
    """Bounded public preflight before a daemon exposes its authenticated client.

    No bearer is submitted, and redirects cannot silently move the proof to
    a different endpoint. An overall deadline also bounds dribbling responses.
    """
    import asyncio
    import json
    from .protocol import EXT_PREFIX

    async def read():
        async with client.stream('GET', base_url.rstrip('/') + EXT_PREFIX + '/compatibility',
                headers=request_headers(extra={'Accept-Encoding': 'identity'}),
                timeout=5, follow_redirects=False) as response:
            if response.status_code != 200:
                raise RuntimeError('host compatibility check failed; upgrade host and guest to stable Collab 2.x and restart the hub')
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise RuntimeError('host sent compressed compatibility data')
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > 65536:
                    raise RuntimeError('host compatibility response exceeds 64 KiB')
                data.extend(chunk)
            try:
                peer = json.loads(data)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise RuntimeError('host returned invalid compatibility data') from exc
            if reason := incompatibility(peer, role='host'):
                raise RuntimeError(reason)
    await asyncio.wait_for(read(), timeout=15)


def advertisement() -> dict[str, Any]:
    from . import __version__
    return {'protocol_major': PROTOCOL_MAJOR, 'version': __version__,
            'minimum_version': MINIMUM_VERSION}


def incompatibility(peer: Any, *, role: str) -> str:
    major = peer.get('protocol_major') if isinstance(peer, dict) else None
    # bool is an int subclass, and coercing strings/floats would accept a
    # malformed handshake as authoritative. Only the documented integer fits.
    if type(major) is int and major > PROTOCOL_MAJOR:
        return (f'this {role} requires a newer Collab protocol ({major}); '
                'upgrade your Collab installation before joining')
    if type(major) is int and major == PROTOCOL_MAJOR:
        from packaging.version import InvalidVersion, Version
        try:
            raw = peer.get('version')
            version = Version(raw) if isinstance(raw, str) else None
        except InvalidVersion:
            version = None
        if (version is not None and version >= Version(MINIMUM_VERSION)
                and version.major == PROTOCOL_MAJOR
                and not version.is_prerelease and not version.is_devrelease):
            return ''
    return (f'this {role} does not advertise a supported stable Collab version '
            f'and protocol {PROTOCOL_MAJOR}; '
            f'Collab {MINIMUM_VERSION} or newer stable 2.x is required on both sides — '
            'upgrade the host and guest, then restart the host and join again')
