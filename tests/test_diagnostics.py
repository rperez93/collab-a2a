"""A record of what the daemon and the hub did, and what it refuses to record.

Everything else here reports the present: `status.json` says what is true now,
`collab check` says what is wrong now. So the question a bug report is made of
— what happened an hour ago — had no answer anywhere, and the report that
arrived was «it stopped working».

This log answers it, and the whole design is in what it will not write down.
The file is meant to be pasted into a public issue, so a single line of
somebody's conversation in it is a leak with no undo. These tests hold that
door shut from both sides: the callers pass classifications rather than text,
and `_safe` scrubs whatever arrives anyway — the home directory's prefix, the
host half of any URL, control characters, and anything past the caps.

It is off by default for a different reason, and one worth keeping separate: a
log nobody asked for is a file that grows on somebody's disk to answer a
question they may never ask.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import threading
import time
from datetime import date, timedelta

import pytest

from collab import cli, config as cfg, diagnostics as diag


@pytest.fixture
def recording(tmp_path, monkeypatch):
    """A directory being written to, with the setting on and the writer attached."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    cfg.set_diagnostics(True)
    diag.begin(tmp_path / "state", "daemon")
    diag._sampled_at = 0.0
    diag._swept_at = 0.0
    yield tmp_path / "state"
    diag._root = None
    diag._proc = ""
    cfg._CACHE.clear()


def _rows(root):
    """Everything recorded so far, once it has actually reached the disk.

    The flush is the point rather than a detail. Records are queued and written
    by a thread, so «log it and read it back» is two acts now — which is what
    every caller outside a test has always wanted and what a test has to say
    out loud.
    """
    diag.flush()
    return diag.records(root)


# --- off unless somebody asks --------------------------------------------------

def test_nothing_is_written_once_somebody_turns_it_off(tmp_path, monkeypatch):
    """It ships on now, so this is the setting doing its job rather than the
    default. Turning it off must still stop the writing outright — not merely
    stop new files, and not leave a directory behind to say it was here."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "c.json"))
    cfg._CACHE.clear()
    cfg.set_diagnostics(False)
    diag.begin(tmp_path / "state", "daemon")
    try:
        assert cfg.diagnostics_enabled() is False
        diag.log("start")
        assert not (tmp_path / "state" / diag.DIRNAME).exists()
    finally:
        diag._root = None
        cfg._CACHE.clear()


def test_it_is_kept_without_anybody_turning_it_on(tmp_path, monkeypatch):
    """The change 1.40.0 made, and the reason for it: a fault is reported after
    it happens, so a record you have to switch on first never covers the
    occurrence that made anybody look."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "c.json"))
    cfg._CACHE.clear()
    diag.begin(tmp_path / "state", "daemon")
    try:
        assert cfg.diagnostics_enabled() is True
        diag.log("start")
        diag.flush()
        assert list((tmp_path / "state" / diag.DIRNAME).glob("*.jsonl"))
    finally:
        diag._root = None
        cfg._CACHE.clear()


def test_it_is_read_live_so_turning_it_on_reaches_a_running_daemon(recording):
    diag.log("start")
    cfg.set_diagnostics(False)
    diag.log("stop")
    cfg.set_diagnostics(True)
    diag.log("reconnected")
    assert [r["event"] for r in _rows(recording)] == ["start", "reconnected"]


