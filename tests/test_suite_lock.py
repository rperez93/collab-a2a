"""A full run waits for another full run on the same machine; see suite_lock."""
import os
import select
import subprocess
import sys
import textwrap

import pytest

import suite_lock


def _line(proc, deadline=10.0):
    """One line of the child's output, or fail after the deadline (never hang)."""
    ready, _, _ = select.select([proc.stdout], [], [], deadline)
    assert ready, "the child said nothing before the deadline"
    return proc.stdout.readline().strip()


@pytest.mark.skipif(sys.platform == "win32", reason="no fcntl: the lock is off on Windows")
def test_a_second_holder_waits_and_is_told_who_holds_it(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_TEST_LOCK", raising=False)
    path = tmp_path / "suite.lock"
    first = suite_lock.acquire(path)
    assert first is not None and path.read_text() == str(os.getpid())
    waiter = None
    try:
        # flock is per open file, so a second open in this process conflicts too.
        assert suite_lock.acquire(path, blocking=False) is None
        waiter = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys; sys.path.insert(0, {os.path.dirname(suite_lock.__file__)!r})
            import suite_lock, pathlib
            h = suite_lock.acquire(pathlib.Path({str(path)!r}), on_wait=lambda who: print('WAIT', who, flush=True))
            print('GOT', flush=True)
        """)], stdout=subprocess.PIPE, text=True)
        assert _line(waiter) == f"WAIT {os.getpid()}"
        # The lock must actually hold it back: nothing more for half a second.
        held_back, _, _ = select.select([waiter.stdout], [], [], 0.5)
        assert not held_back and waiter.poll() is None, "it got the lock while the first run held it"
        first.close()
        assert _line(waiter) == "GOT"
        assert waiter.wait(10) == 0
    finally:
        first.close()
        if waiter is not None and waiter.poll() is None:
            waiter.kill()
            waiter.wait(10)


def test_the_windows_fallback_takes_no_lock(tmp_path, monkeypatch):
    """Without fcntl the lock is off, not an error."""
    monkeypatch.delenv("COLLAB_TEST_LOCK", raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl now raises ImportError
    assert suite_lock.acquire(tmp_path / "x.lock") is None


def test_off_never_locks_and_a_path_override_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_TEST_LOCK", "off")
    assert suite_lock.acquire(tmp_path / "x.lock") is None
    monkeypatch.setenv("COLLAB_TEST_LOCK", str(tmp_path / "mine.lock"))
    assert suite_lock.lock_path() == tmp_path / "mine.lock"


def test_two_pytest_sessions_in_one_process_each_take_and_release_the_lock(tmp_path):
    """The lock belongs to a pytest session, not the process: a second
    pytest.main in the same process used to wait forever on its own lock."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "conftest.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {os.path.dirname(suite_lock.__file__)!r})\n"
        "from suite_lock import pytest_collection_finish, pytest_unconfigure\n")
    (project / "test_x.py").write_text("def test_one():\n    pass\n\ndef test_two():\n    pass\n")
    script = textwrap.dedent(f"""
        import sys, pytest
        codes = [pytest.main([{str(project)!r}, '-q', '-p', 'no:cacheprovider']) for _ in range(2)]
        import suite_lock
        print('CODES', [int(c) for c in codes], 'HELD', suite_lock._held, flush=True)
    """)
    lock = tmp_path / "suite.lock"
    env = dict(os.environ, COLLAB_TEST_LOCK=str(lock), COLLAB_TEST_LOCK_MIN="1")
    done = subprocess.run([sys.executable, "-c", script], cwd=project, env=env,
                          capture_output=True, text=True, timeout=60)
    assert "CODES [0, 0] HELD None" in done.stdout, done.stdout + done.stderr
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return  # no lock on this platform: both sessions finishing is the whole check
    assert lock.read_text().strip().isdigit(), "each session took the lock and stamped its pid"


@pytest.mark.parametrize("count,collect_only,full", [
    (999, False, False), (1000, False, True), (5000, True, False)])
def test_only_a_full_run_that_runs_tests_takes_the_lock(count, collect_only, full, monkeypatch):
    monkeypatch.setattr(suite_lock, "FULL_RUN", 1000)
    assert suite_lock.is_full_run(count, collect_only) is full


def test_an_unopenable_lock_file_lets_the_run_go_ahead(tmp_path, monkeypatch):
    """Another account can own the path on a shared machine: no lock, no error."""
    monkeypatch.delenv("COLLAB_TEST_LOCK", raising=False)
    assert suite_lock.acquire(tmp_path / "missing-dir" / "x.lock") is None


@pytest.mark.skipif(sys.platform == "win32", reason="no fcntl: the lock is off on Windows")
def test_the_wait_has_a_ceiling(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_TEST_LOCK", raising=False)
    path = tmp_path / "suite.lock"
    first = suite_lock.acquire(path)
    try:
        told = []
        assert suite_lock.acquire(path, on_wait=told.append, max_wait=0.5) is None
        assert told == [str(os.getpid())]
    finally:
        first.close()

