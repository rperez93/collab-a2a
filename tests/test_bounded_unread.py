"""A bounded unread inbox for agents that never mark rows read (a notice-mode
monitor, a worker-driven agent), and a read that returns what the notice
named instead of the oldest rows. Every name and text here is invented."""
import json

import pytest

from collab import attention
from collab.client import inbox as inbox_module
from collab.client.inbox import Inbox, UNREAD_KEEP
from collab.protocol import Envelope

ME, MY_NAME = "p_me", "cortex"


def _room(seq, text="room chatter", sender="p_other"):
    return Envelope(kind="chat", sender="other", sender_id=sender, room="general", text=text, seq=seq)


def _dm(seq):
    return Envelope(kind="chat", sender="other", sender_id="p_other", to=MY_NAME, to_id=ME,
                    room=None, text="are you there?", seq=seq)


def _outcome(seq):
    return Envelope(kind="system", sender="collab", to=MY_NAME, to_id=ME, room=None, text="answered",
                    body={"event": "remote-outcome", "remote_outcome": {"state": "answered"}}, seq=seq)


def _fill(box, start, count, make=_room):
    for seq in range(start, start + count):
        box.record(make(seq))


def _unread(box):
    with box._lock:
        return [r[0] for r in box._db.execute("SELECT seq FROM inbox WHERE read=0 ORDER BY seq")]


def test_recv_notice_returns_the_notified_batch_not_the_oldest_rows(tmp_path):
    box = Inbox(tmp_path)
    _fill(box, 1, 3000)
    _fill(box, 3001, 2, make=lambda s: _room(s, f"news {s}"))
    shown = box.notice_batch([3001, 3002], limit=100)
    assert [e.seq for e in shown] == [3001, 3002]
    assert 1 in _unread(box) and 3001 not in _unread(box)          # nothing older touched
    # Without a recorded notice: the newest batch, never the oldest.
    assert [e.seq for e in box.notice_batch([], limit=3)] == [2998, 2999, 3000]


def test_aging_keeps_the_backlog_bounded_but_only_below_what_a_notice_covered(tmp_path):
    box = Inbox(tmp_path)
    _fill(box, 1, 3000)
    assert box.age(notice_seq=0, self_id=ME, self_name=MY_NAME) == 0      # nothing covered yet
    aged = box.age(notice_seq=3000, self_id=ME, self_name=MY_NAME)
    assert aged == 3000 - UNREAD_KEEP and len(_unread(box)) == UNREAD_KEEP
    assert box.aged_count() == aged
    # Rows past the last notice are not aged even when the backlog is large.
    _fill(box, 3001, 600)
    box.age(notice_seq=3000, self_id=ME, self_name=MY_NAME)
    assert all(seq in _unread(box) for seq in range(3001, 3601))


def test_a_dm_is_aged_only_once_the_worker_handled_it(tmp_path):
    box = Inbox(tmp_path)
    box.record(_dm(1))
    box.record(_room(2, f"{MY_NAME}, can you take this?"))           # a question naming me
    box.record(_outcome(3))
    _fill(box, 4, 1000)
    box.age(notice_seq=1003, self_id=ME, self_name=MY_NAME)
    assert {1, 2, 3} <= set(_unread(box))                             # notice-mode, no worker: kept
    box.age(notice_seq=1003, worker_cursor=1003, self_id=ME, self_name=MY_NAME)
    assert 1 not in _unread(box) and 2 not in _unread(box)           # the worker handled them
    assert 3 in _unread(box)                                          # an outcome never ages unread


def test_the_notices_name_the_bounded_read():
    assert "`collab recv --notice`" in attention.notice("s", 2, 10)
    assert "collab recv`" not in attention.notice("s", 2, 10).replace("collab recv --notice`", "")