def test_a_module_that_never_attached_writes_nothing(tmp_path, monkeypatch):
    """Importing this must cost nothing. `log` before `begin` is the ordinary
    state of every process that does not keep a record."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "c.json"))
    cfg._CACHE.clear()
    diag._root = None
    cfg.set_diagnostics(True)
    diag.log("start")                      # must not raise, must write nothing
    assert list(tmp_path.rglob("*.jsonl")) == []
    cfg._CACHE.clear()


# --- what a record looks like --------------------------------------------------

def test_a_record_says_when_which_process_and_what(recording):
    before = time.time()
    diag.log("start", version="1.32.0", host=True)
    row = _rows(recording)[0]
    assert row["proc"] == "daemon"
    assert row["event"] == "start"
    assert row["version"] == "1.32.0" and row["host"] is True
    # The stamp is rounded to the millisecond, so it can land just under the
    # moment before it — a thousandth of a second is not a reading anybody
    # needs, and a full float here is eight characters of noise per line.
    assert before - 0.001 <= row["ts"] <= time.time() + 1


def test_the_file_is_one_per_day_named_for_the_day(recording):
    diag.log("start")
    diag.flush()
    written = list((recording / diag.DIRNAME).glob("*.jsonl"))
    assert len(written) == 1
    assert date.fromisoformat(written[0].stem)


def test_two_processes_share_one_file_and_are_told_apart(recording):
    diag.log("start")
    diag.begin(recording, "hub")
    diag.log("start")
    assert [r["proc"] for r in _rows(recording)] == ["daemon", "hub"]
    assert len(list((recording / diag.DIRNAME).glob("*.jsonl"))) == 1


# --- what it will not write down ----------------------------------------------

def test_a_path_under_home_loses_the_part_that_names_a_person(recording, monkeypatch):
    monkeypatch.setattr(diag, "_home_prefix", lambda: "/home/rafael")
    diag.log("crash", where="/home/rafael/work/api/main.py")
    said = _rows(recording)[0]["where"]
    assert said == "~/work/api/main.py"
    assert "rafael" not in said


def test_a_url_keeps_its_scheme_and_loses_its_address(recording):
    """The address is the tunnel, the host or the port — the one thing a public
    issue must not carry. The scheme stays because http versus https is
    occasionally the fault itself."""
    diag.log("feed_dropped", why="cannot connect to https://a1b2.ngrok.io/ext/collab")
    said = _rows(recording)[0]["why"]
    assert "ngrok.io" not in said and "a1b2" not in said
    assert "https://…" in said


@pytest.mark.parametrize("field", [
    "\x1b[2Jcleared your screen",
    "alice\rbob",
])
def test_control_characters_never_reach_the_file(field, recording):
    """This file is read with `cat` and pasted into an issue, so a control byte
    in it is a command to somebody else's terminal."""
    diag.log("wake_attempt", why=field)
    said = _rows(recording)[0]["why"]
    assert "\x1b" not in said and "\r" not in said


def test_a_long_field_is_cut_and_a_wide_record_is_cut(recording):
    diag.log("wake_attempt", why="x" * 5000)
    assert len(_rows(recording)[0]["why"]) == diag.MAX_FIELD
    diag.log("crash", **{f"f{i}": i for i in range(60)})
    # The four the record always carries — when, which process, which pid, and
    # what happened — and no more than `MAX_FIELDS` of whatever was passed.
    assert len(_rows(recording)[1]) <= diag.MAX_FIELDS + 4


def test_nothing_it_writes_can_stop_the_file_being_read_back(recording):
    """`json.dumps` writes `NaN` and `Infinity` as bare tokens no other parser
    accepts, so one bad float would make the whole day unreadable."""
    diag.log("memory", rss_mb=float("nan"), peak=float("inf"))
    row = _rows(recording)[0]
    assert row["rss_mb"] is None and row["peak"] is None


def test_a_field_that_is_not_data_at_all_does_not_raise(recording):
    diag.log("crash", where=object(), deep={"a": {"b": {"c": {"d": 1}}}})
    assert len(_rows(recording)) == 1


def test_a_half_written_last_line_does_not_lose_the_whole_day(recording):
    """Two processes append here and either can be killed mid-session."""
    diag.log("start")
    diag.flush()
    with open(diag.path_for(recording), "a") as fh:
        fh.write('{"event": "sto')
    assert [r["event"] for r in _rows(recording)] == ["start"]


# --- memory --------------------------------------------------------------------

def test_memory_is_sampled_on_a_clock_and_not_on_every_beat(recording):
    diag.sample_memory()
    for _ in range(50):
        diag.sample_memory()
    assert [r["event"] for r in _rows(recording)] == ["memory"]


def test_the_sample_says_whether_it_is_current_or_a_high_water_mark(recording):
    """They are not interchangeable: `getrusage` never falls, so a monotone
    line off it is not evidence of a leak. A reader has to be told which."""
    diag.sample_memory()
    row = _rows(recording)[0]
    assert row["rss_mb"] > 0
    assert row["source"] in ("current", "peak")


def test_the_span_is_reported_per_process(recording):
    """One line of «min 40, max 900» over a daemon and a hub describes neither."""
    rows = [{"event": "memory", "proc": "daemon", "rss_mb": 40.0, "ts": 1},
            {"event": "memory", "proc": "daemon", "rss_mb": 62.0, "ts": 2},
            {"event": "memory", "proc": "hub", "rss_mb": 900.0, "ts": 3}]
    span = diag.memory_span(rows)
    assert span["daemon"] == {"min": 40.0, "max": 62.0, "last": 62.0, "samples": 2}
    assert span["hub"]["max"] == 900.0


# --- retention ------------------------------------------------------------------

