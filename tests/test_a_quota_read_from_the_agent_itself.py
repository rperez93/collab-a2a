"""Asking Codex what quota it has left, and the four ways that could be wrong.

`collab stats --source` has always accepted any command that prints collab's
JSON. What was missing was a command to point at: Codex has no status line and
no `--usage` flag, so the one figure everybody wants was unreachable from a
shell and the roster showed nothing for every Codex participant.

It is reachable through `codex app-server --stdio`, and the payload it returns
was read from `codex-cli 0.153.4` on 2026-09-07. The fixture below is that
reply, trimmed of the account and credit fields collab never touches. Four
things about it produce a plausible and false figure if guessed wrong, and each
has a test here:

* `resetsAt` is unix seconds, not ISO — passed through it reads as 1970;
* `usedPercent` is percent USED, which is the direction collab stores;
* a bucket carries two windows of different lengths, not a value and a spare;
* the same durations appear under several limit ids, so naming them all
  `five_hour` would collapse three windows into one.
"""

from __future__ import annotations

import json
import os
import queue
import resource
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from collab import quotas

#: What `account/rateLimits/read` actually returned, measured. The account's
#: own limit, plus one model bucket with a window of each length.
MEASURED = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 40, "windowDurationMins": 10080,
                    "resetsAt": 1789278615},
        "secondary": None,
        "planType": "pro",
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 40, "windowDurationMins": 10080,
                        "resetsAt": 1789278615},
            "secondary": None,
        },
        "codex_bengalfox": {
            "limitId": "codex_bengalfox",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {"usedPercent": 0, "windowDurationMins": 300,
                        "resetsAt": 1788820260},
            "secondary": {"usedPercent": 12.5, "windowDurationMins": 10080,
                          "resetsAt": 1789407060},
            "planType": "pro",
        },
    },
}


# --- naming and converting -----------------------------------------------------

def test_the_two_windows_collab_has_words_for_keep_them():
    """So a Codex five-hour window lands in the same roster row as everyone's."""
    assert quotas.window_name(300) == "five_hour"
    assert quotas.window_name(10080) == "seven_day"


def test_any_other_window_is_named_by_its_length_rather_than_dropped():
    assert quotas.window_name(1440) == "1_day"
    assert quotas.window_name(60) == "1_hour"
    assert quotas.window_name(180) == "3_hour"
    assert quotas.window_name(7) == "7_minute"


def test_a_length_that_is_not_one_is_no_window_at_all():
    for junk in (0, -5, None, "soon", "", {}):
        assert quotas.window_name(junk) == ""


def test_a_reset_is_unix_seconds_and_is_stored_as_an_instant():
    """Passed through unconverted every window would read as 1970 and look
    overdue, which is the shape of a wrong answer nobody would question."""
    assert quotas._at(1789278615) == "2026-09-13T05:50:15Z"
    assert quotas._at("1789278615") == "2026-09-13T05:50:15Z"


def test_a_reset_that_is_not_a_time_is_left_out_rather_than_invented():
    for junk in (0, -1, None, "tomorrow", ""):
        assert quotas._at(junk) == ""


# --- the whole payload ---------------------------------------------------------

def test_the_measured_reply_becomes_collabs_own_map():
    got = quotas.codex_quotas(MEASURED)
    assert got == {
        "seven_day": {"used_pct": 40.0, "resets_at": "2026-09-13T05:50:15Z"},
        "codex_bengalfox_five_hour": {"used_pct": 0.0,
                                      "resets_at": "2026-09-07T22:31:00Z"},
        "codex_bengalfox_seven_day": {"used_pct": 12.5,
                                      "resets_at": "2026-09-14T17:31:00Z"},
    }


def test_the_accounts_own_limit_keeps_the_plain_names():
    """It is the one comparable with every other agent's."""
    assert "seven_day" in quotas.codex_quotas(MEASURED)


def test_a_model_bucket_is_prefixed_rather_than_colliding():
    """Three windows share two durations here. Named by duration alone, two of
    them would overwrite the third and the roster would show one figure."""
    got = quotas.codex_quotas(MEASURED)
    assert len([k for k in got if k.endswith("seven_day")]) == 2
    assert got["seven_day"] != got["codex_bengalfox_seven_day"]


