"""The standing reminder every agent's own daemon puts back in front of it.

An agent drifts. Twenty minutes into a session it has stopped saying what it is
doing, the host has stopped looping over the roster, and nothing anywhere is a
fault: the daemon is live, the feed is read, the board simply stopped moving.
The rules say to loop every ten to fifteen minutes and nothing was making that
happen.

So each daemon reminds ITS OWN agent, locally, on the wake path — never by
posting to the room, which would put N copies of the same paragraph in
everybody's transcript for a thing nobody said. Riding the wake buys the whole
discipline that path already has: it does not interrupt a turn in flight, it
respects the gap and the settle window, and it reaches whatever the recipe
knows how to reach.

Four claims are load-bearing and each is asked separately here, because a
single end-to-end test would pass with three of them broken:

* it fires about every `remind_every` minutes and not oftener;
* a message ALWAYS beats it — the reminder rides along or is skipped, and never
  delays or displaces one;
* host and guest are told different things, decided by `is_host`;
* it is a reminder and not a task: nothing reaches the hub, no task moves, no
  batch moves, no activity is published.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from collab import cli, config, wake
from collab.client import daemon as d
from collab.config import SessionProfile
from collab.protocol import Envelope


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


def waker(tmp_path, *, is_host=False, clock=None, armed=True, **cfg):
    clock = clock or [10_000.0]
    if armed:
        wake.write_config(tmp_path, wake.WakeConfig(
            command=cfg.pop("command", ["true"]), **cfg))
    w = wake.Waker(tmp_path, "s_test", attended=lambda: False,
                   now=lambda: clock[0], is_host=is_host)
    return w, clock


def chat(text="hello", sender="ana"):
    return Envelope(kind="chat", sender=sender, text=text)


MINUTE = 60.0


# --- the cadence ---------------------------------------------------------------

def test_the_clock_starts_at_the_first_ask_not_at_zero(tmp_path):
    """«Never reminded» and «reminded an hour ago» are the same stored zero.

    Firing on the first heartbeat would remind an agent in the same minute its
    user started it, and again on every restart of a daemon that is crash-looping.
    """
    w, clock = waker(tmp_path)
    due, why = w.due()
    assert not due and why == "nothing unread"
    clock[0] += 9 * MINUTE
    assert w.due()[0] is False, "fired inside its own interval"


def test_it_fires_once_the_interval_has_passed(tmp_path):
    w, clock = waker(tmp_path)
    w.due()                                   # starts the clock
    clock[0] += 10 * MINUTE
    due, why = w.due()
    assert due and "reminder" in why


def test_it_does_not_fire_again_until_the_next_interval(tmp_path):
    """Ten minutes means ten minutes, not every heartbeat after the tenth."""
    w, clock = waker(tmp_path)
    w.due()
    clock[0] += 10 * MINUTE
    assert w.reminder_due() is True
    w.reminded()
    fired = 0
    for _ in range(60):                       # ten minutes of heartbeats
        clock[0] += 10
        if w.reminder_due():
            fired += 1
            w.reminded()
    assert fired == 1, f"fired {fired} times in one interval"


def test_the_interval_is_read_from_the_config(tmp_path, isolated):
    write_config(isolated, remind_every=30)
    w, clock = waker(tmp_path)
    w.due()
    clock[0] += 10 * MINUTE
    assert w.reminder_due() is False
    clock[0] += 20 * MINUTE
    assert w.reminder_due() is True


def test_zero_turns_it_off_entirely(tmp_path, isolated):
    write_config(isolated, remind_every=0)
    w, clock = waker(tmp_path)
    w.due()
    clock[0] += 10 * 60 * MINUTE              # ten hours
    assert w.reminder_due() is False
    due, why = w.due()
    assert not due and why == "nothing unread"


def test_a_disarmed_wake_carries_no_reminder_of_its_own(tmp_path):
    """This route refuses at its first line, and always did.

    That refusal was the whole gap: for a long time the wake was the ONLY
    route, so an agent that arms none — which is what this project tells a
    Claude Code host to do — could not be reminded at all. The monitor is the
    other route now (`tests/test_reminder_on_the_monitor.py`), and it is asked
    first, so nothing about this line changes: the wake still reminds nobody
    when there is no wake.
    """
    w, clock = waker(tmp_path, armed=False)
    w.due()
    clock[0] += 60 * MINUTE
    due, why = w.due()
    assert not due and why == "no wake command configured"


# --- a message always wins -----------------------------------------------------

def test_a_reminder_never_delays_a_message(tmp_path):
    """The settle window belongs to the burst. Firing a reminder inside it
    would spend the turn, and `min_gap` would then hold the messages back."""
    w, clock = waker(tmp_path, settle=20, min_gap=90)
    w.due()
    clock[0] += 10 * MINUTE
    w.note(chat("look at the build"))
    due, why = w.due()
    assert not due and "burst" in why, "the reminder jumped the settle window"
    clock[0] += 20
    due, why = w.due()
    assert due and "unread" in why, "the messages are what is owed a turn"
    assert w.take() is not None


def test_a_reminder_rides_along_with_the_messages(tmp_path):
    """Due at the same moment, it goes in the same turn rather than a second one."""
    w, clock = waker(tmp_path, settle=0, min_gap=0)
    w.due()
    clock[0] += 10 * MINUTE
    w.note(chat())
    clock[0] += 1
    assert w.due()[0] is True
    assert w.take() is not None
    assert w.reminder_due() is True, "it should ride along, not wait for its own turn"


def test_a_reminder_does_not_displace_a_batch(tmp_path):
    """`take` answers with the messages. There is no such thing as a reminder
    that consumed the turn the messages were owed."""
    w, clock = waker(tmp_path, settle=0, min_gap=0)
    w.due()
    clock[0] += 10 * MINUTE
    w.note(chat("first"))
    clock[0] += 1
    batch = w.take()
    assert batch is not None and [e["text"] for e in batch.events()] == ["first"]


def test_it_waits_for_the_gap_like_everything_else(tmp_path):
    """A turn that has just run is a turn. The reminder does not skip the queue."""
    w, clock = waker(tmp_path, settle=0, min_gap=90)
    w.due()
    clock[0] += 10 * MINUTE
    w.note(chat())
    clock[0] += 1
    w.succeeded(w.take())
    w.reminded()
    clock[0] += 10 * MINUTE
    clock[0] -= 10 * MINUTE - 10              # ten seconds after the turn
    assert w.due()[0] is False


# --- who is being reminded -----------------------------------------------------

def test_the_host_and_the_guest_are_told_different_things(tmp_path):
    host, _ = waker(tmp_path / "h", is_host=True)
    guest, _ = waker(tmp_path / "g", is_host=False)
    assert host.reminder()["text"] != guest.reminder()["text"]


def test_the_role_comes_from_the_profile_not_from_a_name(profile, tmp_path):
    profile.is_host = True
    profile.name = "guest-sounding-name"
    profile.save()
    daemon = a_daemon(profile)
    assert daemon.waker.is_host is True
    assert daemon.waker.reminder()["text"] == config.DEFAULT_REMIND_HOST


def test_the_shipped_defaults_say_what_to_run(tmp_path):
    host, _ = waker(tmp_path / "h", is_host=True)
    guest, _ = waker(tmp_path / "g")
    said = host.reminder()["text"]
    assert "collab who" in said and "collab batch status" in said
    assert "collab check" in said and "collab stats --json" in said
    said = guest.reminder()["text"]
    assert "collab working" in said and "collab idle" in said
    assert "collab task complete" in said and "collab recv" in said


def test_every_command_the_shipped_reminders_cite_parses():
    """They are shipped to be RUN. A flag that does not exist is a turn spent
    on an error message, every ten minutes, for ever."""
    import re

    parser = cli.build_parser()
    known = {}
    for action in parser._actions:
        for name, sub in (getattr(action, "choices", None) or {}).items():
            known[name] = {o for a in sub._actions for o in a.option_strings}
    usage = re.compile(r"collab\s+([a-z][a-z-]*)((?:\s+--?[a-z][\w-]*)*)")
    for text in (config.DEFAULT_REMIND_HOST, config.DEFAULT_REMIND_GUEST,
                 wake.REMINDER_PROMPT):
        for command, flags in usage.findall(text):
            assert command in known, f"no such command: collab {command}"
            for flag in flags.split():
                assert flag in known[command], f"collab {command} has no {flag}"


def test_the_defaults_are_short_enough_to_arrive_every_ten_minutes():
    """It is a tax the agent pays on every interval, not a document."""
    for text in (config.DEFAULT_REMIND_HOST, config.DEFAULT_REMIND_GUEST):
        assert len(text.splitlines()) <= 12, "too long to read at a glance"
        assert len(text) < 800


# --- text of one's own ---------------------------------------------------------

def test_a_custom_text_replaces_the_shipped_one(tmp_path, isolated):
    write_config(isolated, remind_host="host: mind the board",
                 remind_guest="guest: keep going")
    host, _ = waker(tmp_path / "h", is_host=True)
    guest, _ = waker(tmp_path / "g")
    assert host.reminder()["text"] == "host: mind the board"
    assert guest.reminder()["text"] == "guest: keep going"


def test_an_empty_text_falls_back_to_the_shipped_default(tmp_path, isolated):
    """Empty is «I have not written one», not «remind me with nothing»."""
    write_config(isolated, remind_host="", remind_guest="   ")
    host, _ = waker(tmp_path / "h", is_host=True)
    guest, _ = waker(tmp_path / "g")
    assert host.reminder()["text"] == config.DEFAULT_REMIND_HOST
    assert guest.reminder()["text"] == config.DEFAULT_REMIND_GUEST


def test_one_role_can_be_customised_without_touching_the_other(tmp_path, isolated):
    write_config(isolated, remind_guest="guest: keep going")
    host, _ = waker(tmp_path / "h", is_host=True)
    guest, _ = waker(tmp_path / "g")
    assert host.reminder()["text"] == config.DEFAULT_REMIND_HOST
    assert guest.reminder()["text"] == "guest: keep going"


# --- a config file nobody validated --------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"remind_every": "soon"}',
    '{"remind_every": Infinity}',
    '{"remind_every": -Infinity}',
    '{"remind_every": NaN}',
    '{"remind_every": 1e400}',
    '{"remind_every": -5}',
    '{"remind_every": {"minutes": 10}}',
    '{"remind_every": [10]}',
    '{"remind_every": null}',
    '{"remind_every": true}',
    '{"remind_every": 1}',
    '{"remind_host": 7, "remind_guest": {"a": 1}}',
    '{"remind_host": null}',
])
def test_a_hostile_value_never_raises_and_never_reminds_every_beat(tmp_path, isolated,
                                                                  raw):
    """This is read on the heartbeat. A TypeError here is a daemon that stops
    beating over one word somebody typed into a file by hand."""
    isolated.write_text(raw, encoding="utf-8")
    config._CACHE.clear()
    w, clock = waker(tmp_path)
    settings = w.reminder()
    assert isinstance(settings["text"], str) and settings["text"]
    every = settings["every"]
    assert every == 0 or every >= config.MIN_REMIND_EVERY, every
    w.due()
    fired = 0
    for _ in range(120):                      # ten minutes of heartbeats
        clock[0] += 5
        if w.reminder_due():
            fired += 1
            w.reminded()
    assert fired <= 2, f"reminded {fired} times in ten minutes"


def test_a_value_under_the_floor_is_refused_at_the_command(isolated, capsys):
    """Refused where somebody typed it, floored where a file already holds it.

    The same split `watch_status_segments` makes: a typo in a file costs the
    setting, a typo at a command that answered «ok» leaves somebody waiting
    for behaviour that was never going to happen.
    """
    assert cli.main(["config", "remind_every", "1"]) == 2
    assert "remind_every" in capsys.readouterr().err
    assert not isolated.exists() or "remind_every" not in json.loads(isolated.read_text())
    assert cli.main(["config", "remind_every", "0"]) == 0
    assert config.reminder_settings()["every"] == 0


def test_the_settings_round_trip_through_the_config_command(isolated):
    assert cli.main(["config", "remind_every", "15"]) == 0
    assert cli.main(["config", "remind_host", "mind the board"]) == 0
    assert config.reminder_settings(is_host=True) == {
        "every": 15, "text": "mind the board", "configured": True}
    assert cli.main(["config", "remind_every", "--unset"]) == 0
    assert config.reminder_settings()["every"] == config.DEFAULT_REMIND_EVERY


# --- what the agent is actually handed -----------------------------------------

def test_the_reminder_says_where_it_came_from(tmp_path):
    """An agent that reads it as a message from the room has been misled about
    who is talking: nobody is. It is its own configuration."""
    w, _ = waker(tmp_path)
    prompt = w.reminder_prompt(w.reminder()["text"])
    assert "collab" in prompt
    assert "remind_every" in prompt, "and how to change or stop it"
    assert "s_test" in prompt
    assert w.reminder()["text"] in prompt


def test_the_reminder_is_not_framed_as_a_message(tmp_path):
    w, _ = waker(tmp_path)
    prompt = w.reminder_prompt(w.reminder()["text"])
    assert "UNTRUSTED DATA" not in prompt, "nobody said this; it is not evidence"
    assert "MESSAGES (" not in prompt


def test_the_reminder_prompt_is_bounded(tmp_path, isolated):
    """It goes to the same places a batch does, including recipes that pass it
    as one argument, and Linux refuses any argument over 128 KiB."""
    write_config(isolated, remind_guest="z" * 500_000)
    w, _ = waker(tmp_path)
    assert len(w.reminder_prompt(w.reminder()["text"]).encode()) < wake.MAX_PROMPT_BYTES


# --- the daemon delivering it --------------------------------------------------

@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="bob",
                       host_name="alice", token="t", home=str(home),
                       participant_id="p_bob")
    p.save()
    return p


def a_daemon(profile, *, clock=None):
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = profile
    daemon.paths = d.DaemonPaths(profile.dir)
    daemon.waker = wake.Waker(daemon.paths.root, profile.session_id,
                              attended=lambda: False, is_host=profile.is_host,
                              **({"now": lambda: clock[0]} if clock else {}))
    daemon._waking = None
    daemon._waking_batch = None
    daemon._wake_note = ""
    daemon._wake_activity = None
    daemon._http = None
    daemon._notifying = set()
    return daemon


async def _wake_once(daemon):
    await daemon._maybe_wake()
    if daemon._waking is not None:
        await daemon._waking


def test_the_daemon_delivers_the_reminder_with_no_messages_at_all(profile, tmp_path):
    """The whole point: nothing arrived, and the agent gets a turn anyway."""
    landed = tmp_path / "landed.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"],
        settle=0, min_gap=0))
    asyncio.run(_wake_once(daemon))
    assert not landed.exists(), "fired inside its own first interval"
    clock[0] += 10 * MINUTE
    asyncio.run(_wake_once(daemon))
    assert landed.exists(), "the reminder never reached the agent"
    body = landed.read_text()
    assert "collab who" in body or "collab working" in body
    assert "remind_every" in body


def test_a_reminder_is_not_a_task_and_moves_nothing(profile, tmp_path, monkeypatch):
    """It creates no task, moves no batch, publishes no activity, and does not
    reach the hub. A reminder that did any of those would put a paragraph
    nobody wrote into everybody's transcript every ten minutes."""
    from collab import activity as act

    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c", "import sys; sys.stdin.read()"],
        settle=0, min_gap=0))
    posted = []
    monkeypatch.setattr(daemon, "_publish_activity",
                        lambda said: posted.append(said) or asyncio.sleep(0))
    daemon.waker.due()
    clock[0] += 10 * MINUTE
    asyncio.run(_wake_once(daemon))

    assert posted == [], "a reminder published activity"
    assert act.read_local(profile) == {} or not act.read_local(profile).get("what")
    assert daemon._http is None, "a reminder reached the hub"
    assert not list(daemon.waker.batches.glob("*.jsonl")), "a reminder cut a batch"
    assert not list(daemon.waker.done.glob("*.jsonl"))
    assert daemon.waker.waiting() == 0


