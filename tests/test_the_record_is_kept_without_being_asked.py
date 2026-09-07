"""The diagnostic record ships on, and the plain logs stop growing without limit.

Two files are called logs here and they are not the same thing.

`diagnostics/YYYY-MM-DD.jsonl` is the structured record: events, never content,
seven day-files, swept at every start and once a day after. It was off by
default, and the argument was that a log nobody asked for grows on somebody's
disk to answer a question they may never ask. Neither half of that survives
contact with what it actually is — it is bounded, and there is nothing in it to
be careful with — while being off cost the one thing it exists for: a fault is
reported after it happens, and a record you have to switch on first never
covers the occurrence that made anybody look.

`daemon.log` and `hub.log` are the processes' own stdout. Those were always on
and were the ones actually growing: httpx logs a line per request at INFO, and
the daemon makes one about every three seconds for the life of the session, so
the file filled with a running commentary on its own successful polling and the
warnings worth reading scrolled away inside it.

So: the record on, the commentary quieted, and both files rolled aside when
they get large. These hold all three, and hold that turning the record off is
still one setting.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
from logging import handlers

from collab import config as cfg

ROOT = pathlib.Path(__file__).resolve().parent.parent
from collab import diagnostics as diag
from collab.client import daemon_files


# --- the record ----------------------------------------------------------------

def test_the_record_is_kept_unless_somebody_says_otherwise(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    cfg._CACHE.clear()
    assert cfg.DIAGNOSTICS_DEFAULT is True
    assert cfg.diagnostics_enabled() is True
    assert diag.enabled() is True


def test_turning_it_off_is_still_one_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    cfg._CACHE.clear()
    assert cfg.set_diagnostics(False) is False
    assert cfg.diagnostics_enabled() is False
    assert diag.enabled() is False
    assert cfg.set_diagnostics(True) is True


def test_it_is_still_bounded_to_a_week():
    """What makes it safe to leave on: the disk it costs has a ceiling."""
    assert diag.RETAIN_DAYS == 7


def test_the_setting_is_offered_in_the_listing():
    named = {setting.name: setting for setting in cfg.settings()}
    assert named["diagnostics"].default is True
    assert named["follow_agent"].default is True


# --- following the agent is a setting too --------------------------------------

def test_following_the_agent_is_on_and_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    cfg._CACHE.clear()
    assert cfg.follow_agent_enabled() is True
    assert cfg.set_follow_agent(False) is False
    assert cfg.follow_agent_enabled() is False


# --- the plain logs ------------------------------------------------------------

def test_the_http_commentary_is_quieted():
    """It is not what these files are for, and it was most of what was in them.

    The root level is put back afterwards: this configures a whole process, and
    a test that leaves it configured has changed every test after it.
    """
    root = logging.getLogger()
    was = root.level
    try:
        daemon_files.setup_logging()
        for name in daemon_files.NOISY:
            assert logging.getLogger(name).level == logging.WARNING
        assert logging.getLogger("collab.client.daemon").getEffectiveLevel() \
            <= logging.INFO
    finally:
        root.setLevel(was)


def test_a_large_log_is_rolled_aside_when_a_process_opens_it(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_text("x" * 200)
    with daemon_files.open_log(path, cap=100) as fh:
        fh.write("fresh\n")
    assert path.read_text() == "fresh\n"
    assert (tmp_path / "daemon.log.1").read_text() == "x" * 200


def test_a_small_log_is_simply_appended_to(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_text("kept\n")
    with daemon_files.open_log(path, cap=1_000) as fh:
        fh.write("more\n")
    assert path.read_text() == "kept\nmore\n"
    assert not (tmp_path / "daemon.log.1").exists()


def test_only_one_generation_is_kept(tmp_path):
    path = tmp_path / "daemon.log"
    for round_ in ("first", "second"):
        path.write_text(round_ * 100)
        daemon_files.open_log(path, cap=10).close()
    assert (tmp_path / "daemon.log.1").read_text() == "second" * 100


def test_a_missing_log_is_created_rather_than_refused(tmp_path):
    path = tmp_path / "made" / "up" / "daemon.log"
    with daemon_files.open_log(path) as fh:
        fh.write("hello\n")
    assert path.read_text() == "hello\n"


# --- nothing writes on the caller's thread --------------------------------------

def test_the_plain_log_is_written_by_a_thread_and_not_by_the_caller(tmp_path):
    """A `logger.warning` inside the daemon's event loop was an `open` and a
    `write` on the repository's filesystem. One that pauses paused the feed."""
    root = logging.getLogger()
    was, level = list(root.handlers), root.level
    for handler in was:
        root.removeHandler(handler)
    path = tmp_path / "daemon.log"
    wrote_on: list[str] = []

    class Watched(logging.FileHandler):
        def emit(self, record):
            wrote_on.append(threading.current_thread().name)
            super().emit(record)

    root.addHandler(Watched(path))
    try:
        daemon_files.setup_logging()
        assert any(isinstance(h, handlers.QueueHandler) for h in root.handlers)
        logging.getLogger("collab.client.daemon").warning("feed dropped")
        daemon_files._listener.stop()
        assert "feed dropped" in path.read_text()
        assert wrote_on and threading.main_thread().name not in wrote_on
    finally:
        daemon_files._listener = None
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in was:
            root.addHandler(handler)
        root.setLevel(level)


def test_the_log_queue_drops_its_oldest_rather_than_blocking():
    """Bounded, because a daemon sits for days; and never blocking, because the
    caller is the thing being diagnosed."""
    pipe = daemon_files._Bounded(2)
    for n in range(5):
        pipe.put_nowait(n)
    assert [pipe.get_nowait(), pipe.get_nowait()] == [3, 4]
    assert pipe.empty()


