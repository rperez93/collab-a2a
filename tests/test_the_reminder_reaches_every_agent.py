"""The standing reminder, proved on every route that can carry it.

It has two routes and most agents have exactly one. A followed stream costs no
turn and reaches anything that can hold a monitor; the wake spends a turn and
reaches everything else, through five different families of recipe. Any one of
those five could drop a reminder-ONLY turn without anybody noticing, because
the symptom is an agent that drifts exactly as it did before — which is what it
was doing anyway.

So each route gets its own test, and each asks the same question: with nothing
unread at all, does the reminder's own text reach what the agent actually
reads? For a pane that is the file the typed line points at; for a Codex thread
it is the queued message; for a fresh run it is standard input, either read
directly or spliced into a shell argument.

Two more things are held here. An agent holding BOTH routes is reminded once,
not twice, because the daemon keeps one clock and offers the cheaper route
first. And a reminder that rides along with messages goes underneath them: the
messages are why the turn is being spent, and a paragraph that arrives every
ten minutes must not push what somebody said down the page.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import time

import pytest

from collab import cli, config as cfg, wake
from collab.client import daemon as d
from collab.config import SessionProfile
from collab.protocol import Envelope

MINUTE = 60.0
#: A phrase that appears in the reminder's own framing and NOWHERE in the
#: batch's. The shipped reminder text cites `collab working`, and so does the
#: batch prompt — a marker either of them could produce proves nothing about
#: which one arrived, which is the whole question here.
REMINDER_MARK = "standing reminder"
#: And a phrase from the shipped guest text itself, for the monitor's drop —
#: which carries the reminder unframed, because the follower puts its own
#: one-line banner in front of it rather than the wake's six-line preamble.
SHIPPED = "Stay on the objective you were given"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A throwaway global config. Never the machine's own."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    yield
    cfg._CACHE.clear()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="bob",
                           host_name="alice", token="t", home=str(home),
                           participant_id="p_bob")
    saved.save()
    return saved


def a_daemon(profile, clock):
    """Enough of a daemon to run one wake, with a clock the test winds."""
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = profile
    daemon.paths = d.DaemonPaths(profile.dir)
    daemon.waker = wake.Waker(daemon.paths.root, profile.session_id,
                              attended=lambda: False, is_host=profile.is_host,
                              now=lambda: clock[0])
    daemon._waking = None
    daemon._waking_batch = None
    daemon._wake_note = ""
    daemon._wake_activity = None
    daemon._http = None
    daemon._notifying = set()
    return daemon


async def _one_turn(daemon):
    await daemon._maybe_wake()
    if daemon._waking is not None:
        await daemon._waking


def _fire_a_reminder(daemon, clock, command):
    """Arm this command, let the interval pass, and run the turn it earns."""
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=command, settle=0, min_gap=0))
    asyncio.run(_one_turn(daemon))          # starts the interval, sends nothing
    clock[0] += 10 * MINUTE
    asyncio.run(_one_turn(daemon))


# --- the fresh-run recipes: the reminder arrives on standard input ---------------

def test_a_reminder_only_turn_reaches_a_recipe_that_reads_stdin(profile, tmp_path):
    """`codex-exec`, `claude -p`, `gemini`, `copilot`, `goose`: the batch is
    piped, and with no batch it is the reminder that has to be."""
    landed = tmp_path / "stdin.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    _fire_a_reminder(daemon, clock, [
        sys.executable, "-c",
        f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"])
    body = landed.read_text()
    assert REMINDER_MARK in body, "the reminder's own text never arrived"
    assert "nobody typed this" in body, "and it is framed as nobody's message"


def test_a_reminder_only_turn_reaches_a_recipe_that_takes_an_argument(
        profile, tmp_path):
    """`cursor-agent`, `opencode`, `amp`, `aider` splice the prompt into a
    shell argument with `"$(cat)"`. A prompt that is empty there is not a short
    argument, it is a turn started with nothing in it."""
    landed = tmp_path / "argument.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    _fire_a_reminder(daemon, clock, [
        "sh", "-c", f'printf "%s" "$(cat)" > {landed}'])
    assert REMINDER_MARK in landed.read_text()


def test_those_two_shapes_are_every_fresh_run_recipe_there_is():
    """The generalisation the two tests above rest on, held to the table rather
    than assumed: a recipe added in some third shape would slip past both."""
    for recipe in wake.RECIPES:
        if recipe.delivers != wake.FRESH_RUN:
            continue
        joined = " ".join(recipe.argv)
        assert recipe.reads_stdin or '"$(cat)"' in joined, (
            f"{recipe.agent} neither reads stdin nor splices it in")


# --- the tmux pane: a line pointing at a file that holds the reminder -----------

class _Answer:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


def _fake_tmux(recorded, current_command="claude"):
    def runner(argv, **_kwargs):
        recorded.append(argv)
        if "display-message" in argv:
            return _Answer(0, f"900 0 {current_command}")
        return _Answer(0)
    return runner


def test_a_reminder_only_turn_reaches_a_tmux_pane(profile, tmp_path, monkeypatch):
    """Two halves, and the route is only proved by both. The daemon has to
    write the reminder somewhere and name it in the environment; the delivery
    has to type a line pointing at that file, saying it is a reminder."""
    seen = tmp_path / "env.json"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    _fire_a_reminder(daemon, clock, [
        sys.executable, "-c",
        "import json, os, sys;"
        f" open({str(seen)!r}, 'w').write(json.dumps("
        "{'prompt': os.environ.get('COLLAB_WAKE_PROMPT'),"
        " 'kind': os.environ.get('COLLAB_WAKE_KIND'),"
        " 'stdin': sys.stdin.read()}))"])
    handed = json.loads(seen.read_text())
    assert handed["kind"] == "reminder"
    assert REMINDER_MARK in handed["stdin"]
    written = cli.Path(handed["prompt"])
    assert REMINDER_MARK in written.read_text(), \
        "the file the pane is pointed at does not hold the reminder"

    typed = []
    monkeypatch.setattr(wake, "_tmux", lambda argv, runner=None: (
        typed.append(argv) or (0, "900 0 claude") if "display-message" in argv
        else (typed.append(argv) or (0, ""))))
    monkeypatch.setenv("COLLAB_WAKE_PROMPT", handed["prompt"])
    monkeypatch.setenv("COLLAB_WAKE_KIND", "reminder")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    args = argparse.Namespace(to="tmux", target="%3", expect_pid=None,
                              expect_command=None)
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._wake_deliver(args, wake) == 0
    line = " ".join(str(a) for argv in typed for a in argv)
    assert handed["prompt"] in line, "the typed line does not name the file"
    assert "standing reminder" in line
    assert "messages arrived" not in line, "it would be a lie about what is there"


# --- the Codex thread: the reminder is the queued message -----------------------

def test_a_reminder_only_turn_reaches_a_codex_thread(profile, tmp_path, monkeypatch):
    """`codex queue` carries the whole prompt rather than a pointer, so what
    has to be proved here is that the prompt is the reminder and not empty."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    daemon.waker.due()                       # start the interval
    clock[0] += 10 * MINUTE
    prompt = daemon.waker.turn_prompt(None, daemon.waker.reminder()["text"])
    assert REMINDER_MARK in prompt

    queued = []

    def runner(argv, **_kwargs):
        queued.append(argv)
        return _Answer(0, "ok")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("COLLAB_WAKE_PROMPT", "")
    monkeypatch.setattr(cli.sys.stdin, "read", lambda: prompt)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(wake.subprocess, "run", runner)
    args = argparse.Namespace(to="codex", target="th_9", expect_pid=None,
                              expect_command=None)
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._wake_deliver(args, wake) == 0
    sent = queued[0]
    assert "queue" in sent and "th_9" in sent
    assert any(REMINDER_MARK in str(part) for part in sent), \
        "the thread was queued something with no reminder in it"