def test_plain_recv_says_when_more_is_waiting_and_notice_reads_the_batch(tmp_path, monkeypatch, capsys):
    from collab import cli
    from types import SimpleNamespace
    from collab.client.daemon_files import DaemonPaths
    profile = SimpleNamespace(dir=tmp_path, name=MY_NAME, participant_id=ME, session_id="s")
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr(cli, "polled", lambda profile: None)
    box = Inbox(tmp_path)
    _fill(box, 1, 150)
    root = DaemonPaths(tmp_path).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "attention.json").write_text(json.dumps({"seq": 150, "seqs": [149, 150], "expires_at": 0}))
    assert cli.main(["recv", "--notice", "--json"]) == 0
    assert [json.loads(l)["seq"] for l in capsys.readouterr().out.splitlines()] == [149, 150]
    assert cli.main(["recv", "--peek", "--json", "--limit", "10"]) == 0
    out = capsys.readouterr()
    # The hint goes to stderr: --json stays one event per line.
    assert all(json.loads(l).get("seq") for l in out.out.splitlines())
    assert "more unread than this page" in out.err


def test_a_notice_over_41_events_reads_every_row_it_covered(tmp_path):
    """The gate's repro: a notice keeps 40 seqs plus the newest, so a burst of
    100 named 41; reading only the named rows skipped 59 for good."""
    box = Inbox(tmp_path)
    digest = attention.Digest()
    for seq in range(1, 101):
        env = _room(seq, f"news {seq}")
        box.record(env)
        digest.add(env, 0.0)
    shown = box.notice_batch([*digest.seqs, digest.seq], limit=200)
    assert [e.seq for e in shown] == list(range(1, 101))
    assert _unread(box) == []


def test_a_repaired_or_escalated_row_behind_the_cursor_is_never_aged(tmp_path):
    """The gate's repro: a DM recovered with --repair lands behind the worker
    cursor; the cursor is not proof the worker handled it."""
    box = Inbox(tmp_path)
    _fill(box, 2, 1198)
    box.record(_dm(1), repaired=True)
    box.age(notice_seq=1199, worker_cursor=1199, self_id=ME, self_name=MY_NAME)
    assert 1 in _unread(box)                                    # awaiting repair: kept
    with box._lock:
        box._db.execute("DELETE FROM repair_pending")
        box._db.commit()
    box.age(notice_seq=1199, worker_cursor=1199, protected=(1,), self_id=ME, self_name=MY_NAME)
    assert 1 in _unread(box)                                    # the worker escalated it: kept
    box.age(notice_seq=1199, worker_cursor=1199, self_id=ME, self_name=MY_NAME)
    assert 1 not in _unread(box)                                # handled: aged


def test_the_worker_names_the_rows_it_did_not_handle(tmp_path):
    from collab import worker
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate", "enabled": True})
    with store._db() as db:
        db.execute("INSERT INTO repaired VALUES (7)")
    assert 7 in store.protected_seqs()



def _thread(seq, sender, thread):
    return Envelope(kind="chat", sender="x", sender_id=sender, room="general", text="re", thread=thread, seq=seq)


def test_replies_in_my_thread_file_dms_and_skill_approvals_are_never_aged_by_the_cursor(tmp_path):
    """The gate's item 4: the worker skips files and system events, so its
    cursor passing them proves nothing; replies in a thread I started are mine."""
    box = Inbox(tmp_path)
    box.record(_thread(1, ME, "t_mine"))                         # I started the thread
    box.record(_thread(2, "p_other", "t_mine"))                  # a reply to me
    box.record(Envelope(kind="file", sender="other", sender_id="p_other", to=MY_NAME, to_id=ME,
                        room=None, text="", body={"name": "a.zip"}, seq=3))
    box.record(Envelope(kind="system", sender="other", sender_id="p_other", to=MY_NAME, to_id=ME,
                        room=None, text="accepted", body={"event": "skill-approval"}, seq=4))
    box.record(_thread(5, "p_other", "t_theirs"))                # someone else's thread
    _fill(box, 6, 1000)
    box.age(notice_seq=1005, worker_cursor=1005, self_id=ME, self_name=MY_NAME)
    left = set(_unread(box))
    assert {2, 3, 4} <= left and 5 not in left