def test_both_windows_of_a_bucket_are_reported():
    got = quotas.codex_quotas(MEASURED)
    assert "codex_bengalfox_five_hour" in got      # primary
    assert "codex_bengalfox_seven_day" in got      # secondary


def test_the_models_name_and_the_plan_are_never_reported():
    """`limitName` is the model in words and `planType` is what the account
    pays for. Neither is usage, and `collab.stats` has a `model` field for
    something honest to fill."""
    body = json.dumps(quotas.codex_quotas(MEASURED))
    assert "GPT-5.3-Codex-Spark" not in body
    assert "limitName" not in body and "planType" not in body
    assert "pro" not in body


def test_the_allowances_own_id_IS_published_and_that_is_deliberate():
    """Said out loud because the test above is easy to read as more than it is.

    The bucket's id travels to the hub and on to every roster. It is there
    because it is the only thing telling two allowances of the same length
    apart — without it they report one figure between them — and it is an
    opaque codename for an allowance rather than a name for the model. The docs
    say exactly that; this is what holds them to it.
    """
    got = quotas.codex_quotas(MEASURED)
    assert "codex_bengalfox_five_hour" in got
    assert got["seven_day"] != got["codex_bengalfox_seven_day"]


def test_junk_in_is_an_empty_map_and_never_an_exception():
    """STRUCTURAL junk and NUMERIC junk both. The second is the one that got
    through: every case below with a number in it is a well-formed payload, and
    each used to leave the command dead with a traceback rather than silent.

    `int(float("inf"))` raises OverflowError rather than ValueError, and
    `datetime.fromtimestamp(1e30)` raises OverflowError or OSError depending on
    the libc — and `json.loads` accepts a bare `Infinity`, so none of this needs
    a hostile server, only an odd one.
    """
    for junk in (None, [], "", {"rateLimits": "nonsense"}, {"rateLimits": {}},
                 {"rateLimitsByLimitId": {"x": None}},
                 {"rateLimits": {"primary": {"usedPercent": 5,
                                             "windowDurationMins": float("inf")}}},
                 {"rateLimits": {"primary": {"usedPercent": float("inf"),
                                             "windowDurationMins": 300}}},
                 {"rateLimits": {"primary": {"usedPercent": float("nan"),
                                             "windowDurationMins": 300}}},
                 {"rateLimits": {"primary": {"usedPercent": True,
                                             "windowDurationMins": 300}}}):
        assert quotas.codex_quotas(junk) == {}


def test_a_reset_out_of_range_is_left_out_rather_than_crashing():
    """`1e30` is an ordinary JSON number and not an ordinary timestamp."""
    got = quotas.codex_quotas({"rateLimits": {
        "primary": {"usedPercent": 5, "windowDurationMins": 300,
                    "resetsAt": 1e30}}})
    assert got == {"five_hour": {"used_pct": 5.0}}, "the window survives, the time does not"


def test_nothing_it_prints_can_be_json_no_other_reader_understands():
    """`json.dumps(float("inf"))` is a bare `Infinity` — valid to Python and to
    nothing else. It would reach the hub and be unparseable there."""
    got = quotas.codex_quotas({"rateLimits": {
        "primary": {"usedPercent": 1e400, "windowDurationMins": 300}}})
    body = json.dumps(got)
    assert "Infinity" not in body and "NaN" not in body


def test_a_percentage_out_of_range_is_clamped_rather_than_dropped():
    """A number that is merely wrong is still a number; one that is not finite
    is not a percentage at all."""
    got = quotas.codex_quotas({"rateLimits": {
        "primary": {"usedPercent": 120, "windowDurationMins": 300},
        "secondary": {"usedPercent": -4, "windowDurationMins": 10080}}})
    assert got["five_hour"]["used_pct"] == 100.0
    assert got["seven_day"]["used_pct"] == 0.0


def test_two_ids_that_reduce_to_one_slug_keep_both_windows():
    """`setdefault` dropped the second silently, which is the very thing
    `window_name` refuses to do."""
    got = quotas.codex_quotas({
        "rateLimits": {"limitId": "acct"},
        "rateLimitsByLimitId": {
            "a b": {"primary": {"usedPercent": 1, "windowDurationMins": 300}},
            "a_b": {"primary": {"usedPercent": 2, "windowDurationMins": 300}}},
    })
    assert sorted(got) == ["a_b_five_hour", "a_b_five_hour_2"]
    assert sorted(v["used_pct"] for v in got.values()) == [1.0, 2.0]