# --- the monitor: the drop a followed stream reads ------------------------------

def test_a_reminder_reaches_an_agent_that_only_holds_a_monitor(profile, monkeypatch):
    """The route this project tells most agents to use, and the one the
    reminder could not reach at all for a version: `Waker.due` refuses at its
    first line with no wake command configured."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    monkeypatch.setattr(d, "watchers", lambda p: [4242])
    daemon._remind_the_monitor()             # starts the interval
    assert wake.reminder_waiting(daemon.paths.root) is None
    clock[0] += 10 * MINUTE
    daemon._remind_the_monitor()
    drop = wake.reminder_waiting(daemon.paths.root)
    assert drop is not None
    assert SHIPPED in drop["text"]
    printed = wake.reminder_line(drop)
    assert SHIPPED in printed
    assert "[reminder]" in printed, "and the one line of framing that says whose"


def test_an_agent_holding_both_routes_is_reminded_once(profile, monkeypatch, tmp_path):
    """One clock, kept by the daemon, and the cheaper route offered first. Two
    clocks would drift, and an agent with both would be reminded twice."""
    landed = tmp_path / "woken.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'a').write(sys.stdin.read())"],
        settle=0, min_gap=0))
    monkeypatch.setattr(d, "watchers", lambda p: [4242])

    daemon._remind_the_monitor()
    asyncio.run(_one_turn(daemon))
    clock[0] += 10 * MINUTE
    daemon._remind_the_monitor()
    asyncio.run(_one_turn(daemon))

    assert wake.reminder_waiting(daemon.paths.root) is not None, "the monitor got it"
    assert not landed.exists(), "and the wake spent a turn on it as well"
    assert daemon.waker.reminded_via == "monitor"


# --- riding with messages ---------------------------------------------------------

def test_a_reminder_riding_with_a_batch_goes_underneath_the_messages(
        profile, tmp_path):
    """The messages are why the turn is being spent. A paragraph that arrives
    every ten minutes must not push what somebody actually said down the page."""
    landed = tmp_path / "both.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"],
        settle=0, min_gap=0))
    daemon.waker.due()
    clock[0] += 10 * MINUTE
    daemon.waker.note(Envelope(kind="chat", sender="ana",
                               text="the auth refactor is ready"),
                      own_name="bob")
    asyncio.run(_one_turn(daemon))

    body = landed.read_text()
    assert "the auth refactor is ready" in body
    assert REMINDER_MARK in body
    assert body.index("the auth refactor is ready") < body.index(REMINDER_MARK)


# --- the trace it now leaves ------------------------------------------------------

def test_the_route_that_carried_it_is_recorded(profile, monkeypatch, tmp_path):
    """«My agent is not being reminded» could not be told from «it is, by the
    route you forgot it had», because neither route left a trace."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    assert daemon.waker.reminded_via == ""
    monkeypatch.setattr(d, "watchers", lambda p: [4242])
    daemon._remind_the_monitor()
    clock[0] += 10 * MINUTE
    daemon._remind_the_monitor()
    assert daemon.waker.reminded_via == "monitor"
    assert wake.Waker(daemon.paths.root, "s").reminded_via == "monitor", \
        "it did not survive the daemon restarting"