def _day_file(root, days_ago):
    when = date.today() - timedelta(days=days_ago)
    path = root / diag.DIRNAME / f"{when.isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event": "start"}\n')
    return path


def test_a_file_older_than_the_window_is_deleted(recording):
    old = _day_file(recording, diag.RETAIN_DAYS + 1)
    recent = _day_file(recording, 1)
    assert diag.sweep(recording, force=True) == 1
    assert not old.exists() and recent.exists()


def test_the_sweep_runs_once_a_day_and_not_on_every_beat(recording):
    diag.sweep(recording, force=True)
    _day_file(recording, diag.RETAIN_DAYS + 2)
    assert diag.sweep(recording) == 0, "swept again within the day"


def test_a_file_that_is_not_ours_is_left_where_it_is(recording):
    """A directory is not ours to tidy up merely because we write in it."""
    stray = recording / diag.DIRNAME / "notes.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("hello\n")
    diag.sweep(recording, force=True)
    assert stray.exists()


# --- the report -----------------------------------------------------------------

def _draft(profile, monkeypatch, **flags):
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(**{"action": "draft", "out": None,
                                 "session": None, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_issue(args)
    return code, out.getvalue()


@pytest.fixture
def reporting(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    # OFF, and said out loud. The record ships on, so a report written with it
    # off is now the deliberate case rather than the default one — and it is
    # still the case the header has to carry on its own.
    cfg.set_diagnostics(False)
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    yield profile
    diag._root = None
    cfg._CACHE.clear()


def test_a_report_written_with_the_log_off_still_carries_the_header(
        reporting, monkeypatch):
    """Somebody who has just hit a fault has not had this on for the week
    before it. Telling them to turn it on is right; giving them nothing in the
    meantime is not."""
    code, out = _draft(reporting, monkeypatch)
    assert code == 0
    path = [w for w in out.split() if w.endswith(".md")][0]
    body = open(path).read()
    assert "## Versions" in body and "- collab `" in body
    assert "diagnostic log is **off**" in body
    assert "collab config diagnostics on" in body
    assert "the diagnostic log is off" in out


def test_the_report_never_posts_anything_and_says_so(reporting, monkeypatch):
    code, out = _draft(reporting, monkeypatch)
    assert code == 0
    assert "gh issue create --repo rperez93/collab-a2a" in out
    assert "nothing has been sent anywhere" in out


def test_the_report_names_the_wake_recipe_and_never_its_target(
        reporting, monkeypatch):
    """`tmux` is a fact about the installation. `%3` is a fact about somebody's
    terminal, and this file is written to be posted in public."""
    from collab import wake
    from collab.client.daemon_files import DaemonPaths

    root = DaemonPaths(reporting.dir).root
    wake.write_config(root, wake.WakeConfig(command=wake.recipe("tmux").command(
        target="%3", pid="900", running="claude", collab="/usr/bin/collab")))
    code, out = _draft(reporting, monkeypatch)
    body = open([w for w in out.split() if w.endswith(".md")][0]).read()
    assert "armed, tmux recipe" in body
    assert "%3" not in body and "900" not in body


def test_the_report_carries_the_counts_and_the_tail_of_the_log(
        reporting, monkeypatch, tmp_path):
    from collab.client.daemon_files import DaemonPaths

    cfg.set_diagnostics(True)
    diag.begin(DaemonPaths(reporting.dir).root, "daemon")
    for _ in range(5):
        diag.log("wake_attempt", outcome="exit-1", why="3 in a row")
    diag.log("reconnected", after_failures=2)
    diag.flush()                # the draft reads the disk; see the test below

    out_path = tmp_path / "report.md"
    code, out = _draft(reporting, monkeypatch, out=str(out_path))
    assert code == 0
    body = out_path.read_text()
    assert "| `wake_attempt` | 5 |" in body
    assert "| `reconnected` | 1 |" in body
    assert '"outcome": "exit-1"' in body, "the tail itself, not only the counts"


def test_the_tail_is_bounded(reporting, monkeypatch, tmp_path):
    from collab.client.daemon_files import DaemonPaths

    cfg.set_diagnostics(True)
    diag.begin(DaemonPaths(reporting.dir).root, "daemon")
    for i in range(cli.ISSUE_LINES + 50):
        diag.log("wake_attempt", n=i)
    # WRITING AND READING ARE TWO ACTS NOW. Without this the draft below reads
    # whatever happens to have reached the disk, which is a race the machine
    # wins on a quiet run and loses under load — measured 5 failures in 12 with
    # the CPU busy. `_rows` got this flush; this path was missed.
    diag.flush()
    out_path = tmp_path / "report.md"
    _draft(reporting, monkeypatch, out=str(out_path))
    body = out_path.read_text()
    assert f"### Last {cli.ISSUE_LINES} records" in body
    assert '"n": 49\n' not in body, "the oldest are the ones dropped"
    assert '"n": 249' in body


# --- the record is written off the caller's thread ------------------------------
#
# The daemon logs from inside its own event loop while it is holding the feed,
# so an `open`/`write`/`close` there is a filesystem stall the feed waits out.
# A 9p mount over /mnt/c is the case that made this worth changing.

def test_the_disk_is_touched_by_the_writer_and_not_by_the_caller(recording):
    """The property, stated as who is holding the pen."""
    wrote_on: list[str] = []
    real = diag._write

    def watched(batch, dropped=0):
        wrote_on.append(threading.current_thread().name)
        real(batch, dropped)

    diag._write = watched
    try:
        diag.log("wake_attempt", outcome="ok")
        diag.flush()
    finally:
        diag._write = real
    assert wrote_on and set(wrote_on) == {"collab-diagnostics"}


def test_a_flush_waits_for_a_batch_that_is_already_in_flight(recording):
    """The race a flag cannot see: taken off the queue and not yet on the disk.

    `flush` returning there is `flush` not flushing, and it runs on the
    shutdown path — so what it would lose is the `stop` record.
    """
    started, may_finish = threading.Event(), threading.Event()
    real = diag._write

    def slow(batch, dropped=0):
        started.set()
        may_finish.wait(5)
        real(batch, dropped)

    diag._write = slow
    try:
        diag.log("feed_dropped", why="TimeoutError")
        assert started.wait(5), "the writer never took the batch"
        finished = threading.Event()
        waiter = threading.Thread(target=lambda: (diag.flush(10), finished.set()))
        waiter.start()
        assert not finished.wait(0.3), "flush returned with the write in flight"
        may_finish.set()
        waiter.join(5)
        assert finished.is_set()
    finally:
        diag._write = real
        may_finish.set()
    assert [r["event"] for r in diag.records(diag._root)] == ["feed_dropped"]


def test_a_stop_is_on_the_disk_by_the_time_it_returns(recording):
    """It is the last thing a dying process writes, so its caller does wait."""
    diag.log("stop", failures=2)
    assert [r["event"] for r in diag.records(diag._root)] == ["stop"]


def test_an_ordinary_record_does_not_make_its_caller_wait(recording):
    """The waiting is bought for the one record whose process is leaving, and
    for nothing else.

    `error` went first and `crash` followed it, for the same reason. Both
    `_log_crash` call sites in the daemon are inside the heartbeat, on the event
    loop, and both are explicitly survivable — «the rest carries on». Waiting
    there blocked the loop for two seconds per exception, exactly when the disk
    was misbehaving. The paths that really are dying flush for themselves.
    """
    assert diag.URGENT == frozenset({"stop"})
    held = threading.Event()
    real = diag._write
    diag._write = lambda batch, dropped=0: (held.wait(5), real(batch, dropped))
    try:
        diag.log("error", where="feed", kind="TimeoutError")   # must not block
        diag.log("wake_attempt", outcome="ok")
    finally:
        held.set()
        diag.flush(5)
        diag._write = real
    assert {r["event"] for r in diag.records(diag._root)} == {"error", "wake_attempt"}


def test_a_survivable_crash_does_not_block_the_loop_it_happened_in(recording):
    """The measurement that moved `crash` out of the urgent set: 2.001 s of
    blocked event loop per exception the heartbeat was going to survive."""
    import time as clock

    held = threading.Event()
    real = diag._write
    diag._write = lambda batch, dropped=0: (held.wait(5), real(batch, dropped))
    try:
        began = clock.monotonic()
        diag.log("crash", where="heartbeat", kind="ValueError")
        waited = clock.monotonic() - began
    finally:
        held.set()
        diag.flush(5)
        diag._write = real
    assert waited < 0.5, f"the caller waited {waited:.1f}s on the disk"
    assert [r["event"] for r in diag.records(diag._root)] == ["crash"]


def test_a_flood_is_bounded_and_says_how_much_it_lost(recording):
    """A queue that grew while the disk was unwritable would turn the fault it
    was reporting into a larger one. What goes is counted."""
    held = threading.Event()
    real = diag._write

    def blocked(batch, dropped=0):
        held.wait(5)
        real(batch, dropped)

    diag._write = blocked
    try:
        for i in range(diag.BUFFER_MAX + 200):
            diag.log("wake_attempt", n=i)
        held.set()
        diag.flush(10)
    finally:
        diag._write = real
        held.set()
    rows = diag.records(diag._root)
    lost = [r for r in rows if r["event"] == "dropped"]
    assert lost, "a gap in the record must say it is a gap"
    assert sum(r["records"] for r in lost) + len(rows) - len(lost) \
        == diag.BUFFER_MAX + 200


def test_every_record_says_which_process_wrote_it(recording):
    """Two processes share this file, and `proc` is a role rather than a pid."""
    diag.log("start")
    diag.flush()
    row = diag.records(diag._root)[0]
    assert row["pid"] == os.getpid() and row["proc"] == "daemon"


# --- what a failure leaves behind ----------------------------------------------

def test_a_recorded_exception_keeps_its_type_and_loses_its_words(recording):
    """An exception's own text is where the addresses are; httpx puts the
    tunnel URL in it. The traceback is what locates the bug."""
    try:
        raise TimeoutError("reading https://silly-name-1234.ngrok-free.app/feed")
    except TimeoutError as exc:
        diag.exception("feed", exc)
    diag.flush()
    row = diag.records(diag._root)[0]
    assert row["event"] == "error" and row["kind"] == "TimeoutError"
    assert row["where"] == "feed"
    assert "ngrok-free.app" not in json.dumps(row)
    assert "silly-name-1234" not in json.dumps(row)


def test_every_warning_collab_logs_is_counted_without_its_words(recording):
    """The half that answers «has this been happening all night».

    The message is deliberately not kept: this file is written to be pasted
    into a public issue, and a formatted log line is content. What identifies
    the warning instead is where it was raised.
    """
    logger = logging.getLogger("collab.client.daemon")
    handler = diag.Handler(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        logger.warning("feed dropped (%s); retrying", "https://tunnel.example/x")
        try:
            raise ValueError("/home/someone/secret/path")
        except ValueError:
            logger.exception("the wake failed")
    finally:
        logger.removeHandler(handler)
    diag.flush()
    rows = diag.records(diag._root)
    assert [r["event"] for r in rows] == ["warning", "error"]
    assert rows[0]["where"] == "client.daemon" and rows[0]["at"] > 0
    assert rows[1]["kind"] == "ValueError"
    body = json.dumps(rows)
    assert "tunnel.example" not in body and "retrying" not in body
    assert "secret" not in body


def test_a_warning_below_the_threshold_is_not_recorded(recording):
    logger = logging.getLogger("collab.client.daemon")
    handler = diag.Handler(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        logger.info("reminder handed to the monitor")
    finally:
        logger.removeHandler(handler)
    diag.flush()
    assert diag.records(diag._root) == []


def test_a_batch_is_cut_so_no_record_is_ever_written_in_two_pieces():
    """Two processes append to one day file, so a record split across two
    `write` calls is a record the other can land in the middle of.

    A single line was always one small write and safe by construction. A batch
    is up to `BUFFER_MAX` of them — measured around 100 KB — at which size a
    short write stops being unthinkable, and a short write is a torn record.
    """
    lines = [json.dumps({"n": i, "pad": "x" * 200}) for i in range(400)]
    chunks = diag._chunks(lines)
    assert len(chunks) > 1, "the fixture must be big enough to be cut"
    assert max(len(c) for c in chunks) <= diag.WRITE_CAP
    assert all(c.endswith("\n") for c in chunks)
    assert "".join(chunks) == "".join(line + "\n" for line in lines)


def test_a_record_longer_than_the_cap_is_written_whole_anyway():
    """Cutting a record up to obey a size limit is the harm the limit is for.
    `MAX_FIELD` and `MAX_FIELDS` mean it cannot arise; this says what happens.
    """
    assert diag._chunks(["y" * (diag.WRITE_CAP * 3)]) == \
        ["y" * (diag.WRITE_CAP * 3) + "\n"]


def test_two_writers_never_leave_a_half_line_in_the_file(recording):
    """The property those chunks exist for, exercised against the real file."""
    import threading as t

    def hammer(tag):
        for i in range(200):
            diag.log("wake_attempt", who=tag, n=i)

    threads = [t.Thread(target=hammer, args=(f"w{n}",)) for n in range(4)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()
    diag.flush()
    body = diag.path_for(recording).read_text()
    assert body.endswith("\n")
    for line in body.splitlines():
        json.loads(line)            # every line is a whole record, or this raises
