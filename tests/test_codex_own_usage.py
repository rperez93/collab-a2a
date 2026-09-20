"""Own-thread telemetry is bounded and never borrows another thread's usage."""
import json
import os
import resource
import time

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