def test_the_wake_records_itself_as_the_route(profile, tmp_path):
    landed = tmp_path / "x.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    _fire_a_reminder(daemon, clock, [
        sys.executable, "-c",
        f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"])
    assert daemon.waker.reminded_via == "wake"


# --- and it can be asked for now ---------------------------------------------------

def _remind(profile, monkeypatch, **flags):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(**{"action": "now", "session": None, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_remind(args)
    return code, out.getvalue()


def test_asking_for_one_now_makes_the_next_heartbeat_deliver_it(
        profile, monkeypatch, tmp_path):
    """A MARKER AND NOT THE STATE. The daemon reads `state.json` once, at
    construction, so a command winding `reminded_at` back would change a file
    the daemon is not going to read again — and would be overwritten by its
    next write."""
    landed = tmp_path / "asked.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'w').write(sys.stdin.read())"],
        settle=0, min_gap=0))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    asyncio.run(_one_turn(daemon))
    assert not landed.exists(), "it fired inside its own first interval"

    code, out = _remind(profile, monkeypatch)
    assert code == 0 and "via your wake" in out

    asyncio.run(_one_turn(daemon))           # the clock has NOT moved
    assert landed.exists(), "the request never reached the running daemon"
    assert REMINDER_MARK in landed.read_text()