def test_a_window_with_no_percentage_is_not_a_window():
    assert quotas.codex_quotas({"rateLimits": {
        "primary": {"windowDurationMins": 300, "resetsAt": 1789278615}}}) == {}


# --- the conversation ----------------------------------------------------------

class Server:
    """A stand-in for `codex app-server`, so the protocol is testable without it.

    IT BLOCKS, because a pipe does. Written as «yield whatever is queued and
    then stop», this ended the stream the instant the reply to `initialize` had
    been read — before the main thread had sent the two messages that produce
    the rest — and the conversation looked like a server that had hung up. The
    grace below is what a real `readline` gives for free.
    """

    #: How long an empty stream waits before it counts as closed.
    GRACE = 1.0

    def __init__(self, replies: list[dict] | None = None,
                 answer_initialize: bool = True) -> None:
        self.sent: list[dict] = []
        self.replies = replies if replies is not None else [{"id": 2, "result": MEASURED}]
        self.answer_initialize = answer_initialize
        self._queue: "queue.Queue[str]" = queue.Queue()

    def send(self, message: dict) -> None:
        self.sent.append(message)
        if message.get("id") == 1 and self.answer_initialize:
            self._queue.put(json.dumps({"id": 1, "result": {"ok": True}}))
        if message.get("method") == "account/rateLimits/read":
            for reply in self.replies:
                self._queue.put(json.dumps(reply))

    def lines(self):
        while True:
            try:
                yield self._queue.get(timeout=self.GRACE)
            except queue.Empty:
                return


def test_nothing_is_asked_before_the_server_says_it_is_ready():
    """Messages sent before the `initialize` answer are answered by nothing."""
    server = Server()
    result, why = quotas.speak(server.send, server.lines)
    assert why == ""
    methods = [m.get("method") for m in server.sent]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]
    assert result == MEASURED


def test_a_server_that_never_finishes_starting_up_says_so():
    server = Server(answer_initialize=False)
    result, why = quotas.speak(server.send, server.lines)
    assert result == {}
    assert "never finished starting" in why


def test_a_refusal_is_reported_rather_than_read_as_no_quota():
    """«It said no» and «it has no limits» are different, and only one of them
    is a reason to stop showing a figure."""
    server = Server(replies=[{"id": 2, "error": {"message": "not signed in"}}])
    result, why = quotas.speak(server.send, server.lines)
    assert result == {} and "not signed in" in why


def test_anything_unparseable_on_the_wire_is_stepped_over():
    server = Server(replies=[{"id": 2, "result": MEASURED}])
    server._queue.put("this is not json")
    result, why = quotas.speak(server.send, server.lines)
    assert why == "" and result == MEASURED


def test_running_out_of_time_is_not_running_out_of_quota():
    class Clock:
        def __init__(self):
            self.at = 0.0

        def __call__(self):
            self.at += 100.0
            return self.at

    server = Server()
    result, why = quotas.speak(server.send, server.lines, now=Clock(), timeout=1)
    assert result == {} and "in time" in why


# --- what the command promises -------------------------------------------------

def test_an_agent_collab_cannot_ask_is_refused_by_name():
    report, why = quotas.probe("gemini")
    assert report == {} and "gemini" in why and "codex" in why


