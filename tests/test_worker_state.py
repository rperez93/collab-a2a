"""A background conversation must survive interruption without losing decisions."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from collab import cli, worker


def configured(tmp_path):
    store = worker.Store(tmp_path)
    store.configure({"agent": "codex", "scope": "Coordinate test ownership"})
    return store


def turn(store, **changes):
    args = dict(expected_cursor=0, cursor=3, summary="Alice owns tests",
                replies=[{"text": "Please cover tests", "to": "alice", "room": "main"}],
                escalations=[{"reason": "decision", "question": "Approve the split?", "seq": 2}])
    args.update(changes)
    return store.commit_turn(**args)


def test_worker_state_survives_restart_and_keeps_the_main_inbox_separate(tmp_path):
    inbox = tmp_path / "inbox.db"
    inbox.write_bytes(b"the main agent has not read this")
    store = configured(tmp_path)
    assert turn(store)
    restored = worker.Store(tmp_path)
    assert restored.snapshot()["cursor"] == 3
    assert restored.pending()[0]["question"] == "Approve the split?"
    assert restored.outbox()[0]["text"] == "Please cover tests"
    assert inbox.read_bytes() == b"the main agent has not read this"
    assert restored.path.stat().st_mode & 0o777 == 0o600


def test_cursor_cas_prevents_duplicate_turns_from_concurrent_workers(tmp_path):
    store = configured(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: turn(worker.Store(tmp_path)), range(8)))
    assert outcomes.count(True) == 1
    assert len(store.pending()) == len(store.outbox()) == 1


def test_context_only_turns_are_also_committed_once(tmp_path):
    store = configured(tmp_path)
    context_id = store.context("Please coordinate the test split")
    snapshot = store.snapshot()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: turn(worker.Store(tmp_path), cursor=0,
            escalations=[], context_ids=[context_id], expected_turn=snapshot["turn"]), range(4)))
    assert outcomes.count(True) == 1
    assert len(store.outbox()) == 1


def test_off_and_reconfiguration_discard_a_stale_provider_response(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    store.off()
    assert not turn(store)
    store.configure({"agent": "claude", "scope": "Coordinate docs"})
    assert not turn(store, expected_generation=generation)
    assert store.snapshot()["cursor"] == 0
    assert not store.pending()


def test_decisions_and_new_context_are_consumed_only_by_a_successful_turn(tmp_path):
    store = configured(tmp_path)
    assert turn(store)
    decision = store.pending()[0]["id"]
    store.answer(decision, "Yes, own those tests")
    old = store.context("The implementation is ready")
    snapshot = store.snapshot()
    new = store.context("But the build now fails")
    assert not store.pending()
    assert snapshot["answers"][0]["prompt"]["seq"] == 2
    assert not turn(store, context_ids=[old], answer_ids=[decision])
    assert len(store.snapshot()["answers"]) == 1
    assert turn(store, expected_cursor=3, cursor=3, replies=[], escalations=[],
                context_ids=[old], answer_ids=[decision])
    assert store.snapshot()["answers"] == []
    assert [r["id"] for r in store.snapshot()["context"]] == [new]


def test_outbox_is_retained_until_delivery_acknowledgement(tmp_path):
    store = configured(tmp_path)
    turn(store)
    record = store.outbox()[0]
    store.off()
    assert store.outbox()[0] == record
    store.acknowledge(record["id"])
    store.acknowledge(record["id"])
    assert store.outbox() == []


def test_capacity_failure_rolls_back_the_entire_turn(tmp_path, monkeypatch):
    store = configured(tmp_path)
    context_id = store.context("authoritative context")
    monkeypatch.setattr(worker, "MAX_PENDING", 1)
    with pytest.raises(ValueError, match="too many pending"):
        turn(store, context_ids=[context_id])
    assert store.snapshot()["cursor"] == 0
    assert store.snapshot()["context"][0]["id"] == context_id
    assert store.outbox() == []
    assert store.pending() == []


def test_health_updates_neither_consume_messages_nor_claim_success(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    store.health(error="model unavailable", retry_at=100, expected_generation=generation)
    assert store.status()["last_progress"] == 0
    assert store.status()["error"] == "model unavailable"
    store.off()
    store.health(running=True, expected_generation=generation)
    assert not store.status()["running"]


@pytest.mark.parametrize("agent,model", [("codex", "gpt-5.6-luna"), ("claude", "haiku")])
def test_native_providers_have_explicit_cheap_defaults(tmp_path, agent, model):
    store = worker.Store(tmp_path)
    store.configure({"agent": agent, "scope": "Only coordinate ownership"})
    assert store.configuration()["model"] == model


@pytest.mark.parametrize("agent", ["cursor", "opencode"])
def test_providers_without_a_stable_cheap_alias_require_a_model(tmp_path, agent):
    store = worker.Store(tmp_path)
    with pytest.raises(ValueError, match="model"):
        store.configure({"agent": agent, "scope": "Only coordinate ownership"})
    store.configure({"agent": agent, "model": "chosen-model", "scope": "Coordinate"})
    assert store.configuration()["model"] == "chosen-model"


@pytest.mark.parametrize("config", [
    {"agent": "codex", "scope": ""},
    {"agent": "command", "scope": "Coordinate", "command": "sh -c bad"},
    {"agent": "command", "scope": "Coordinate", "command": ["bad\0command"]},
    {"agent": "codex", "scope": "Coordinate", "command": ["custom"]},
])
def test_invalid_configuration_never_replaces_a_working_worker(tmp_path, config):
    store = configured(tmp_path)
    old = store.configuration()
    with pytest.raises(ValueError):
        store.configure(config)
    assert store.configuration() == old


def test_parallel_context_writes_do_not_overwrite_each_other(tmp_path):
    configured(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda n: worker.Store(tmp_path).context(f"Context {n}"), range(40)))
    assert len(set(ids)) == 40
    assert len(worker.Store(tmp_path).snapshot()["context"]) == 40


def test_worker_cli_starts_the_daemon_and_reports_persisted_configuration(tmp_path, monkeypatch, capsys):
    profile = SimpleNamespace(dir=tmp_path)
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: profile)
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    started = []
    monkeypatch.setattr(cli.onboard, "ensure_daemon", lambda p: started.append(p))
    monkeypatch.setattr(cli, "is_running", lambda p: 123)
    assert cli.main(["worker", "start", "--agent", "codex", "--scope", "Coordinate tests"]) == 0
    assert started == [profile]
    assert cli.main(["worker", "context", "Tests pass"]) == 0
    assert cli.main(["worker", "status", "--json"]) == 0
    assert '"daemon_running": true' in capsys.readouterr().out
    assert cli.main(["worker", "off"]) == 0
    assert not worker.Store(tmp_path).status()["enabled"]


def test_worker_cli_rejects_shell_text_before_starting_a_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: SimpleNamespace(dir=tmp_path))
    assert cli.main(["worker", "start", "--agent", "command", "--scope", "Coordinate",
                     "--command", "echo hello"]) == 1
    assert "JSON argv array" in capsys.readouterr().err
    assert worker.Store(tmp_path).configuration() is None


def test_worker_cli_replies_only_to_an_existing_decision(tmp_path, monkeypatch, capsys):
    store = configured(tmp_path)
    turn(store)
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: SimpleNamespace(dir=tmp_path))
    assert cli.main(["worker", "reply", store.pending()[0]["id"], "Approved"]) == 0
    assert cli.main(["worker", "reply", "missing", "Approved"]) == 1
    assert "no pending worker decision" in capsys.readouterr().err


def test_unicode_context_is_bounded_in_bytes_instead_of_codepoints(tmp_path):
    store = configured(tmp_path)
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        store.context("😃" * (worker.MAX_TEXT // 4 + 1))
    assert store.snapshot()["context"] == []
    store.context("😃" * ((worker.MAX_TEXT - 2) // 4))


def test_pending_identical_decisions_keep_one_stable_id(tmp_path):
    store = configured(tmp_path)
    assert turn(store)
    old_id = store.pending()[0]["id"]
    assert turn(store, expected_cursor=3, cursor=3)
    assert [r["id"] for r in store.pending()] == [old_id]
    assert turn(store, expected_cursor=3, cursor=3,
                escalations=[{"reason": "conflict", "question": "Approve the split?", "seq": 2}])
    assert len(store.pending()) == 2


def test_model_attempt_budget_survives_failure_and_listener_restart(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    assert store.reserve_turn(expected_generation=generation, now=1000, limit=2) == 0
    store.health(error="provider failed")
    restarted = worker.Store(tmp_path)
    assert restarted.reserve_turn(expected_generation=generation, now=1001, limit=2) == 0
    assert restarted.reserve_turn(expected_generation=generation, now=1002, limit=2) == 4600
    status = restarted.status()
    assert status["error"] == "Worker hourly turn limit reached"
    assert status["retry_at"] == 4600
    assert not status["running"]
    assert status["cursor"] == 0
    assert status["last_progress"] == 0
    assert restarted.reserve_turn(expected_generation=generation, now=4600, limit=2) == 0


def test_concurrent_model_attempts_cannot_overspend_the_limit(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: worker.Store(tmp_path).reserve_turn(
            expected_generation=generation, now=1000, limit=3), range(12)))
    assert attempts.count(0.0) == 3
    assert attempts.count(4600) == 9


def test_disabled_or_reconfigured_workers_do_not_spend_a_stale_reservation(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    store.off()
    assert store.reserve_turn(expected_generation=generation, now=1000) is None
    store.configure({"agent": "claude", "scope": "Docs ownership"})
    assert store.reserve_turn(expected_generation=generation, now=1001) is None
    assert store.snapshot().get("model_attempts", []) == []


def test_reconfiguring_a_worker_does_not_reset_its_spending_budget(tmp_path):
    store = configured(tmp_path)
    generation = store.snapshot()["generation"]
    assert store.reserve_turn(expected_generation=generation, now=1000, limit=1) == 0
    store.configure({"agent": "claude", "scope": "Docs ownership"})
    generation = store.snapshot()["generation"]
    assert store.reserve_turn(expected_generation=generation, now=1001, limit=1) == 4600


def test_json_escaping_cannot_make_one_context_record_exceed_the_page(tmp_path):
    store = configured(tmp_path)
    with pytest.raises(ValueError, match="serialized JSON bytes"):
        store.context("a" + "\n" * 10000)
    with pytest.raises(ValueError, match="serialized JSON bytes"):
        store.context("a" + "\x00" * 3000)
    assert not store.snapshot()["context"]


def test_one_deferred_reply_keeps_its_identity_and_other_peers_remain_ready(tmp_path):
    store = configured(tmp_path)
    turn(store, replies=[{"text": "alice reply", "to": "alice", "room": "main"},
                         {"text": "bob reply", "to": "bob", "room": "main"}])
    records = {r["to"]: r for r in store.outbox()}
    old = records["alice"]
    assert store.defer(old["id"], retry_at=9000, error="alice is offline")
    restarted = worker.Store(tmp_path)
    records = {r["to"]: r for r in restarted.outbox()}
    assert records["alice"]["id"] == old["id"]
    assert records["alice"]["text"] == old["text"]
    assert records["alice"]["created_at"] == old["created_at"]
    assert records["alice"]["retry_at"] == 9000
    assert not records["bob"].get("retry_at")
    status = restarted.status()
    assert status["outbox_count"] == 2
    assert {r["to"]: r["error"] for r in status["outbox"]} == {"alice": "alice is offline", "bob": ""}
    assert all("text" not in r for r in status["outbox"])
    restarted.acknowledge(old["id"])
    assert not store.defer(old["id"], retry_at=9500, error="late failure")
    assert len(store.outbox()) == 1


@pytest.mark.parametrize("retry_at", [float("inf"), float("nan"), -1, "later"])
def test_invalid_retry_times_never_wedge_an_outbox_record(tmp_path, retry_at):
    store = configured(tmp_path)
    turn(store)
    old = store.outbox()[0]
    with pytest.raises(ValueError, match="retry time"):
        store.defer(old["id"], retry_at=retry_at, error="offline")
    assert store.outbox()[0] == old
