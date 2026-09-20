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
    for line in reversed(data.splitlines()):
        if len(line) > RECORD_BYTES or b'"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
            payload = event.get("payload", {})
            if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
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
            continue
    return {}


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
    if thread.get("model"):
        out.update(model=thread["model"], model_observed_at=time.time())
    return out
