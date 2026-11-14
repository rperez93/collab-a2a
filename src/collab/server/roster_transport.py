"""Transport savings for the two finite, authenticated roster responses.

This is deliberately a response helper rather than response middleware: the
same application serves SSE and file streams, where collecting a body before
compressing it would trade a few bytes for delayed delivery and unbounded RAM.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

from collab import roster_clocks

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Small rosters cost more in framing/CPU than they save. Level 1 captures the
# repetitive task/participant keys without level 9's work on every refresh.
# The upper bound limits the extra compression allocation and CPU for a huge
# board; its ordinary, uncompressed response remains backward compatible.
MIN_GZIP_BYTES = 1024
MAX_GZIP_BYTES = 2 * 1024 * 1024


def _encoding_qualities(value: str) -> dict[str, float]:
    """Respect explicit refusals and preferences, including wildcard quality.

    A substring check accepts «gzip;q=0» and even «notgzip». An explicit
    identity preference also wins over a lower-quality gzip offer. Without
    such a preference, identity remains the fallback rather than preventing
    compression for the usual «gzip, deflate» client header.
    """
    qualities: dict[str, float] = {}
    for offer in value.split(","):
        coding, *parameters = offer.strip().lower().split(";")
        quality = 1.0
        for parameter in parameters:
            name, _, raw = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
                if not 0 <= quality <= 1:
                    quality = 0.0
        qualities[coding.strip()] = quality
    return qualities


def _roster_parts(payload: dict[str, Any], *, clocks: bool = False) -> tuple[list[bytes], str]:
    """Encode each value once; omit only supported observation clocks from the tag.

    The opt-in clock mode transports authoritative pace in bounded headers on
    304 responses. Its separate digest domain prevents legacy clients reusing
    a validator whose omitted pace they cannot refresh. Traversal stops after
    the three protocol objects: clock-shaped names inside user data remain
    ordinary state, and large task values are encoded only once.
    """
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                               separators=(",", ":"), sort_keys=True)
    batch = payload.get("batch")
    pace = batch.get("pace") if isinstance(batch, dict) else None
    supported = clocks and roster_clocks.clock_headers(batch) is not None
    ignored = {("server_time",), ("batch", "counted_at")}
    if supported:
        ignored.add(("batch", "pace", "since_last"))
        if pace["state"] == "running":
            ignored.update(("batch", "pace", key) for key in ("elapsed", "eta"))
            if pace["overdue"]:
                ignored.add(("batch", "pace", "per_task"))

    def fields(obj, path=()):
        wire, semantic = [b"{"], [b"{"]
        for key, value in sorted(obj.items()):
            prefix = encoder.encode(key).encode("utf-8") + b":"
            here = (*path, key)
            if here in (("batch",), ("batch", "pace")) and isinstance(value, dict):
                encoded, hashed = fields(value, here)
            else:
                encoded = [encoder.encode(value).encode("utf-8")]
                hashed = encoded
            if len(wire) > 1:
                wire.append(b",")
            wire.extend((prefix, *encoded))
            if here not in ignored:
                if len(semantic) > 1:
                    semantic.append(b",")
                semantic.extend((prefix, *hashed))
        wire.append(b"}")
        semantic.append(b"}")
        return wire, semantic

    parts, semantic = fields(payload)
    digest = hashlib.sha256(b"roster-clocks-v1:" if clocks else b"")
    for part in semantic:
        digest.update(part)
    return parts, 'W/"' + digest.hexdigest() + '"'


def roster_response(payload: dict[str, Any], request: Request, *,
                    conditional: bool = False) -> Response:
    """Send a private roster, with optional validation and negotiated gzip.

    There is no shared payload cache. Authentication and viewer filtering have
    already happened in the route, and every request computes its own tag.
    Excluding the two observation clocks permits an unchanged roster to validate,
    while last_seen, feed connectivity, identity, task progress and seq still
    invalidate it. The tag is weak because server_time changes across reads
    and gzip and identity use different bytes for the same state.
    """
    headers = {"Cache-Control": "private, no-cache",
               "Vary": "Authorization, Accept-Encoding, Collab-Roster-Clocks"}
    qualities = _encoding_qualities(request.headers.get("accept-encoding", ""))
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    identity_quality = qualities.get("identity", 0.0 if qualities.get("*") == 0 else 1.0)
    if gzip_quality <= 0 and identity_quality <= 0:
        return Response(status_code=406, headers=headers)
    if conditional:
        clocks = request.headers.get(roster_clocks.OFFER) == "1"
        parts, tag = _roster_parts(payload, clocks=clocks)
        headers["ETag"] = tag
        # If-None-Match uses weak comparison for GET. Each tag is a digest,
        # so commas cannot occur within it and a list is safe to split here.
        offered = request.headers.get("if-none-match", "")
        if any(part.strip() == "*" or part.strip().removeprefix("W/") == tag[2:]
               for part in offered.split(",")):
            clock_values = roster_clocks.clock_headers(payload.get("batch")) if clocks else None
            has_pace = isinstance(payload.get("batch"), dict) and payload["batch"].get("pace") is not None
            # Unsupported future/malformed pace needs its full JSON body.
            if not clocks or not has_pace or clock_values is not None:
                return Response(status_code=304, headers={**headers, **(clock_values or {})})
        response = Response(b"".join(parts), media_type="application/json", headers=headers)
        del parts
    else:
        response = JSONResponse(payload, headers=headers)
    size = len(response.body)
    if MIN_GZIP_BYTES <= size <= MAX_GZIP_BYTES and gzip_quality > 0 \
            and gzip_quality >= qualities.get("identity", 0.0):
        compressed = gzip.compress(response.body, compresslevel=1, mtime=0)
        if len(compressed) < size or identity_quality <= 0:
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    # Compression is deliberately unavailable outside the size budget. Sending
    # identity anyway would violate an explicit refusal (including «*;q=0»),
    # so return an empty negotiation failure rather than forbidden JSON.
    if "Content-Encoding" not in response.headers and identity_quality <= 0:
        return Response(status_code=406, headers=headers)
    return response
