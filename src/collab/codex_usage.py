"""Read usage for the exact Codex thread that launched this participant.

The quota API is account-wide and cannot name the coding model or its context.
Resolve only CODEX_THREAD_ID through thread/read, then validate the returned
file's session header before reading bounded telemetry records. Never discover
threads by scanning the sessions directory or import conversation text.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import stat
import time

from .telemetry import number

# Two MiB covers recent token events without rereading an ever-growing log on
# each heartbeat. A missing record stays unknown until a new event is written.
TAIL_BYTES = 2 * 1024 * 1024
RECORD_BYTES = 256 * 1024


def read_snapshot(path: str, thread_id: str) -> dict:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            header = json.loads(stream.readline(RECORD_BYTES))
            if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != thread_id:
                return {}
            size = os.fstat(stream.fileno()).st_size
            header_end = stream.tell()
            start = max(header_end, size - TAIL_BYTES)
            stream.seek(start)
            if start > header_end:
                stream.readline(RECORD_BYTES)  # discard a possible partial record
            data = stream.read(TAIL_BYTES)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    # BOTH RECORDS FROM ONE BACKWARD PASS over the tail already read. The
    # newest `token_count` gives the counters; the newest `turn_context` gives
    # the model that ran the last turn. Returning at the first token_count —
    # as this did — never reached the turn record, which is written BEFORE the
    # turn's token events, so the model came only from thread/read: the
    # thread's setting, not the model that answered. A `/model` switch shows
    # in the next turn's record, so this follows it within one probe cycle.
    usage: dict | None = None
    model: dict | None = None
    for line in reversed(data.splitlines()):
        if usage is not None and model is not None:
            break
        if len(line) > RECORD_BYTES:
            continue
        if model is None and b'"turn_context"' in line:
            model = _turn_model(line)
            if model is not None:
                continue
        if usage is None and b'"token_count"' in line:
            usage = _token_usage(line)
    out = usage or {}
    if model:
        out.update(model)
    return out


def _turn_model(line: bytes) -> dict | None:
    """`{model, model_observed_at}` from one `turn_context` record, or None.

    Verified against codex-cli 0.169.0 rollouts (2026-11-13): `{"timestamp",
    "type": "turn_context", "payload": {"model": "<slug>", …}}`, one per turn.
    Stamped with the TURN's time, not ours: it says which model ran then, and
    an idle agent's model is as old as its last turn.
    """
    try:
        event = json.loads(line)
        if event.get("type") != "turn_context":
            return None
        name = (event.get("payload") or {}).get("model")
        if not isinstance(name, str) or not name.strip():
            return None
        stamp = datetime.datetime.fromisoformat(
            event["timestamp"].replace("Z", "+00:00")).timestamp()
        return {"model": name.strip()[:150], "model_observed_at": stamp}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def _token_usage(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
        payload = event.get("payload", {})
        if event.get("type") != "event_msg" or payload.get("type") != "token_count":
            return None
        info = payload.get("info") or {}
        total = info.get("total_token_usage") or {}
        last = info.get("last_token_usage") or {}
        stamp = datetime.datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
        out = {"source": "codex-own-thread", "observed_at": stamp}
        for target, native in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
                               ("tokens_cached", "cached_input_tokens"), ("tokens_cache_write", "cache_write_input_tokens")):
            if number(total.get(native)) is not None:
                out[target] = total[native]
        if "tokens_in" in out and "tokens_cached" in out:
            out["tokens_in"] = max(out["tokens_in"] - out["tokens_cached"], 0)
        if number(last.get("total_tokens")) is not None:
            out["context_tokens"] = last["total_tokens"]
        limit = number(info.get("model_context_window"))
        if limit:
            out["context_limit"] = limit
            if "context_tokens" in out:
                out["context_pct"] = min(100, 100 * out["context_tokens"] / limit)
        return out
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def current_usage() -> dict:
    from .hosttool import detect
    if detect() != "codex":
        # A Claude process launched from Codex inherits CODEX_THREAD_ID too.
        # The immediate host marker wins; inherited identity is not ownership.
        return {}
    thread_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not thread_id:
        return {}
    from .quotas import from_codex
    result, why = from_codex(timeout=3, request={"method": "thread/read", "params": {
        "threadId": thread_id, "includeTurns": False}})
    thread = result.get("thread") or {}
    if why or thread.get("id") != thread_id:
        return {}
    out = read_snapshot(thread["path"], thread_id) if thread.get("path") else {}
    # THE THREAD'S SETTING ONLY WHEN NO TURN SAID WHICH MODEL RAN. A fresh
    # thread has no turn yet, and a tail that rolled past its last turn record
    # has none in reach; otherwise the turn record is the better witness.
    if thread.get("model") and "model" not in out:
        out.update(model=thread["model"], model_observed_at=time.time())
    return out
