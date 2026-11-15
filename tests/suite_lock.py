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
FULL_RUN = 1000


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
            on_wait(handle.read().strip() or "unknown")
        fcntl.flock(handle, fcntl.LOCK_EX)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle
