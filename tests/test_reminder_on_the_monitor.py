"""The standing reminder, delivered down the monitor every agent already has.

v1.29.0 put the reminder on the wake, and `Waker.due` refuses at its first line
when no wake command is configured. So the agent this project tells to arm no
wake at all — «Claude Code needs none of this; it holds its own monitor» — was
the one agent the reminder could never reach. It worked for Codex, for Gemini
and for anything driven through a tmux pane, and did nothing, silently, for the
agent most likely to be in the session.

A followed stream (`collab listen --follow`) is the monitor every agent has, and
since v1.28.0 it already marks what it prints as read: «the agent was shown
this» is a thing that stream can say. So the reminder goes down it.

ONE CLOCK. The daemon decides «is it due», exactly as it did for the wake, and
leaves the reminder in a drop file the follower picks up. The follower never
decides anything: two clocks would drift, and an agent holding both a monitor
and an armed wake would be reminded twice. The daemon offers it to the monitor
first, because that path costs nothing and the wake costs a whole turn.

It is still not a message. It never enters the inbox, never counts as unread,
never reaches the hub, never appears in anybody else's transcript and never
appears in `collab watch` — that pane is the human's window, and this is for
the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time

import pytest

from collab import cli, config, wake
from collab.client import daemon as d
from collab.client.inbox import Inbox
from collab.protocol import ALL_KINDS, KIND_CHAT, Envelope

MINUTE = 60.0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A throwaway global config. Never the machine's own."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("COLLAB_CONFIG", str(path))
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


def write_config(path, **values):
    path.write_text(json.dumps(values), encoding="utf-8")
    config._CACHE.clear()


# --- a daemon with a clock we hold ---------------------------------------------

class _Bridge:
    """Records anything the daemon would have put on the wire for `watch`."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.clients = 0

    async def broadcast(self, env) -> None:
        self.sent.append(env)


@pytest.fixture()
def monitors(monkeypatch):
    """How many followed streams the daemon believes are armed."""
    count = [0]
    monkeypatch.setattr(d, "watchers", lambda _p: list(range(count[0])))
    return count


def a_daemon(profile, *, clock, is_host=None):
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = profile
    daemon.paths = d.DaemonPaths(profile.dir)
    daemon.waker = wake.Waker(
        daemon.paths.root, profile.session_id, attended=lambda: False,
        now=lambda: clock[0],
        is_host=profile.is_host if is_host is None else is_host)
    daemon._waking = None
    daemon._waking_batch = None
    daemon._wake_note = ""
    daemon._wake_activity = None
    daemon._http = None
    daemon._notifying = set()
    daemon.bridge = _Bridge()
    return daemon


def arm(daemon, landed=None):
    """Arm a wake that records every prompt it is handed."""
    command = ([sys.executable, "-c",
                f"import sys; open({str(landed)!r}, 'a').write(sys.stdin.read())"]
               if landed else [sys.executable, "-c", "import sys; sys.stdin.read()"])
    wake.write_config(daemon.paths.root,
                      wake.WakeConfig(command=command, settle=0, min_gap=0))


def beat(daemon, seen=None):
    """One heartbeat's worth of the two delivery paths, in the daemon's order.

    Anything newly left for a monitor is appended to `seen`, so a test can
    count reminders without the daemon keeping a tally for it.
    """
    before = wake.reminder_waiting(daemon.paths.root)

    async def run():
        daemon._remind_the_monitor()
        await daemon._maybe_wake()
        if daemon._waking is not None:
            await daemon._waking

    asyncio.run(run())
    after = wake.reminder_waiting(daemon.paths.root)
    if seen is not None and after is not None and (
            before is None or after["at"] != before["at"]):
        seen.append(after)
    return after


def woken_reminders(landed):
    """How many times the wake carried a reminder into an agent."""
    return landed.read_text().count("standing reminder") if landed.exists() else 0


# --- the daemon leaves it for the monitor --------------------------------------

