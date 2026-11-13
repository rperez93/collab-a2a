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


def _roster_parts(payload: dict[str, Any]) -> tuple[list[bytes], str]:
    """Encode each value once, sharing its bytes between the body and weak tag.

    Two observation clocks advance even when the room stands still:
    server_time and batch.counted_at. A started or even CLOSED batch therefore
    defeated the old validator on every read. Exclude exactly those clocks
    from the semantic digest, while retaining both in the JSON response.

    Encoding a second whole canonical payload cost another large serialization
    on the hub event loop. Hash sorted field/value chunks as they are encoded,
    then reuse those same chunks for the wire body. Only the small batch clock
    field is spliced back before that object's final brace; no regex searches,
    recursive clock-name filtering, or second whole-payload buffer is needed.
    """
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                               separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(b"{")
    parts = [b"{"]
    semantic_fields = 0
    for key, value in sorted(payload.items()):
        prefix = encoder.encode(key).encode("utf-8") + b":"
        if len(parts) > 1:
            parts.append(b",")
        parts.append(prefix)
        if key == "batch" and isinstance(value, dict) and "counted_at" in value:
            batch = {name: field for name, field in value.items() if name != "counted_at"}
            encoded = encoder.encode(batch).encode("utf-8")
            parts.extend((encoded[:-1], b"," if batch else b"",
                          b'"counted_at":', encoder.encode(value["counted_at"]).encode("utf-8"),
                          b"}"))
        else:
            encoded = encoder.encode(value).encode("utf-8")
            parts.append(encoded)
        if key != "server_time":
            if semantic_fields:
                digest.update(b",")
            digest.update(prefix)
            digest.update(encoded)
            semantic_fields += 1
        # The batch's clockless bytes are not kept beside its wire slice.
        del encoded
    parts.append(b"}")
    digest.update(b"}")
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
               "Vary": "Authorization, Accept-Encoding"}
    qualities = _encoding_qualities(request.headers.get("accept-encoding", ""))
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    identity_quality = qualities.get("identity", 0.0 if qualities.get("*") == 0 else 1.0)
    if gzip_quality <= 0 and identity_quality <= 0:
        return Response(status_code=406, headers=headers)
    if conditional:
        parts, tag = _roster_parts(payload)
        headers["ETag"] = tag
        # If-None-Match uses weak comparison for GET. Each tag is a digest,
        # so commas cannot occur within it and a list is safe to split here.
        offered = request.headers.get("if-none-match", "")
        if any(part.strip() == "*" or part.strip().removeprefix("W/") == tag[2:]
               for part in offered.split(",")):
            return Response(status_code=304, headers=headers)
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
