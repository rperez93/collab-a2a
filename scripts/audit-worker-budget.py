#!/usr/bin/env python3
"""Summarize one own-session `collab worker status --json` without conversation text.

Example (run from the participant's normal working directory):
  collab worker status --json | python3 /checkout/scripts/audit-worker-budget.py \
      --limit 60 --window 3600

The limit/window must match `collab config worker_max_attempts` and
`collab config worker_budget_window`. This does not change settings or run a model.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time

#: The whole input: one status snapshot is far smaller.
MAX_INPUT = 2_000_000
#: How long the WHOLE input may take to arrive, however it arrives. A byte cap
#: alone left `read()` waiting for ever on a producer that opened the pipe and
#: never wrote or closed it (friday's review of #191); a per-read timeout would
#: still let a trickle run on indefinitely. `collab worker status --json`
#: answers in well under a second.
INPUT_DEADLINE = 10.0
CHUNK = 65536


def read_input(stream, *, limit=MAX_INPUT, deadline=INPUT_DEADLINE):
    """All of `stream`, or ValueError: over `limit` bytes, or not complete
    within `deadline` seconds of starting, whatever arrives in between.

    A blocking read cannot enforce a deadline by itself, so it runs on a
    daemon thread and the clock is watched here. A thread left blocked on a
    silent pipe dies with the process, which exits on the refusal.
    """
    chunks: queue.Queue = queue.Queue(maxsize=4)  # bounded: the reader waits for us

    # THE RAW DESCRIPTOR, NOT THE BUFFERED STREAM. A daemon thread blocked in
    # `sys.stdin.buffer.read` holds that object's lock, and the interpreter
    # aborts at exit trying to take it (SIGABRT, exit 134 — measured here on
    # the first version of this fix). `os.read` holds no Python-level lock.
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None

    def reader():
        try:
            while True:
                chunk = os.read(fd, CHUNK) if fd is not None else stream.read(CHUNK)
                chunks.put(chunk)
                if not chunk:
                    return
        except Exception as exc:  # surfaced to the consumer, never swallowed
            chunks.put(exc)

    threading.Thread(target=reader, name="input-reader", daemon=True).start()
    ends = time.monotonic() + deadline
    data = bytearray()
    while True:
        left = ends - time.monotonic()
        if left <= 0:
            raise ValueError(f"no complete input within {deadline:g} s; pipe one status snapshot")
        try:
            chunk = chunks.get(timeout=left)
        except queue.Empty:
            continue  # the loop's own clock check refuses it
        if isinstance(chunk, Exception):
            raise ValueError(f"cannot read input: {type(chunk).__name__}")
        if not chunk:
            return bytes(data)
        data += chunk
        if len(data) > limit:
            raise ValueError("input exceeds 2 MB; use one worker status snapshot")


def number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def identifier(value):
    return value if isinstance(value, str) and len(value) <= 200 else None


def counter(value):
    return value if isinstance(value, int) and number(value) and value >= 0 else None


def summarize(state, *, now, limit, window):
    if not isinstance(state, dict):
        raise ValueError("expected a worker status object")
    if (not number(now) or not number(window) or window <= 0
            or counter(limit) is None or limit < 1):
        raise ValueError("positive limit/window and a finite capture time are required")
    raw = state.get("model_attempts")
    history = (raw if isinstance(raw, list) and all(number(stamp) for stamp in raw)
               else None)
    active = (sorted(stamp for stamp in history if stamp > now - window)
              if history is not None else None)
    cfg = state.get("config") if isinstance(state.get("config"), dict) else {}
    usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
    observed = usage.get("tokens_observed_at", usage.get("observed_at"))
    retry = state.get("retry_at")
    total = state.get("attempts_total")
    turns = state.get("turn")
    return {
        "captured_at": now,
        "agent": identifier(cfg.get("agent")),
        "configured_model": identifier(cfg.get("model")),
        "usage_model": identifier(usage.get("usage_model")),
        "limit": limit,
        "window_seconds": window,
        "attempts_lifetime": counter(total),
        "committed_turns_lifetime": counter(turns),
        # reserve_turn also counts future timestamps after a wall-clock rollback.
        "future_reservations": sum(stamp > now for stamp in history) if history is not None else None,
        "attempts_in_window": len(active) if active is not None else None,
        "remaining_attempts": max(0, limit - len(active)) if active is not None else None,
        "first_attempt_in_window": active[0] if active else None,
        "last_attempt_in_window": active[-1] if active else None,
        "window_attempt_span_seconds": active[-1] - active[0] if active else None,
        "reported_retry_delay_seconds": max(0, retry - now) if number(retry) else None,
        "budget_error_reported": state.get("error") == "Worker hourly turn limit reached",
        "tokens_observed_lifetime": {
            key: counter(usage.get(key))
            for key in ("tokens_in", "tokens_out", "tokens_cached")
        },
        "usage_observed_at": observed if number(observed) else None,
        # 2.1.2 has no durable per-attempt outcome ledger. Empty outbox is NOT
        # evidence that a call was a no-op: delivery deletes acknowledged rows.
        "calls_with_replies": None,
        "calls_with_escalations": None,
        "calls_without_actions": None,
        "failed_calls": None,
        # Prices and billing coverage are not available from this status input.
        "cost_usd": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--window", type=float, required=True)
    parser.add_argument("--at", type=float, help="capture time of a saved input, Unix seconds")
    parser.add_argument("--input-deadline", type=float, default=INPUT_DEADLINE,
                        help="seconds the whole input may take to arrive (default 10, at most 60)")
    args = parser.parse_args()
    if not (math.isfinite(args.input_deadline) and 0 < args.input_deadline <= 60):
        parser.exit(2, "--input-deadline must be more than 0 and at most 60 seconds\n")
    try:
        data = read_input(sys.stdin.buffer, deadline=args.input_deadline)
        result = summarize(json.loads(data), now=time.time() if args.at is None else args.at,
                           limit=args.limit, window=args.window)
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        parser.exit(2, f"cannot summarize worker status: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