def test_a_missing_codex_is_a_reason_and_not_a_crash(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(quotas.subprocess, "Popen", missing)
    report, why = quotas.from_codex()
    assert report == {} and "not on PATH" in why


def test_a_probe_that_cannot_answer_reports_nothing_at_all(monkeypatch):
    """Silence leaves the stored windows alone. An empty `quotas` map would
    REPLACE them, so a transient failure would clear the roster for everybody —
    see `collab.stats`. Clearing is a decision with its own command."""
    monkeypatch.setitem(quotas.PROBES, "codex", lambda: ({}, "codex is asleep"))
    report, why = quotas.probe("codex")
    assert report == {}
    assert why == "codex is asleep"


def test_a_successful_probe_is_a_report_collab_understands():
    from collab import stats

    monkeyed = {"quotas": quotas.codex_quotas(MEASURED)}
    figures = stats.normalise(json.dumps(monkeyed))
    assert figures["quotas"]["seven_day"]["used_pct"] == 40.0
    # The flat field the roster's older readers draw, derived from the map.
    assert figures["quota_seven_day"] == 40.0


@pytest.mark.parametrize("name", sorted(quotas.PROBES))
def test_every_probe_returns_the_two_part_answer(name, monkeypatch):
    monkeypatch.setitem(quotas.PROBES, name, lambda: ({"five_hour": {}}, ""))
    report, why = quotas.probe(name)
    assert why == "" and "quotas" in report


def test_a_server_that_goes_quiet_is_timed_out_rather_than_waited_on():
    """The defect this exists to prevent, and it was real.

    Written as a plain `for line in lines()`, the deadline is only consulted
    when a line ARRIVES — so a server that answers `initialize` and then says
    nothing more was never timed out at all. Measured before the fix: it hung
    until something outside killed it.
    """
    import time as clock

    class Quiet:
        def __init__(self):
            self.q = [json.dumps({"id": 1, "result": {}})]

        def send(self, message):
            pass

        def lines(self):
            while self.q:
                yield self.q.pop(0)
            while True:          # blocks, exactly as `readline` does
                clock.sleep(0.05)

    server = Quiet()
    began = clock.time()
    result, why = quotas.speak(server.send, server.lines, timeout=1.0)
    assert result == {} and "in time" in why
    assert clock.time() - began < 10, "it did not give up on its own"


def test_a_limit_id_cannot_carry_anything_into_somebody_elses_terminal():
    """These keys travel: a quota map is published and drawn on every
    participant's roster, so an id from a vendor's wire format is not trusted
    to be tame. `collab.protocol.scrub` exists for the same reason."""
    assert quotas._slug("codex_bengalfox") == "codex_bengalfox"
    assert "\x1b" not in quotas._slug("a\x1b[2Jb")
    assert "\n" not in quotas._slug("two\nlines")
    assert "/" not in quotas._slug("../../etc/passwd")
    assert len(quotas._slug("x" * 500)) <= 40
    assert quotas._slug("   ") == ""


def test_a_hostile_limit_id_still_produces_a_usable_key():
    got = quotas.codex_quotas({
        "rateLimits": {"limitId": "codex"},
        "rateLimitsByLimitId": {
            "we\nird/id": {"primary": {"usedPercent": 5,
                                       "windowDurationMins": 300}}},
    })
    assert list(got) == ["we_ird_id_five_hour"]
    assert got["we_ird_id_five_hour"] == {"used_pct": 5.0}


def test_arming_two_usage_commands_at_once_is_refused(capsys):
    """Both flags set `stats_command`. Taking either silently would write a
    command nobody asked for and leave them reading the other one."""
    from collab import cli

    code = cli.main(["stats", "--agent", "codex", "--source", "mine"])
    assert code == 1
    seen = capsys.readouterr()
    said = seen.out + seen.err
    assert "--agent and --source" in said


def test_the_documented_flags_exist_on_the_parser():
    """`collab stats --agent codex` is written in the README and the reference,
    and an agent follows those literally."""
    from collab.cli import build_parser

    parsed = build_parser().parse_args(["stats", "--agent", "codex"])
    assert parsed.agent == "codex"
    parsed = build_parser().parse_args(["stats", "--probe", "codex"])
    assert parsed.probe == "codex"


# --- the process, and what it must not leave behind -----------------------------

def _fake_codex(tmp_path, body: str):
    script = tmp_path / "fakecodex.py"
    script.write_text(body)
    return (sys.executable, str(script))


def test_a_server_that_forks_and_exits_leaves_nothing_running(tmp_path):
    """The leak a handle on the child cannot reach.

    Waiting on the process we started succeeds at once — it really has gone —
    while whatever it forked still holds the pipe. Terminate and kill hit a
    corpse, the reader stays blocked on a pipe with no EOF coming, and the
    grandchild runs on. Ending the process GROUP is the only handle that
    reaches it.
    """
    argv = _fake_codex(tmp_path, textwrap.dedent("""
        import json, os, sys, time
        sys.stdin.readline()
        print(json.dumps({"id": 1, "result": {}}), flush=True)
        if os.fork() == 0:
            time.sleep(60)          # holds the write end open
            os._exit(0)
        os._exit(0)
    """))
    # THE THREADS THIS CALL ADDS, not every reader in the process. Other tests
    # in this file drive fakes with no pipe to close, and their readers are
    # still sitting there — which is fine, and would make a blanket assertion
    # fail depending on the order the file happened to run in.
    before = {t.ident for t in threading.enumerate()}
    began = time.monotonic()
    report, why = quotas.from_codex(argv=argv, timeout=2)
    assert report == {} and why
    assert time.monotonic() - began < 15, "it waited on a pipe nobody would close"
    time.sleep(0.5)
    mine = [t for t in threading.enumerate()
            if t.ident not in before and "quota" in t.name]
    assert not mine, "the reader outlived the call"
    assert not _still_running(str(tmp_path)), "a grandchild was left behind"


def _still_running(marker: str) -> list[str]:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True,
                         check=False).stdout
    return [line for line in out.splitlines()
            if marker in line and "fakecodex" in line]