def test_the_queue_is_bounded_to_something_a_daemon_can_hold():
    assert 0 < daemon_files.LOG_QUEUE_MAX <= 5_000


def test_the_two_logs_divide_the_words_from_the_fact(tmp_path, monkeypatch):
    """The whole arrangement, exercised as a real process configures it.

    Both handlers see one record: the diagnostics one on the `collab` logger
    and the root's queue behind it. Order matters and is not incidental —
    `QueueHandler.prepare` formats the message and clears `exc_info`, so a
    diagnostics handler running after it would record every exception as having
    no type. `collab` is the more specific logger, so it always runs first.

    What each keeps is the point. The plain log holds the words, for a person
    reading `daemon.log`. The structured record holds that a `ValueError` was
    raised at a line, and not one word of what was said — because that file is
    written to be pasted into a public issue.
    """
    from collab import diagnostics as diag

    root = logging.getLogger()
    was, level = list(root.handlers), root.level
    for handler in was:
        root.removeHandler(handler)
    plain = tmp_path / "daemon.log"
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    cfg._CACHE.clear()
    diag.begin(tmp_path / "state", "daemon")
    try:
        root.addHandler(logging.FileHandler(plain))
        daemon_files.setup_logging()
        try:
            raise ValueError("reading https://silly-name.ngrok-free.app/feed")
        except ValueError:
            logging.getLogger("collab.client.daemon").exception("the wake failed")
        daemon_files._listener.stop()
        diag.flush()

        rows = diag.records(tmp_path / "state")
        assert [(r["event"], r["kind"]) for r in rows] == [("error", "ValueError")]
        assert rows[0]["where"] == "client.daemon"
        assert "ngrok" not in str(rows) and "wake failed" not in str(rows)
        assert "the wake failed" in plain.read_text()
        assert "ngrok-free.app" in plain.read_text()
    finally:
        daemon_files._listener = None
        diag._root = None
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in was:
            root.addHandler(handler)
        root.setLevel(level)
        logging.getLogger("collab").handlers.clear()


# --- what the writing thread must not lose --------------------------------------

def _in_a_fresh_interpreter(body: str, tmp_path) -> str:
    """Run `body` in its own process, with collab importable and a clean slate.

    THE WRITER IS MODULE STATE, shared by every test in this file — a thread,
    two counters and a queue. A test that reaches into it to stage a failure
    fights the other tests rather than the code, and the state it leaves behind
    follows whatever runs next. A fresh interpreter has exactly one writer, in
    exactly the condition the test puts it in.
    """
    script = tmp_path / "probe.py"
    script.write_text(body)
    done = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, timeout=60,
                          env={**os.environ,
                               "PYTHONPATH": str(ROOT / "src"),
                               "COLLAB_CONFIG": str(tmp_path / "config.json")})
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


def test_a_flush_restarts_a_writer_that_has_died(tmp_path):
    """The writer is started from `_enqueue` and nowhere else, so a thread that
    died after the last record was queued was never replaced — and `flush`
    returned at once, stranding everything still in memory. On the `atexit`
    path that is the whole tail of the record.
    """
    root = str(tmp_path / "state")
    said = _in_a_fresh_interpreter(
        "import pathlib\n"
        "from collab import diagnostics as diag\n"
        f"root = pathlib.Path({root!r})\n"
        "diag.begin(root, 'daemon')\n"
        "for i in range(5):\n"
        "    diag.log('wake_attempt', n=i)\n"
        "diag._writer = None            # as if the thread had died\n"
        "diag.flush(5)\n"
        "print(len(diag.records(root)))\n", tmp_path)
    assert said == "5", f"only {said} of 5 records reached the disk"


def test_a_record_with_exc_info_but_no_exception_is_still_kept(tmp_path):
    """`logger.error(..., exc_info=True)` outside an except block yields
    `(None, None, None)`, which is truthy. Reading `.__name__` on it raised, the
    blanket except swallowed the error, and the WHOLE record was lost rather
    than one field of it.
    """
    root = str(tmp_path / "state")
    said = _in_a_fresh_interpreter(
        "import json, logging, pathlib\n"
        "from collab import diagnostics as diag\n"
        f"root = pathlib.Path({root!r})\n"
        "diag.begin(root, 'daemon')\n"
        "log = logging.getLogger('collab.client.daemon')\n"
        "log.addHandler(diag.Handler(level=logging.WARNING))\n"
        "log.error('nothing is being handled here', exc_info=True)\n"
        "diag.flush(5)\n"
        "print(json.dumps([(r['event'], r['kind']) "
        "for r in diag.records(root)]))\n", tmp_path)
    assert json.loads(said) == [["error", ""]]


def test_every_write_is_one_syscall_of_whole_records(tmp_path, monkeypatch):
    """`_chunks` cuts at 8 KiB, and a buffered writer re-merged the pieces: the
    real syscall was twice the cap. The records still landed whole, by an
    accident of where the buffer broke rather than by the rule the cap states.
    """
    from collab import diagnostics as diag

    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    cfg._CACHE.clear()
    root = tmp_path / "state"
    diag.begin(root, "daemon")
    try:
        for i in range(400):
            diag.log("wake_attempt", n=i, pad="x" * 150)
        diag.flush(10)
        body = diag.path_for(root).read_text()
        assert body.endswith("\n")
        for line in body.splitlines():
            json.loads(line)
    finally:
        diag._root = None