def test_the_request_is_consumed_and_does_not_fire_twice(profile, monkeypatch,
                                                         tmp_path):
    landed = tmp_path / "once.txt"
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(
        command=[sys.executable, "-c",
                 f"import sys; open({str(landed)!r}, 'a').write('turn\\n')"],
        settle=0, min_gap=0))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    asyncio.run(_one_turn(daemon))
    _remind(profile, monkeypatch)
    for _ in range(3):
        asyncio.run(_one_turn(daemon))
    assert landed.read_text().count("turn") == 1


def test_reading_the_state_does_not_eat_the_request(profile, monkeypatch):
    """`collab status` and `collab wake show` ask whether one is due. Asking
    must not consume a request they are only reporting."""
    clock = [10_000.0]
    daemon = a_daemon(profile, clock)
    wake.write_config(daemon.paths.root, wake.WakeConfig(command=["true"]))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    _remind(profile, monkeypatch)
    told = wake.Waker(daemon.paths.root, "s").explain()
    assert told["due"] is True and "reminder" in told["why"]
    assert daemon.waker.remind_now_marker.exists(), "the reader ate it"


def test_asking_with_the_reminder_off_says_so(profile, monkeypatch, isolated):
    cfg.setting("remind_every").write(0)
    code, out = _remind(profile, monkeypatch)
    assert code == 1
    assert "remind_every 10" in out


def test_asking_with_no_route_at_all_says_which_two_are_missing(profile, monkeypatch):
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    code, out = _remind(profile, monkeypatch)
    assert code == 1
    assert "listen --follow" in out and "wake" in out


def test_with_no_daemon_the_monitors_drop_is_written_directly(profile, monkeypatch):
    """The wake IS the daemon, so with none running the monitor is the only
    route left — and nothing is going to pick up a marker."""
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    monkeypatch.setattr(cli, "watchers", lambda p: [4242])
    code, out = _remind(profile, monkeypatch)
    assert code == 0 and "followed stream" in out
    drop = wake.reminder_waiting(d.DaemonPaths(profile.dir).root)
    assert drop is not None and SHIPPED in drop["text"]


def test_with_no_daemon_and_only_a_wake_it_says_to_start_the_listener(
        profile, monkeypatch):
    wake.write_config(d.DaemonPaths(profile.dir).root,
                      wake.WakeConfig(command=["true"]))
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    code, out = _remind(profile, monkeypatch)
    assert code == 1 and "daemon start" in out


# --- and every surface says which route carries it ---------------------------------

def test_check_names_the_route_and_when_the_last_one_went(profile, monkeypatch):
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread_messages": 0,
         "wake": {"armed": False}}))
    cfg.setting("remind_every").write(10)
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [4242])
    said = {r["check"]: r for r in cli._checks(profile)}["reminder"]
    assert said["verdict"] == cli.CHECK_OK
    assert "via your monitor" in said["detail"]
    assert "never yet" in said["detail"]

    waker = wake.Waker(d.DaemonPaths(profile.dir).root, "s")
    waker.reminded("monitor")
    said = {r["check"]: r for r in cli._checks(profile)}["reminder"]
    assert "via monitor" in said["detail"]


def test_wake_show_says_the_route_the_last_one_took(profile, monkeypatch):
    root = d.DaemonPaths(profile.dir).root
    wake.write_config(root, wake.WakeConfig(command=["true"]))
    wake.Waker(root, "s").reminded("wake")
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    args = argparse.Namespace(session=None, json=False, notify=None, settle=None,
                              min_gap=None, timeout=None, run=[], agent=None,
                              target=None, yes=True, to=None, expect_pid=None,
                              expect_command=None, action="show")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        assert cli.cmd_wake(args) == 0
    assert "via wake" in out.getvalue()