def test_notice_batch_guards_hold(tmp_path):
    """Pins the guards whose mutants survived: read=0, the relevant-kind
    filter, own rows unless asked, and --peek."""
    box = Inbox(tmp_path)
    box.record(_room(1, "first"))
    box.record(Envelope(kind="presence", sender="o", sender_id="p_other", room="general", text="", seq=2))
    box.record(_room(3, "mine", sender=ME))
    box.record(_room(4, "later"))
    assert [e.seq for e in box.notice_batch([1], mark=False, exclude_sender_id=ME)] == [1, 4]
    assert 1 in _unread(box)                                     # --peek marked nothing
    assert [e.seq for e in box.notice_batch([1], exclude_sender_id=ME)] == [1, 4]
    assert box.notice_batch([1], exclude_sender_id=ME) == []      # read rows are not returned again
    assert [e.seq for e in box.notice_batch([3])] == [3]          # --mine-too includes own


def test_a_directed_room_chat_is_not_freely_aged(tmp_path):
    """Pins the $.to check: a chat with a recipient name but no id is addressed."""
    box = Inbox(tmp_path)
    box.record(Envelope(kind="chat", sender="o", sender_id="p_other", to=MY_NAME, room=None, text="hi", seq=1))
    _fill(box, 2, 1000)
    box.age(notice_seq=1001, self_id=ME, self_name=MY_NAME)
    assert 1 in _unread(box)


def test_the_daemon_ages_only_after_a_committed_notice_or_with_the_worker_on(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    from collab import worker_notices
    box = Inbox(tmp_path / "inbox")
    _fill(box, 1, 1000)
    root = tmp_path / "daemon"
    root.mkdir()
    fake = SimpleNamespace(paths=SimpleNamespace(root=root), inbox=box,
                           profile=SimpleNamespace(dir=tmp_path, participant_id=ME, name=MY_NAME))
    monkeypatch.setattr(worker_notices, "enabled", lambda d: False)
    (root / "attention.json").write_text(json.dumps({"seq": 1000, "seqs": [1000], "expires_at": 9e9}))
    Daemon._age_unread(fake)
    assert len(_unread(box)) == 1000                              # a lease, not a delivered notice
    fake._aged_at = 0.0
    (root / "attention.json").write_text(json.dumps({"seq": 1000, "seqs": [1000], "expires_at": 0}))
    Daemon._age_unread(fake)
    assert len(_unread(box)) == UNREAD_KEEP


def test_a_capped_wake_names_the_oldest_first_read_for_its_earlier_rows():
    import inspect
    from collab import wake
    src = inspect.getsource(wake)
    assert "`collab recv`, oldest first, has them" in src
    assert "`collab recv --notice` has the latest" not in src



def test_a_reply_in_any_thread_i_posted_in_is_never_aged(tmp_path):
    """The gate: a peer may post first under a thread id; once I replied in
    it, later replies are mine to read."""
    box = Inbox(tmp_path)
    box.record(_thread(1, "p_other", "t_shared"))                 # a peer starts it
    box.record(_thread(2, ME, "t_shared"))                        # I reply
    box.record(_thread(3, "p_other", "t_shared"))                 # a later peer reply
    _fill(box, 4, 1000)
    box.age(notice_seq=1003, worker_cursor=1003, self_id=ME, self_name=MY_NAME)
    assert 3 in _unread(box)


def test_an_escalated_row_behind_the_cursor_survives_aging_end_to_end(tmp_path, monkeypatch):
    """The two surviving mutants: Store.protected_seqs must include the open
    escalations' seqs, and Daemon._age_unread must pass them to Inbox.age."""
    from types import SimpleNamespace
    from collab import worker, worker_notices
    from collab.client.daemon import Daemon
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate", "enabled": True})
    with store._db() as db:
        state = store._state(db)
        state["cursor"] = 1001
        store._save(db, state)
        store._add(db, "escalation", {"reason": "blocker", "seq": 1,
                                      "question": "an oversized DM could not be read"})
    assert 1 in store.protected_seqs()
    box = Inbox(tmp_path / "inbox")
    box.record(_dm(1))
    _fill(box, 2, 1000)
    root = tmp_path / "daemon"
    root.mkdir()
    monkeypatch.setattr(worker_notices, "enabled", lambda d: True)
    fake = SimpleNamespace(paths=SimpleNamespace(root=root), inbox=box,
                           profile=SimpleNamespace(dir=tmp_path, participant_id=ME, name=MY_NAME))
    Daemon._age_unread(fake)
    assert 1 in _unread(box) and len(_unread(box)) < 1001     # aged around it, not it
