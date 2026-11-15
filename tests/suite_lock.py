"""One full test run per machine at a time.

Two full suites on one machine (two agents, one box) slow each other to
nearly double their time, and the timing tests then fail in whichever run
loses: every timing failure at the v2.2.0 gate came from that, and each test
passed when run alone. A full run therefore takes this lock and waits for
another full run to finish; a focused run (fewer tests than FULL_RUN) never
waits.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: Collected tests at or above which a run counts as a full suite.
FULL_RUN = int(os.environ.get("COLLAB_TEST_LOCK_MIN", "1000"))

#: The handle this pytest session holds, released when the session ends.
_held = None


def lock_path() -> Path:
    """Per machine and account, outside any home a test isolates."""
    override = os.environ.get("COLLAB_TEST_LOCK", "")
    if override and override != "off":
        return Path(override)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"collab-full-suite-{uid}.lock"


def acquire(path: Path, on_wait=None, blocking: bool = True):
    """Hold the lock for as long as the returned file stays open. Returns
    None when the lock is off, unsupported here, or (not blocking) taken.

    `on_wait(holder)` is called once, with the holder's pid text, before
    blocking on a lock someone else holds.
    """
    if os.environ.get("COLLAB_TEST_LOCK") == "off":
        return None
    try:
        import fcntl
    except ImportError:  # shortcut: no lock on Windows, add msvcrt.locking if runs overlap there
        return None
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not blocking:
            handle.close()
            return None
        if on_wait is not None:
            handle.seek(0)
            on_wait(handle.read(32).strip() or "unknown")  # a pid; never trust the file's size
        fcntl.flock(handle, fcntl.LOCK_EX)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def pytest_collection_finish(session):
    """A full run waits for any other full run on this machine."""
    global _held
    if _held is not None or len(session.items) < FULL_RUN or session.config.option.collectonly:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    def say(holder):
        if reporter is not None:
            reporter.write_line(f"waiting for another full test run on this machine "
                                f"(pid {holder}) to finish; COLLAB_TEST_LOCK=off skips the wait")
            reporter.flush()  # said now, not when the wait is over
    _held = acquire(lock_path(), on_wait=say)


def pytest_unconfigure(config):
    """Released when the session ends, however it ends (pass, fail or Ctrl-C):
    a second pytest.main in the same process opened the file again and waited
    on its own process's lock forever."""
    global _held
    handle, _held = _held, None
    if handle is not None:
        try:
            handle.close()  # closing the descriptor drops the flock
        except OSError:
            pass

