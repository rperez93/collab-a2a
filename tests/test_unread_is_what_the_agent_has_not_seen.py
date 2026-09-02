"""The envelope on the status line counts what the agent has not been shown.

`✉ N` is `unread_messages` out of `status.json`, and the daemon computes it as
the chat rows in the inbox still marked unread. Only one thing ever marked a
row read: `collab recv`. An agent whose monitor is `collab listen --follow` —
the arrangement every skill here prescribes — had every message it was shown
counted against it for ever, so the badge said «✉ 9» over a conversation it
had read line by line. The number was not wrong about the database; it was
wrong about what «unread» means to the reader.

Read means DELIVERED: printed by the monitor the agent is watching, or drained
by `collab recv`. Both go through one method, and the badge follows it.

The other thing the count did by name it now does by id: a participant's own
words are told apart by `fromId`, so a rename does not turn an agent's history
into other people's unread mail.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest

from collab import cli
from collab.client import daemon as d
from collab.client.inbox import Inbox
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, KIND_PRESENCE, Envelope

CHAT = (KIND_CHAT,)


def _chat(seq, sender, text="hi", *, sender_id="", to=None):
    return Envelope(kind=KIND_CHAT, text=text, sender=sender, sender_id=sender_id,
                    seq=seq, to=to, room=None if to else "general")


def _wait(pred, *, timeout=15.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --- one operation marks read --------------------------------------------------------

def test_marking_read_is_one_operation(tmp_path):
    inbox = Inbox(tmp_path)
    try:
        for seq in (1, 2, 3):
            inbox.record(_chat(seq, "alice"))
        assert inbox.unread_count(kinds=CHAT) == 3
        assert inbox.mark_read([1, 3]) == 2
        assert inbox.unread_count(kinds=CHAT) == 1
        assert inbox.mark_read([1, 3]) == 0, "already read rows are not counted twice"
        assert inbox.mark_read([99]) == 0, "a seq we never had is not an error"
    finally:
        inbox.close()


# --- the monitor's lines are delivered ---------------------------------------------

def _listen(monkeypatch, profile, *, replay=0, room=None):
    """Run `collab listen --follow` in a thread, collecting what it prints.

    `--exit-when-idle` ends the loop once the daemon is gone; `is_running` is
    stubbed so the test decides when that is.
    """
    printed: list[str] = []
    alive = threading.Event()
    alive.set()
    monkeypatch.setattr(cli, "is_running", lambda _p: 4242 if alive.is_set() else None)
    monkeypatch.setattr(cli, "print", lambda *a, **k: printed.append(" ".join(map(str, a))),
                        raising=False)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(session=None, follow=True, json=False, room=room,
                              limit=50, replay=replay, mine_too=False,
                              exit_when_idle=True)
    thread = threading.Thread(target=cli.cmd_listen, args=(args,), daemon=True)
    thread.start()
    return printed, alive, thread


def test_a_line_the_monitor_printed_is_read(profile, monkeypatch):
    inbox = Inbox(profile.dir)
    try:
        printed, alive, thread = _listen(monkeypatch, profile)
        time.sleep(0.6)                     # the tail has to be past the end first
        inbox.record(_chat(1, "alice", "one"))
        inbox.record(_chat(2, "alice", "two"))
        _wait(lambda: len(printed) == 2, what="two lines on the monitor")
        _wait(lambda: inbox.unread_count(kinds=CHAT) == 0,
              timeout=5, what="the printed lines to be marked read")
        alive.clear()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        inbox.close()


def test_a_line_the_monitor_did_not_print_stays_unread(profile, monkeypatch):
    """`--room` narrows what the monitor shows; what it does not show it has
    not delivered, and that message is still waiting for somebody."""
    inbox = Inbox(profile.dir)
    try:
        printed, alive, thread = _listen(monkeypatch, profile, room="general")
        time.sleep(0.6)
        inbox.record(Envelope(kind=KIND_CHAT, text="elsewhere", sender="alice",
                              seq=1, room="other"))
        inbox.record(_chat(2, "alice", "here"))
        _wait(lambda: len(printed) == 1, what="only the general line")
        _wait(lambda: inbox.unread_count(kinds=CHAT) == 1, timeout=5,
              what="the other room's line to stay unread")
        time.sleep(0.5)
        assert inbox.unread_count(kinds=CHAT) == 1
        alive.clear()
        thread.join(timeout=5)
    finally:
        inbox.close()


def test_a_replayed_line_is_read_too(profile, monkeypatch):
    """`--replay N` puts history in front of the agent on purpose; a message
    that was put in front of it is not waiting for it."""
    inbox = Inbox(profile.dir)
    try:
        inbox.record(_chat(1, "alice", "earlier"))
        assert inbox.unread_count(kinds=CHAT) == 1
        printed, alive, thread = _listen(monkeypatch, profile, replay=5)
        _wait(lambda: len(printed) == 1, what="the replayed line")
        alive.clear()
        thread.join(timeout=5)
        assert inbox.unread_count(kinds=CHAT) == 0
    finally:
        inbox.close()


def test_a_plain_listing_leaves_the_count_alone(profile, monkeypatch):
    """`collab listen` without --follow is a look at the transcript, like
    `collab watch`; it does not stand in for the agent reading."""
    inbox = Inbox(profile.dir)
    try:
        inbox.record(_chat(1, "alice", "earlier"))
        monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
        monkeypatch.setattr(cli, "print", lambda *a, **k: None, raising=False)
        cli.cmd_listen(argparse.Namespace(session=None, follow=False, json=False,
                                          room=None, limit=50, replay=0,
                                          mine_too=False, exit_when_idle=False))
        assert inbox.unread_count(kinds=CHAT) == 1
    finally:
        inbox.close()


# --- own words are told apart by id ------------------------------------------------

def test_own_messages_are_told_apart_by_id_not_name(tmp_path):
    inbox = Inbox(tmp_path)
    try:
        inbox.record(_chat(1, "bob", "mine, before the rename", sender_id="p_bob"))
        inbox.record(_chat(2, "bob", "a different bob", sender_id="p_other"))
        inbox.record(_chat(3, "alice", "theirs", sender_id="p_alice"))
        # Renamed to robert since; by name, seq 1 is now somebody else's.
        assert inbox.unread_count(exclude_sender="robert", kinds=CHAT) == 3
        assert inbox.unread_count(exclude_sender="robert", exclude_sender_id="p_bob",
                                  kinds=CHAT) == 2
        # And a same-named stranger is not us.
        assert inbox.unread_count(exclude_sender="bob", exclude_sender_id="p_bob",
                                  kinds=CHAT) == 2
    finally:
        inbox.close()


def test_rows_without_an_id_fall_back_to_the_name(tmp_path):
    """An older hub stamped no `fromId`; those rows are judged as before."""
    inbox = Inbox(tmp_path)
    try:
        inbox.record(_chat(1, "bob", "unstamped, mine"))
        inbox.record(_chat(2, "alice", "unstamped, theirs"))
        assert inbox.unread_count(exclude_sender="bob", exclude_sender_id="p_bob",
                                  kinds=CHAT) == 1
    finally:
        inbox.close()


def test_an_inbox_from_an_older_collab_gains_the_column(tmp_path):
    db = sqlite3.connect(tmp_path / "inbox.db")
    db.executescript("""
        CREATE TABLE inbox (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            kind TEXT NOT NULL, sender TEXT NOT NULL, payload TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO inbox VALUES (1, 't', 'chat', 'bob', '{"kind":"chat","from":"bob","seq":1}', 0);
    """)
    db.commit(); db.close()
    inbox = Inbox(tmp_path)
    try:
        assert inbox.record(_chat(2, "alice", sender_id="p_alice"))
        assert inbox.unread_count(exclude_sender="bob", exclude_sender_id="p_bob",
                                  kinds=CHAT) == 1
    finally:
        inbox.close()


def test_the_daemon_counts_by_id_after_a_rename(profile):
    profile.participant_id = "p_edith"
    profile.name = "edie"                    # renamed since the rows were written
    daemon = d.Daemon(profile)
    try:
        daemon.inbox.record(_chat(1, "edith", "my own, under my old name", sender_id="p_edith"))
        daemon.inbox.record(_chat(2, "alice", "theirs", sender_id="p_alice"))
        daemon.write_status()
        status = json.loads((profile.dir / "status.json").read_text())
        assert status["unread_messages"] == 1
        assert status["unread"] == 1
    finally:
        daemon.inbox.close()


# --- end to end: a real hub, a real daemon, a real inbox ------------------------------

def _join(base, session, name):
    r = httpx.post(f"{base}/ext/collab/v1/join",
                   json={"invite": session["invite"], "name": name, "hello": {}},
                   timeout=10)
    r.raise_for_status()
    return r.json()


def _say(base, headers, text, **extra):
    r = httpx.post(f"{base}/ext/collab/v1/messages", json={"text": text, **extra},
                   headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()["seq"]


@pytest.fixture()
def streaming_guest(live_server, session, tmp_path, monkeypatch):
    """bob's real daemon, streaming the real feed into a real inbox."""
    base = live_server["base"]
    joined = _join(base, session, "bob")
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s_test").mkdir(parents=True)
    profile = SessionProfile(session_id="s_test", url=base, name="bob", host_name="alice",
                             token=joined["token"], home=str(home),
                             participant_id=joined["id"])
    profile.save(make_current=False)
    daemon = d.Daemon(profile)

    loop = asyncio.new_event_loop()
    holder: dict = {}

    async def go():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client:
            holder["task"] = asyncio.current_task()
            try:
                await daemon._stream_once(client)
            except (asyncio.CancelledError, RuntimeError):
                pass

    thread = threading.Thread(target=lambda: loop.run_until_complete(go()), daemon=True)
    thread.start()
    _wait(lambda: (profile.dir / "status.json").exists(), what="the daemon's first status")
    yield {"daemon": daemon, "profile": profile, "base": base,
           "bob": {"Authorization": f"Bearer {joined['token']}"}}
    if "task" in holder:
        loop.call_soon_threadsafe(holder["task"].cancel)
    thread.join(timeout=10)
    daemon.inbox.close()
    loop.close()


def _status(profile):
    return json.loads((profile.dir / "status.json").read_text())


def test_the_badge_tracks_what_bob_has_not_seen(streaming_guest, live_server, session,
                                                 host_headers, monkeypatch):
    daemon, profile, base = (streaming_guest[k] for k in ("daemon", "profile", "base"))
    bob = streaming_guest["bob"]
    inbox = daemon.inbox

    def landed(seq):
        _wait(lambda: inbox.last_seq() >= seq, what=f"seq {seq} in bob's inbox")
        _wait(lambda: _status(profile)["last_seq"] >= seq, what=f"status.json at seq {seq}")
        return _status(profile)

    # Three from the host: three unread.
    for n, text in enumerate(("one", "two", "three"), start=1):
        status = landed(_say(base, host_headers, text))
        assert status["unread_messages"] == n, (text, status["unread_messages"])

    # Bob's own words come back down the feed and are not unread.
    status = landed(_say(base, bob, "me too"))
    assert status["unread_messages"] == 3

    # A direct message to bob is.
    status = landed(_say(base, host_headers, "just you", to="bob"))
    assert status["unread_messages"] == 4

    # Carol arrives: an event, not a message. `unread` moves, the badge does not.
    carol = _join(base, session, "carol")
    carol_h = {"Authorization": f"Bearer {carol['token']}"}
    status = landed(_say(base, host_headers, "welcome"))
    assert status["unread_messages"] == 5
    assert status["unread"] > status["unread_messages"], "the presence event counts only in `unread`"

    # A DM between two others never reaches bob's inbox at all.
    _say(base, host_headers, "not for bob", to="carol")
    status = landed(_say(base, carol_h, "hi all"))
    assert status["unread_messages"] == 6
    assert inbox.unread_count(kinds=CHAT, exclude_sender="bob",
                              exclude_sender_id=profile.participant_id) == 6

    # `collab recv` drains, and the badge follows on the next write.
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    monkeypatch.setattr(cli, "print", lambda *a, **k: None, raising=False)
    cli.cmd_recv(argparse.Namespace(session=None, wait=0.0, limit=100, json=False,
                                    peek=False, mine_too=False))
    daemon.write_status()
    assert _status(profile)["unread_messages"] == 0

    # `collab listen --follow` is bob's monitor. What it prints, bob has seen.
    printed: list[str] = []
    alive = threading.Event(); alive.set()
    monkeypatch.setattr(cli, "is_running", lambda _p: 4242 if alive.is_set() else None)
    monkeypatch.setattr(cli, "print", lambda *a, **k: printed.append(" ".join(map(str, a))),
                        raising=False)
    args = argparse.Namespace(session=None, follow=True, json=False, room=None, limit=50,
                              replay=0, mine_too=False, exit_when_idle=True)
    monitor = threading.Thread(target=cli.cmd_listen, args=(args,), daemon=True)
    monitor.start()
    time.sleep(0.6)
    landed(_say(base, host_headers, "four"))
    landed(_say(base, host_headers, "five"))
    _wait(lambda: len(printed) == 2, what="the monitor to print both")
    _wait(lambda: inbox.unread_count(kinds=CHAT) == 0, timeout=5,
          what="the monitor's lines to be read")
    daemon.write_status()
    assert _status(profile)["unread_messages"] == 0, printed
    alive.clear()
    monitor.join(timeout=5)
