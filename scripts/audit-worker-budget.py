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
import sys
import time


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
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("input exceeds 2 MB; use one worker status snapshot")
        result = summarize(json.loads(data), now=time.time() if args.at is None else args.at,
                           limit=args.limit, window=args.window)
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        parser.exit(2, f"cannot summarize worker status: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