def test_a_monitor_is_offered_the_reminder_with_no_wake_armed(profile, monitors):
    """The whole gap: no wake, and the agent is still reminded."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    monitors[0] = 1
    assert not wake.read_config(daemon.paths.root).enabled, "no wake, on purpose"

    beat(daemon)
    assert wake.reminder_waiting(daemon.paths.root) is None, \
        "fired inside its own first interval"
    clock[0] += 10 * MINUTE
    waiting = beat(daemon)
    assert waiting is not None, "the reminder never reached the monitor"
    assert "collab working" in waiting["text"] or "collab who" in waiting["text"]
    assert waiting["every"] == 10


def test_no_monitor_and_no_wake_leaves_nothing_behind(profile, monitors):
    """A reminder dropped where nothing is reading would be delivered late, to
    whatever monitor started next — which is not «every ten minutes»."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    beat(daemon)
    clock[0] += 60 * MINUTE
    beat(daemon)
    assert wake.reminder_waiting(daemon.paths.root) is None


def test_the_monitor_is_not_reminded_oftener_than_the_interval(profile, monitors):
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    monitors[0] = 1
    seen: list[dict] = []
    beat(daemon, seen)
    for _ in range(360):                       # thirty minutes of heartbeats
        clock[0] += 5
        beat(daemon, seen)
    assert len(seen) == 3, f"{len(seen)} reminders in thirty minutes"


def test_zero_delivers_none_by_either_path(profile, monitors, isolated):
    write_config(isolated, remind_every=0)
    landed = profile.dir / "landed.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    arm(daemon, landed)
    monitors[0] = 1
    seen: list[dict] = []
    for _ in range(120):                       # an hour of heartbeats
        clock[0] += 30
        beat(daemon, seen)
    assert seen == []
    assert woken_reminders(landed) == 0


def test_the_host_and_the_guest_are_told_different_things_on_the_monitor(profile,
                                                                        monitors):
    clock = [10_000.0]
    monitors[0] = 1
    host = a_daemon(profile, clock=clock, is_host=True)
    beat(host)                                 # starts the clock
    clock[0] += 10 * MINUTE
    said_host = beat(host)
    guest = a_daemon(profile, clock=clock, is_host=False)
    clock[0] += 10 * MINUTE
    said_guest = beat(guest)

    assert said_host is not None and said_guest is not None
    assert said_host["role"] == "host" and said_guest["role"] == "guest"
    assert said_host["text"] == config.DEFAULT_REMIND_HOST
    assert said_guest["text"] == config.DEFAULT_REMIND_GUEST
    assert said_host["text"] != said_guest["text"]


# --- one reminder per interval, whichever path carries it ----------------------

def test_a_monitor_and_an_armed_wake_give_one_reminder_between_them(profile,
                                                                    monitors):
    """Two routes to one agent is two copies of the same paragraph, ten minutes
    apart from each other, for ever. The clock is one clock."""
    landed = profile.dir / "landed.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    arm(daemon, landed)
    monitors[0] = 1
    seen: list[dict] = []
    beat(daemon, seen)
    for _ in range(360):                       # thirty minutes
        clock[0] += 5
        beat(daemon, seen)

    woken = woken_reminders(landed)
    assert len(seen) + woken == 3, \
        f"{len(seen)} on the monitor and {woken} on the wake"
    assert woken == 0, "the monitor is free and the wake costs a turn"


def test_the_wake_still_carries_it_when_nothing_is_monitoring(profile, monitors):
    """An agent with a wake armed and no monitor keeps what v1.29.0 gave it."""
    landed = profile.dir / "landed.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    arm(daemon, landed)
    seen: list[dict] = []
    beat(daemon, seen)
    clock[0] += 10 * MINUTE
    beat(daemon, seen)
    assert woken_reminders(landed) == 1
    assert seen == [], "nothing was monitoring and it was dropped anyway"


# --- it is not a message -------------------------------------------------------