def test_a_reminder_is_not_delivered_while_a_turn_is_in_flight(profile):
    """A second heartbeat during a slow turn must not start a second agent."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c", "import time; time.sleep(0.4)"],
        settle=0, min_gap=0))
    daemon.waker.due()
    clock[0] += 10 * MINUTE

    async def race():
        await daemon._maybe_wake()
        first = daemon._waking
        assert first is not None, "the reminder never started a turn"
        clock[0] += 10 * MINUTE
        await daemon._maybe_wake()
        assert daemon._waking is first, "started a second turn mid-turn"
        await first

    asyncio.run(race())


def test_the_reminder_clock_survives_the_daemon_restarting(profile):
    """Otherwise every restart is a reminder, and a crash loop is a flood."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(command=["true"]))
    daemon.waker.due()
    clock[0] += 10 * MINUTE
    daemon.waker.reminded()

    again = a_daemon(profile, clock=clock)
    assert again.waker.reminder_due() is False
    clock[0] += 10 * MINUTE
    assert again.waker.reminder_due() is True


def test_a_woken_turn_carries_both_when_both_are_due(profile, tmp_path):
    """One turn, not two: the messages and the reminder in the same prompt."""
    landed = tmp_path / "landed.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock=clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"],
        settle=0, min_gap=0))
    daemon.waker.due()
    clock[0] += 10 * MINUTE
    daemon.waker.note(chat("please review the patch"))
    clock[0] += 1
    asyncio.run(_wake_once(daemon))

    body = landed.read_text()
    assert "please review the patch" in body, "the messages were dropped"
    assert "UNTRUSTED DATA" in body, "and still framed as data"
    assert "remind_every" in body, "the reminder did not ride along"
    assert daemon.waker.reminder_due() is False, "its clock was not reset"


