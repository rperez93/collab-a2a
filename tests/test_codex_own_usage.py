"""Own-thread telemetry is bounded and never borrows another thread's usage."""
import json
import os
import resource
import time

import pytest

from collab import codex_usage


def log(path, ident="mine", prefix=b""):
    event = {"timestamp": "2026-09-20T22:32:12Z", "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50},
            "last_token_usage": {"total_tokens": 500}, "model_context_window": 1000}}}
    path.write_bytes(json.dumps({"type": "session_meta", "payload": {"id": ident}}).encode()
                     + b"\n" + prefix + json.dumps(event).encode() + b"\n")


def test_only_the_exact_thread_can_supply_counters(tmp_path):
    path = tmp_path / "rollout"
    log(path)
    assert codex_usage.read_snapshot(str(path), "other") == {}
    stats = codex_usage.read_snapshot(str(path), "mine")
    assert stats["tokens_in"] == 200 and stats["tokens_cached"] == 800
    assert stats["context_pct"] == 50
    assert stats["observed_at"] == 1789943532


def test_a_huge_record_cannot_make_telemetry_reread_the_whole_log(tmp_path):
    """A 32 MiB unbroken record costs at most a 2 MiB tail and one header."""
    path = tmp_path / "rollout"
    log(path, prefix=b"x" * (32 * 1024 * 1024) + b"\n")
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    stats = codex_usage.read_snapshot(str(path), "mine")
    after = resource.getrusage(resource.RUSAGE_SELF)
    assert stats["context_pct"] == 50
    assert time.monotonic() - started < 1
    assert after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime < .5
    assert after.ru_maxrss - before.ru_maxrss < 16 * 1024


def test_a_fifo_returned_as_a_log_never_blocks(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    started = time.monotonic()
    assert codex_usage.read_snapshot(str(path), "mine") == {}
    assert time.monotonic() - started < .5


def test_thread_lookup_uses_only_the_inherited_identity(monkeypatch, tmp_path):
    from collab import quotas
    path = tmp_path / "log"
    log(path)
    monkeypatch.setenv("CODEX_THREAD_ID", "mine")
    calls = []
    def lookup(**kwargs):
        calls.append(kwargs)
        return {"thread": {"id": "mine", "model": "actual-model", "path": str(path)}}, ""
    monkeypatch.setattr(quotas, "from_codex", lookup)
    assert codex_usage.current_usage()["model"] == "actual-model"
    assert calls == [{"timeout": 3, "request": {"method": "thread/read", "params": {
        "threadId": "mine", "includeTurns": False}}}]


def test_claude_cannot_borrow_its_codex_parents_thread(monkeypatch):
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CODEX_THREAD_ID', 'inherited-parent')
    assert codex_usage.current_usage() == {}


def turn(model, when):
    return json.dumps({"timestamp": when, "type": "turn_context",
                       "payload": {"model": model, "cwd": "/x"}}).encode() + b"\n"


def test_a_model_switched_between_turns_is_the_one_reported(monkeypatch, tmp_path):
    """The thread's setting is not the model that answered; the last turn's record is."""
    from collab import quotas
    path = tmp_path / "rollout"
    log(path)
    with path.open("ab") as fh:
        fh.write(turn("model-before", "2026-09-20T22:30:00Z"))
        fh.write(turn("model-after", "2026-09-20T22:40:00Z"))
    stats = codex_usage.read_snapshot(str(path), "mine")
    assert stats["model"] == "model-after"
    assert stats["model_observed_at"] == 1789944000, "stamped with the turn, not with now"
    assert stats["context_pct"] == 50, "the counters still come from the token record"

    monkeypatch.setenv("CODEX_THREAD_ID", "mine")
    monkeypatch.setattr(quotas, "from_codex", lambda **kw: (
        {"thread": {"id": "mine", "model": "thread-setting", "path": str(path)}}, ""))
    assert codex_usage.current_usage()["model"] == "model-after"


def test_a_tail_of_malformed_turn_records_costs_a_bounded_pass(tmp_path):
    """Looking past the token record for a turn must not become an unbounded parse.

    Measured on the machine this was written on: 2 MiB of 6.6 KB look-alike
    records cost 0.009 s of CPU and no measurable growth in peak RSS; the
    bounds below leave a wide margin.
    """
    path = tmp_path / "rollout"
    junk = (b'{"type":"turn_context","payload":{"model":' + b'1' * 6600 + b'}}\n')
    log(path)
    with path.open("ab") as fh:
        fh.write(junk * (codex_usage.TAIL_BYTES // len(junk) + 50))
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    stats = codex_usage.read_snapshot(str(path), "mine")
    after = resource.getrusage(resource.RUSAGE_SELF)
    assert "model" not in stats, "no model was readable, so none is reported"
    assert time.monotonic() - started < 1
    assert after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime < .5
    assert after.ru_maxrss - before.ru_maxrss < 16 * 1024


def test_the_real_order_turn_record_then_tokens_still_finds_the_model(tmp_path):
    """Codex writes `turn_context` BEFORE the turn's token events.

    The pass meets the token record first, walking backwards. A pass that
    stopped there — the old behaviour — returns no model at all, which the
    test above, with the records in the opposite order, could not see.
    """
    path = tmp_path / "rollout"
    meta = json.dumps({"type": "session_meta", "payload": {"id": "mine"}}).encode() + b"\n"
    def tokens(when):
        return json.dumps({"timestamp": when, "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 1},
                                             "last_token_usage": {"total_tokens": 11},
                                             "model_context_window": 100}}}).encode() + b"\n"
    path.write_bytes(meta + turn("model-a", "2026-09-20T22:30:00Z") + tokens("2026-09-20T22:31:00Z")
                     + turn("model-b", "2026-09-20T22:40:00Z") + tokens("2026-09-20T22:41:00Z"))
    stats = codex_usage.read_snapshot(str(path), "mine")
    assert stats["model"] == "model-b"
    assert stats["observed_at"] == 1789944060, "the newest token record, not the older one"


def test_gemini_inside_codex_reads_no_codex_thread(monkeypatch):
    """The inherited CODEX_THREAD_ID is the parent's; the model there is not ours."""
    from collab import hosttool, quotas
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setenv("GEMINI_CLI", "1")
    monkeypatch.setattr(quotas, "from_codex", lambda **kw: pytest.fail("asked codex"))
    for nearest in ("gemini", ""):
        monkeypatch.setattr(hosttool, "nearest_agent", lambda n=nearest: n)
        assert codex_usage.current_usage() == {}