def test_the_reminder_never_becomes_an_event(profile, monitors):
    """Not in the inbox, not unread, not on the bridge `collab watch` reads,
    not at the hub. Every one of those would put a paragraph nobody wrote into
    somebody else's window."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    monitors[0] = 1
    inbox = Inbox(profile.dir)
    try:
        beat(daemon)
        clock[0] += 10 * MINUTE
        assert beat(daemon) is not None

        assert daemon.bridge.sent == [], "a reminder reached `collab watch`"
        assert daemon._http is None, "a reminder reached the hub"
        assert inbox.all_events(limit=100) == [], "a reminder entered the inbox"
        assert inbox.unread_count(kinds=(KIND_CHAT,)) == 0
        assert not inbox.jsonl.exists() or "standing" not in inbox.jsonl.read_text()
    finally:
        inbox.close()


def test_the_drop_lives_beside_the_wake_and_not_among_the_events(profile, monitors):
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    monitors[0] = 1
    beat(daemon)
    clock[0] += 10 * MINUTE
    beat(daemon)
    path = daemon.waker.reminder_drop
    assert path.exists() and path.parent.name == "wake"


# --- what the followed stream does with it -------------------------------------

def _listen(monkeypatch, profile, *, as_json=False, room=None, follow=True):
    printed: list[str] = []
    alive = threading.Event()
    alive.set()
    monkeypatch.setattr(cli, "is_running", lambda _p: 4242 if alive.is_set() else None)
    monkeypatch.setattr(cli, "print",
                        lambda *a, **k: printed.append(" ".join(map(str, a))),
                        raising=False)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(session=None, follow=follow, json=as_json, room=room,
                              limit=50, replay=0, mine_too=False,
                              exit_when_idle=True)
    thread = threading.Thread(target=cli.cmd_listen, args=(args,), daemon=True)
    thread.start()
    return printed, alive, thread


def _wait(pred, *, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _drop(profile, *, at, text="mind the board", every=10, role="guest"):
    home = profile.dir / "wake"
    home.mkdir(parents=True, exist_ok=True)
    (home / wake.REMINDER_DROP).write_text(json.dumps(
        {"at": at, "every": every, "role": role, "text": text}), encoding="utf-8")


def test_a_followed_stream_prints_the_reminder(profile, monkeypatch):
    printed, alive, thread = _listen(monkeypatch, profile)
    try:
        time.sleep(0.6)
        _drop(profile, at=time.time(), text="mind the board")
        shown = _wait(lambda: [p for p in printed if "mind the board" in p],
                      what="the reminder on the monitor")
        line = shown[0]
        assert "reminder" in line
        assert "remind_every 0" in line, "and how to stop it"
        assert "every 10m" in line and "guest" in line
    finally:
        alive.clear()
        thread.join(timeout=5)
    inbox = Inbox(profile.dir)
    try:
        assert inbox.all_events(limit=100) == [], "the monitor recorded it as an event"
        assert inbox.unread_count(kinds=(KIND_CHAT,)) == 0
    finally:
        inbox.close()


def test_a_followed_stream_prints_each_reminder_once(profile, monkeypatch):
    printed, alive, thread = _listen(monkeypatch, profile)
    try:
        time.sleep(0.6)
        _drop(profile, at=1000.0, text="first")
        _wait(lambda: any("first" in p for p in printed), what="the first")
        time.sleep(1.5)
        assert len([p for p in printed if "first" in p]) == 1
        _drop(profile, at=2000.0, text="second")
        _wait(lambda: any("second" in p for p in printed), what="the second")
        assert len([p for p in printed if "first" in p]) == 1
    finally:
        alive.clear()
        thread.join(timeout=5)


def test_a_stale_drop_is_not_replayed_to_a_monitor_that_starts(profile, monkeypatch):
    """Otherwise restarting a monitor is a reminder, and a monitor that keeps
    dropping is a flood — the same reason the wake's clock is durable."""
    _drop(profile, at=1000.0, text="from before this monitor existed")
    printed, alive, thread = _listen(monkeypatch, profile)
    try:
        time.sleep(1.2)
        assert not [p for p in printed if "before this monitor" in p]
    finally:
        alive.clear()
        thread.join(timeout=5)


def test_a_plain_listing_carries_no_reminder(profile, monkeypatch):
    """`collab listen` without `--follow` is a look at the transcript, like
    `collab watch`. It marks nothing read and it is nobody's monitor."""
    inbox = Inbox(profile.dir)
    try:
        inbox.record(Envelope(kind=KIND_CHAT, text="hi", sender="jarvis", seq=1,
                              room="general"))
    finally:
        inbox.close()
    _drop(profile, at=time.time(), text="mind the board")
    printed, alive, thread = _listen(monkeypatch, profile, follow=False)
    thread.join(timeout=5)
    alive.clear()
    assert any("hi" in p for p in printed), "the listing itself did not run"
    assert not [p for p in printed if "mind the board" in p]


def test_a_room_filtered_monitor_still_carries_it(profile, monkeypatch):
    """`--room` filters MESSAGES. The reminder is not one, so no message filter
    reaches it — and a filtered followed stream is still a monitor."""
    printed, alive, thread = _listen(monkeypatch, profile, room="backend")
    try:
        time.sleep(0.6)
        _drop(profile, at=time.time(), text="mind the board")
        _wait(lambda: any("mind the board" in p for p in printed),
              what="the reminder on a filtered monitor")
    finally:
        alive.clear()
        thread.join(timeout=5)


