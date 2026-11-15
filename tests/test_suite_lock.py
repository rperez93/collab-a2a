"""A full run waits for another full run on the same machine; see suite_lock."""
import os
import subprocess
import sys
import textwrap

import suite_lock


def test_a_second_holder_waits_and_is_told_who_holds_it(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_TEST_LOCK", raising=False)
    path = tmp_path / "suite.lock"
    first = suite_lock.acquire(path)
    assert first is not None and path.read_text() == str(os.getpid())
    try:
        # flock is per open file, so a second open in this process conflicts too.
        assert suite_lock.acquire(path, blocking=False) is None
        told = []
        waiter = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys; sys.path.insert(0, {os.path.dirname(suite_lock.__file__)!r})
            import suite_lock, pathlib
            h = suite_lock.acquire(pathlib.Path({str(path)!r}), on_wait=lambda who: print('WAIT', who, flush=True))
            print('GOT', flush=True)
        """)], stdout=subprocess.PIPE, text=True)
        told.append(waiter.stdout.readline().strip())
        assert told == [f"WAIT {os.getpid()}"]
        assert waiter.poll() is None, "it waits while the first run holds the lock"
    finally:
        first.close()
    assert waiter.stdout.readline().strip() == "GOT"
    assert waiter.wait(10) == 0


def test_off_never_locks_and_a_path_override_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_TEST_LOCK", "off")
    assert suite_lock.acquire(tmp_path / "x.lock") is None
    monkeypatch.setenv("COLLAB_TEST_LOCK", str(tmp_path / "mine.lock"))
    assert suite_lock.lock_path() == tmp_path / "mine.lock"