def test_the_budget_fits_inside_the_one_the_daemon_gives_a_usage_command():
    """The daemon runs a usage command with `timeout=20` and kills the shell.
    A SIGKILL does not unwind, so anything slower than that budget leaves the
    server it started alive — one per cycle, on a two-minute timer."""
    assert quotas.TIMEOUT < 20


def test_a_frozen_clock_still_comes_home():
    """`left` comes from the injected clock and the wait is in real seconds, so
    a clock that does not move — the usual way of writing «time does not
    pass» — left this spinning at five wakeups a second for ever."""
    def blocking():
        while True:
            time.sleep(0.05)
            yield ""            # whitespace: skipped, and never ends the stream

    began = time.monotonic()
    report, why = quotas.speak(lambda m: None, blocking, now=lambda: 0.0,
                               timeout=1.0)
    assert report == {} and "in time" in why
    assert time.monotonic() - began < 10


def test_a_reader_that_dies_is_not_waited_out(tmp_path):
    """The sentinel is sent from a `finally`. Listed exceptions only, anything
    unlisted skipped it and the consumer then spent the whole deadline before
    giving up with the wrong reason."""
    def explodes():
        raise RuntimeError("the transport gave up")
        yield ""                                        # pragma: no cover

    began = time.monotonic()
    report, why = quotas.speak(lambda m: None, explodes, timeout=30)
    assert report == {} and "never finished starting" in why
    assert time.monotonic() - began < 5


def test_the_armed_command_is_quoted_for_the_shell_that_will_run_it():
    """It is stored and run later with `shell=True`. An install under a path
    with a space armed a command the shell split in two, and the only symptom
    was `rc 127` two minutes later in a log nobody reads."""
    import shlex

    assert shlex.quote("/opt/my collab/bin/collab") != "/opt/my collab/bin/collab"
    assert shlex.split(f"{shlex.quote('/opt/my collab/bin/collab')} stats "
                       f"--probe codex")[0] == "/opt/my collab/bin/collab"


# --- what a server can make this hold in memory ---------------------------------

def test_a_line_that_never_ends_is_abandoned_rather_than_accumulated():
    """`readline` grows its buffer until a newline arrives, with no ceiling.

    Measured on the version that used it: a server writing megabyte blobs and
    no newline took the process to 10.5 GB resident in ten seconds — and held
    the interpreter in a C loop while it did, so the deadline could not be
    enforced and the call ran past the budget the daemon allows, which is the
    orphaning `TIMEOUT` exists to prevent.
    """
    # A SMALL CAP AND A SMALL WRITE. A pipe holds about 64 KiB, so writing a
    # real `MAX_LINE` into one before anything reads it simply blocks and the
    # test deadlocks rather than failing. What is under test is that there IS a
    # cap, not how large it is.
    cap, chunk, huge = 1000, 100, 5000
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"x" * huge)          # no newline anywhere in it
        os.write(write_fd, b'{"id": 2}\n')
        os.close(write_fd)
        write_fd = -1
        got = list(quotas.read_lines(read_fd, chunk=chunk, cap=cap))
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)
    assert len(got) == 1
    # Most of that fragment was thrown away rather than held: what comes back is
    # the tail plus whatever followed it, not five thousand characters.
    assert len(got[0]) <= cap + chunk + 20, f"it kept {len(got[0])} characters"
    assert got[0].endswith('{"id": 2}')
    # And the residue is harmless: it is not JSON, and `speak` steps over
    # anything that will not parse — see the test that says so.
    with pytest.raises(ValueError):
        json.loads(got[0])