def test_the_json_stream_carries_something_that_is_not_an_envelope(profile,
                                                                  monkeypatch):
    """A `--json` monitor is still a monitor, so it gets one. It must not be
    readable as a message: `Envelope.from_dict` defaults a missing kind to
    `chat`, so the line carries a kind of its own that no hub event has, and no
    `seq` to be mistaken for a position in the feed."""
    printed, alive, thread = _listen(monkeypatch, profile, as_json=True)
    try:
        time.sleep(0.6)
        _drop(profile, at=time.time(), text="mind the board")
        shown = _wait(lambda: [p for p in printed if "mind the board" in p],
                      what="the reminder on a json monitor")
        row = json.loads(shown[0])
        assert row["kind"] == "reminder" and row["kind"] not in ALL_KINDS
        assert row["local"] is True
        assert "seq" not in row and "from" not in row
        assert row["text"] == "mind the board" and row["every"] == 10
    finally:
        alive.clear()
        thread.join(timeout=5)


def test_a_junk_drop_never_takes_the_monitor_down(profile, monkeypatch):
    """It is read on a timer by the one process the agent is watching. A raise
    here is not an error message; it is a monitor that stopped."""
    home = profile.dir / "wake"
    home.mkdir(parents=True, exist_ok=True)
    path = home / wake.REMINDER_DROP
    printed, alive, thread = _listen(monkeypatch, profile)
    try:
        time.sleep(0.6)
        for raw in ('', 'not json', '[]', 'null', '{"at": "soon"}',
                    '{"at": NaN, "text": "x"}', '{"at": 1e400, "text": "x"}',
                    '{"at": 5000.0}', '{"at": 5000.0, "text": ""}',
                    '{"at": 5000.0, "text": 7}'):
            path.write_text(raw, encoding="utf-8")
            time.sleep(0.15)
        assert thread.is_alive(), "the monitor died on a file it read"
        assert not [p for p in printed if "reminder" in p]
        _drop(profile, at=9000.0, text="mind the board")
        _wait(lambda: any("mind the board" in p for p in printed),
              what="a good reminder after the junk")
    finally:
        alive.clear()
        thread.join(timeout=5)


def test_the_text_cannot_rewrite_the_readers_terminal(profile, monkeypatch):
    """It is somebody's own config, but it is still printed to a terminal, and
    a stray escape in a hand-edited file is a command rather than text."""
    printed, alive, thread = _listen(monkeypatch, profile)
    try:
        time.sleep(0.6)
        _drop(profile, at=time.time(), text="one\x1b[2Jtwo\rthree")
        shown = _wait(lambda: [p for p in printed if "two" in p], what="the reminder")
        assert "\x1b" not in shown[0] and "\r" not in shown[0]
    finally:
        alive.clear()
        thread.join(timeout=5)


# --- what `collab check` now says ----------------------------------------------

def _checks(profile, monkeypatch, *, monitors, armed):
    (profile.dir / "status.json").write_text(json.dumps({
        "state": "live", "heartbeat": time.time(), "unread_messages": 0,
        "wake": {"armed": armed}}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: list(range(monitors)))
    return {r["check"]: r for r in cli._checks(profile)}


def test_the_check_is_quiet_when_a_monitor_can_carry_it(profile, monkeypatch,
                                                        isolated):
    write_config(isolated, remind_every=10)
    assert "reminder" not in _checks(profile, monkeypatch, monitors=1, armed=False)


def test_the_check_warns_with_neither_a_monitor_nor_a_wake(profile, monkeypatch,
                                                           isolated):
    write_config(isolated, remind_every=10)
    said = _checks(profile, monkeypatch, monitors=0, armed=False)["reminder"]
    assert said["verdict"] == cli.CHECK_WARN
    assert "listen --follow" in said["fix"], "the route every agent has, first"
    assert "wake agents" in said["fix"]
    assert "remind_every 0" in said["fix"]


def test_the_check_stays_quiet_for_a_reminder_nobody_configured(profile, monkeypatch):
    """Unchanged: nothing configured is a decision, not a fault."""
    assert "reminder" not in _checks(profile, monkeypatch, monitors=0, armed=False)
