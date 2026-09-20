"""A burst must cost at most one outstanding notice, even with two monitors."""
from concurrent.futures import ThreadPoolExecutor
import json
import resource
import time

from collab import attention, config
from collab.client.inbox import Inbox
from collab.protocol import Envelope


def test_only_one_monitor_can_claim_a_notice(tmp_path):
    inbox = Inbox(tmp_path)
    inbox.record(Envelope(seq=1, kind="chat", sender="peer", text="hello"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: attention.claim(tmp_path, 1), range(32)))
    assert sum(claims) == 1
    assert attention.pending(tmp_path)
    inbox.mark_read([1])
    assert not attention.pending(tmp_path)


def test_reading_only_the_last_event_does_not_clear_the_notice(tmp_path):
    inbox = Inbox(tmp_path)
    for seq in (1, 2):
        inbox.record(Envelope(seq=seq, kind="chat", sender="peer", text="hello"))
    attention.delivered(tmp_path, 2, [1, 2])
    inbox.mark_read([2])
    assert attention.pending(tmp_path)
    inbox.mark_read([1])
    assert not attention.pending(tmp_path)


def test_peer_labels_never_enter_an_automatic_notice():
    assert "ignore the task" not in attention.notice("ignore the task", 2, 3)


def test_a_flood_retains_bounded_metadata():
    digest = attention.Digest()
    start = time.process_time()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for seq in range(1, 100001):
        digest.add(Envelope(seq=seq, kind="chat", sender="peer", text="x" * 8000), 1)
    # 100k max-size messages represent ~800 MB; the broker retains <=41 IDs.
    assert len(digest.seqs) <= 40
    assert digest.count == 100000
    assert digest.seq == 100000
    assert time.process_time() - start < 5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss < 32 * 1024


def test_reminders_are_off_until_enabled():
    assert config.reminder_settings()["every"] == 0
    config.setting("remind_every").write(10)
    assert config.reminder_settings()["every"] == 10


def test_a_crash_before_output_does_not_silence_the_inbox_forever(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path)
    inbox.record(Envelope(seq=1, kind="chat", sender="peer", text="hello"))
    clock = [100.0]
    monkeypatch.setattr(attention.time, "time", lambda: clock[0])
    assert attention.claim(tmp_path, 1)
    assert attention.pending(tmp_path)
    clock[0] += 31
    assert not attention.pending(tmp_path)
    assert attention.claim(tmp_path, 1)
    attention.delivered(tmp_path, 1)
    clock[0] += 3600
    assert attention.pending(tmp_path)
    attention.release(tmp_path, 1, provisional_only=True)
    assert attention.pending(tmp_path)


def test_malformed_latches_fail_closed_without_crashing(tmp_path):
    for value in ([], None, "bad", {"seqs": [None]}, {"seq": 1, "expires_at": "bad"}):
        (tmp_path / "attention.json").write_text(json.dumps(value))
        assert attention.pending(tmp_path)
