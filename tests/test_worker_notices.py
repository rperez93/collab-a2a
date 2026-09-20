"""Decisions remain actionable without making every peer message a main turn."""
import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from collab import cli, wake, worker, worker_notices as notices
from collab.client.inbox import Inbox
from collab.protocol import Envelope


def configured(root):
    store = worker.Store(root)
    store.configure({"agent": "codex", "scope": "Coordinate test ownership"})
    return store


def decision(store, text="private peer question"):
    snap = store.snapshot()
    assert store.commit_turn(expected_cursor=snap["cursor"], cursor=snap["cursor"],
        summary="", replies=[], escalations=[{"reason": "decision", "question": text, "seq": 0}])


def test_a_healthy_worker_produces_no_notices_even_with_unread_peer_chatter(tmp_path):
    configured(tmp_path)
    inbox = Inbox(tmp_path)
    try:
        inbox.record(Envelope(kind="chat", sender="peer", text="urgent", seq=1))
    finally:
        inbox.close()
    assert notices.claim(tmp_path) is None


def test_decisions_repeat_without_the_main_agent_reading_the_inbox(tmp_path):
    store = configured(tmp_path)
    decision(store)
    claim = notices.claim(tmp_path, now=1000)
    assert "private peer question" not in claim["text"]
    assert "collab worker pending" in claim["text"]
    assert notices.commit(tmp_path, claim["token"], now=1000)
    assert notices.claim(tmp_path, now=1299) is None
    assert notices.claim(tmp_path, now=1300)


def test_changed_decisions_respect_a_minimum_gap_and_failures_do_not_leak_stderr(tmp_path):
    store = configured(tmp_path)
    decision(store)
    claim = notices.claim(tmp_path, now=1000)
    notices.commit(tmp_path, claim["token"], now=1000)
    store.health(error="secret/provider/credential: invalid")
    assert notices.claim(tmp_path, now=1014) is None
    claim = notices.claim(tmp_path, now=1015)
    assert "secret" not in claim["text"]
    assert "worker status" in claim["text"]


def test_monitor_and_wake_cannot_claim_the_same_notice(tmp_path):
    store = configured(tmp_path)
    decision(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: notices.claim(tmp_path, now=1000), range(8)))
    assert sum(claim is not None for claim in claims) == 1


def test_dead_deliverer_lease_expires_and_cannot_release_its_successor(tmp_path):
    store = configured(tmp_path)
    decision(store)
    old = notices.claim(tmp_path, now=1000)
    assert notices.claim(tmp_path, now=1119) is None
    new = notices.claim(tmp_path, now=1120)
    notices.release(tmp_path, old["token"])
    assert not notices.commit(tmp_path, old["token"], now=1121)
    assert notices.claim(tmp_path, now=1121) is None
    assert notices.commit(tmp_path, new["token"], now=1121)


def test_failed_output_releases_a_claim_for_immediate_retry(tmp_path):
    store = configured(tmp_path)
    decision(store)
    claim = notices.claim(tmp_path, now=1000)
    notices.release(tmp_path, claim["token"])
    assert notices.claim(tmp_path, now=1001)


def test_answered_decisions_and_disabled_workers_stop_notices(tmp_path):
    store = configured(tmp_path)
    decision(store)
    store.answer(store.pending()[0]["id"], "Approved")
    assert notices.claim(tmp_path) is None
    store.health(error="provider failed")
    store.off()
    assert notices.claim(tmp_path) is None


def test_compact_monitor_delivers_only_worker_decisions_and_leaves_inbox_unread(profile, monkeypatch):
    store = configured(profile.dir)
    inbox = Inbox(profile.dir)
    printed = []
    alive = threading.Event()
    alive.set()
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr(cli, "is_running", lambda p: 1 if alive.is_set() else None)
    monkeypatch.setattr(cli, "print", lambda *args, **kwargs: printed.append(str(args[0])), raising=False)
    args = argparse.Namespace(follow=True, delivery="notice", replay=0, json=False,
        room=None, mine_too=False, exit_when_idle=True, limit=50)
    thread = threading.Thread(target=cli.cmd_listen, args=(args,), daemon=True)
    thread.start()
    try:
        time.sleep(0.3)
        inbox.record(Envelope(kind="chat", sender="peer", text="private raw message", seq=1))
        time.sleep(1.2)
        assert printed == []
        decision(store)
        deadline = time.monotonic() + 3
        while not printed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(printed) == 1
        assert "worker pending" in printed[0]
        assert "private" not in printed[0]
        assert inbox.unread_count() == 1
    finally:
        alive.clear()
        thread.join(timeout=4)
        inbox.close()
    assert not thread.is_alive()


def test_check_distinguishes_worker_consumption_from_missing_decision_route(profile, monkeypatch):
    store = configured(profile.dir)
    (profile.dir / "status.json").write_text(json.dumps({"state": "live", "heartbeat": time.time(), "unread_messages": 20}))
    monkeypatch.setattr(cli, "is_running", lambda p: 123)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    rows = {r["check"]: r for r in cli._checks(profile)}
    assert rows["watching"]["verdict"] == "fail"
    assert "no route" in rows["watching"]["detail"]
    assert rows["acting"]["verdict"] == "ok"
    wake.write_config(profile.dir, wake.WakeConfig(command=["true"]))
    rows = {r["check"]: r for r in cli._checks(profile)}
    assert rows["watching"]["verdict"] == "ok"
    decision(store)
    rows = {r["check"]: r for r in cli._checks(profile)}
    assert rows["acting"]["verdict"] == "warn"
    assert "worker pending" in rows["acting"]["fix"]


def test_explicit_full_monitor_still_delivers_messages_with_a_worker(profile, monkeypatch):
    configured(profile.dir)
    inbox = Inbox(profile.dir)
    inbox.record(Envelope(kind="chat", sender="peer", text="explicitly requested transcript", seq=1))
    printed = []
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    monkeypatch.setattr(cli, "print", lambda *args, **kwargs: printed.append(str(args[0])), raising=False)
    args = argparse.Namespace(follow=True, delivery="full", replay=50, json=False,
        room=None, mine_too=False, exit_when_idle=True, limit=50)
    try:
        assert cli.cmd_listen(args) == 0
        assert "explicitly requested transcript" in printed[0]
        assert inbox.unread_count() == 0
    finally:
        inbox.close()


def test_status_names_the_worker_and_does_not_claim_nobody_reads(profile, monkeypatch, capsys):
    configured(profile.dir)
    wake.write_config(profile.dir, wake.WakeConfig(command=["true"]))
    (profile.dir / "status.json").write_text(json.dumps({"state": "live", "heartbeat": time.time()}))
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda cls: profile))
    monkeypatch.setattr(cli, "is_running", lambda p: 123)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    monkeypatch.setattr(cli.reboot, "swept", lambda: None)
    assert cli.cmd_status(argparse.Namespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["worker"]["enabled"]
    assert status["conversation_consumer"] == "worker"
    assert "worker handles the conversation" in status["hint"]