# --- no wake armed, and the check that says so ---------------------------------

def test_the_check_says_a_configured_reminder_cannot_be_delivered(profile, monkeypatch,
                                                                  isolated):
    write_config(isolated, remind_every=10)
    (profile.dir / "status.json").write_text(json.dumps({
        "state": "live", "heartbeat": time.time(), "unread_messages": 0,
        "wake": {"armed": False}}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    results = {r["check"]: r for r in cli._checks(profile)}
    assert "reminder" in results, "the honest failure was silent"
    said = results["reminder"]
    assert said["verdict"] == cli.CHECK_WARN
    assert "no wake" in said["detail"]
    # The command, not the executable: `collab check` names itself by whatever
    # argv[0] it was invoked as, which under pytest is pytest.
    assert "wake agents" in said["fix"], "a failure without its fix is a scolding"
    assert "remind_every 0" in said["fix"], "and the way to decline it"


def test_the_check_is_quiet_once_a_wake_is_armed(profile, monkeypatch, isolated):
    write_config(isolated, remind_every=10)
    (profile.dir / "status.json").write_text(json.dumps({
        "state": "live", "heartbeat": time.time(), "unread_messages": 0,
        "wake": {"armed": True, "last_wake": time.time()}}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    assert "reminder" not in {r["check"] for r in cli._checks(profile)}


def test_the_check_is_quiet_when_the_reminder_is_off(profile, monkeypatch, isolated):
    write_config(isolated, remind_every=0)
    (profile.dir / "status.json").write_text(json.dumps({
        "state": "live", "heartbeat": time.time(), "unread_messages": 0}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    assert "reminder" not in {r["check"] for r in cli._checks(profile)}


def test_the_line_typed_into_a_pane_does_not_claim_messages_arrived(monkeypatch,
                                                                    tmp_path):
    """`tmux send-keys` can only carry a pointer to the file, so that one line
    is all the agent has to go on before it opens it. Told that messages
    arrived and finding a reminder, it has been misled by its own tooling.

    It goes through the `COLLAB_WAKE_PROMPT` fallback deliberately: that path
    raised `NameError: contextlib` for anything reaching it, which nothing had
    ever done because the daemon always writes the prompt to stdin as well.
    """
    import argparse
    import contextlib
    import io

    class _Answer:
        def __init__(self, code=0, out=""):
            self.returncode, self.stdout, self.stderr = code, out, ""

    typed = []

    def fake_tmux(argv, **_kwargs):
        typed.append(argv)
        if "display-message" in argv:
            return _Answer(0, "900 0 codex")
        return _Answer(0)

    written = tmp_path / "reminder.txt"
    written.write_text("collab's standing reminder for session s_test", "utf-8")
    monkeypatch.setattr(wake.subprocess, "run", fake_tmux)
    monkeypatch.setenv("COLLAB_WAKE_PROMPT", str(written))
    monkeypatch.setenv("COLLAB_WAKE_KIND", "reminder")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    args = argparse.Namespace(to="tmux", target="%0", expect_pid=None,
                              expect_command=None)
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._wake_deliver(args, wake) == 0
    line = " ".join(str(a) for argv in typed for a in argv)
    assert "standing reminder" in line
    assert "messages arrived" not in line


def _wake_show(profile, monkeypatch, **kwargs):
    import argparse
    import contextlib
    import io

    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(**{
        "session": None, "json": False, "notify": None, "settle": None,
        "min_gap": None, "timeout": None, "run": [], "agent": None,
        "target": None, "yes": True, "to": None, "expect_pid": None,
        "expect_command": None, "action": "show", **kwargs})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_wake(args)
    return code, out.getvalue()


def test_wake_show_says_the_reminder_is_riding_on_it(profile, monkeypatch):
    """This is the page somebody opens to ask what the daemon does to their
    agent. A reminder delivered on this wake and never mentioned on it is a
    thing running unattended that nothing anywhere admits to."""
    wake.write_config(d.DaemonPaths(profile.dir).root,
                      wake.WakeConfig(command=["true"]))
    code, out = _wake_show(profile, monkeypatch)
    assert code == 0
    assert "reminder" in out and "every 10m" in out
    assert "guest" in out, "and which of the two texts arrives"


def test_wake_show_says_where_a_disarmed_wakes_reminder_goes_instead(profile,
                                                                     monkeypatch):
    """And says it only to somebody who asked for a reminder.

    It used to say a disarmed wake took the reminder with it. That is no longer
    true — it rides a followed stream too — so this page names the other route
    rather than sending the reader off to arm a wake it does not need.

    This asserted the warning on an UNCONFIGURED profile, which contradicted
    the test below it: `remind_every` is ten by default, so a disarmed wake
    told every reader about a feature they had never touched. The rule is the
    one `collab check` already follows — nothing configured is a decision, not
    a fault — and the two pages have to agree or the quieter one is wrong.
    """
    config.setting("remind_every").write(15)
    code, out = _wake_show(profile, monkeypatch)
    assert code == 0 and "disarmed" in out
    assert "reminder" in out, "the reminder has no route here and nothing said so"
    assert "listen --follow" in out, "and the route that does not need a wake"


def test_a_reminder_nobody_asked_for_is_not_a_fault(profile, monkeypatch):
    """Nothing configured is a decision, not a fault — the same rule the stats
    check follows. Warning every user who never armed a wake is the noise that
    gets `collab check` ignored."""
    (profile.dir / "status.json").write_text(json.dumps({
        "state": "live", "heartbeat": time.time(), "unread_messages": 0}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    assert "reminder" not in {r["check"] for r in cli._checks(profile)}