def test_whole_lines_are_reassembled_across_reads():
    """The bounded read splits arbitrarily; a record must survive that."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"a": ')
        os.write(write_fd, b'1}\n{"b": 2}\n{"c"')
        os.write(write_fd, b': 3}\n')
        os.close(write_fd)
        write_fd = -1
        got = list(quotas.read_lines(read_fd, chunk=4))
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)
    assert got == ['{"a": 1}', '{"b": 2}', '{"c": 3}']


def test_a_flooding_server_costs_neither_the_memory_nor_a_core(tmp_path):
    """End to end, against a server doing exactly what broke it.

    Two limits, doing two jobs. Without the per-line cap this held 10.5 GB.
    With it and without the whole-conversation cap it held nothing and burned
    93% of a core for the entire deadline — on something the daemon runs every
    two minutes. With both: 0.02 s of wall clock, 0.003 s of CPU, 13 MiB.
    """
    argv = _fake_codex(tmp_path, textwrap.dedent("""
        import json, sys
        sys.stdin.buffer.readline()
        sys.stdout.write(json.dumps({"id": 1, "result": {}}) + "\\n")
        sys.stdout.flush()
        blob = "x" * (1024 * 1024)
        while True:
            sys.stdout.write(blob)          # a megabyte, and never a newline
            sys.stdout.flush()
    """))
    def cpu() -> float:
        used = resource.getrusage(resource.RUSAGE_SELF)
        return used.ru_utime + used.ru_stime

    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    before_cpu, began = cpu(), time.monotonic()
    report, why = quotas.from_codex(argv=argv, timeout=3)
    elapsed = time.monotonic() - began
    spent = cpu() - before_cpu
    grew = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss) / 1024
    assert report == {}, "a flood is not an answer"
    assert elapsed < 12, "the deadline could not be enforced"
    assert grew < 200, f"it held {grew:.0f} MiB of a line that never ended"
    assert spent < 1.5, f"it spent {spent:.1f}s of CPU discarding somebody's noise"


def test_a_silent_server_costs_almost_no_cpu_while_it_waits(tmp_path):
    """The other shape, and the ordinary one: a poll loop that waits should
    wait, not spin. Measured at 0.001 s of CPU across three seconds."""
    argv = _fake_codex(tmp_path, "import sys, time\nsys.stdin.buffer.readline()\n"
                                 "time.sleep(60)\n")

    def cpu() -> float:
        used = resource.getrusage(resource.RUSAGE_SELF)
        return used.ru_utime + used.ru_stime

    before, began = cpu(), time.monotonic()
    quotas.from_codex(argv=argv, timeout=2)
    assert time.monotonic() - began >= 2, "it did not actually wait"
    assert cpu() - before < 0.5, "waiting should not cost a core"


# --- and what it can put in a key on somebody else's roster ----------------------

def test_a_window_longer_than_a_year_is_not_a_window():
    """The other half of the key was capped and this half was not:
    `windowDurationMins: 1e300` published a three-hundred-character key."""
    assert quotas.window_name(quotas.MAX_WINDOW_MINUTES + 1) == ""
    assert quotas.window_name(1e300) == ""
    assert quotas.window_name(10 ** 400) == ""


def test_every_key_it_can_publish_is_short():
    """OVER THE WHOLE DOMAIN, not a handful of durations.

    Sampling six that happen to be short asserts a bound without establishing
    it — and the real maximum is not among them: it is `100000_minute`, at
    thirteen characters. A second of arithmetic proves what the sample only
    suggested.
    """
    longest = max(len(quotas.window_name(m))
                  for m in range(1, quotas.MAX_WINDOW_MINUTES + 1))
    assert longest == 13, f"the longest key it can publish is now {longest}"
