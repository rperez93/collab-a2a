"""Bounded authoritative pace observations accompanying a conditional roster.

The participant body can stay unchanged while a running batch's ETA advances.
An opt-in client receives those small observations on a 304, rather than
resetting its local fetch clock over an old ETA. Unknown schemas keep the full
JSON path; this extension must never silently discard a new hub field.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

OFFER = "Collab-Roster-Clocks"
PACE = "Collab-Batch-Pace"
COUNTED_AT = "Collab-Batch-Counted-At"
MAX_HEADER_BYTES = 2048
_FIELDS = {"state", "counted", "elapsed", "eta", "overdue", "per_task", "since_last"}


def valid_pace(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return False
    if value["state"] not in ("running", "done", "closed") or type(value["overdue"]) is not bool:
        return False
    if type(value["counted"]) is not int or not 0 <= value["counted"] <= 2**53:
        return False
    return all(field is None or (type(field) in (int, float) and
               0 <= field <= 1e100 and math.isfinite(field))
               for name in ("elapsed", "eta", "per_task", "since_last")
               for field in (value[name],))


def clock_headers(batch: Any) -> dict[str, str] | None:
    if not isinstance(batch, dict) or not valid_pace(batch.get("pace")):
        return None
    stamp = batch.get("counted_at")
    if type(stamp) not in (int, float) or not 0 <= stamp <= 1e100 or not math.isfinite(stamp):
        return None
    headers = {PACE: json.dumps(batch["pace"], allow_nan=False, separators=(",", ":")),
               COUNTED_AT: json.dumps(stamp, allow_nan=False)}
    return headers if sum(len(v) for v in headers.values()) <= MAX_HEADER_BYTES else None


def read_clocks(headers: Mapping[str, str]) -> dict[str, Any] | None:
    """Reject partial observations before the daemon renews any freshness stamp."""
    pace, stamp = headers.get(PACE), headers.get(COUNTED_AT)
    if pace is None or stamp is None or len(pace) + len(stamp) > MAX_HEADER_BYTES:
        return None
    try:
        batch = {"pace": json.loads(pace), "counted_at": json.loads(stamp)}
        return batch if clock_headers(batch) is not None else None
    except (ValueError, RecursionError, OverflowError):
        return None
