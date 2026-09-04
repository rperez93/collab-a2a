"""Two edges the reminder got wrong, and one of them is «said when nobody asked».

`collab wake show` warned that the reminder needs a wake even where nobody had
ever configured one — `remind_every` is ten by default, so gating the line on
the interval alone told every reader about a feature they had not asked for.
`collab check` gates the same sentence on whether a key was actually written,
and the two pages must agree.

And `write_prompt` derived its temporary file from the destination, which is
unique for a batch and FIXED for the reminder — so two writers shared one
temporary name, and the first to rename it left the second renaming a path that
no longer existed. Two daemons overlapping for one session across a restart is
the ordinary way to get two writers.
"""

from __future__ import annotations

import argparse
import concurrent.futures as _futures
import contextlib
import io

import pytest

from collab import cli, config, wake
from collab.config import SessionProfile


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A throwaway global config. Never the machine's own."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    config._CACHE.clear()
    yield
    config._CACHE.clear()


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


def _wake_show(profile, monkeypatch):
    monkeypatch.setattr(cli.SessionProfile, "current",
                        classmethod(lambda c: profile))
    args = argparse.Namespace(
        session=None, json=False, notify=None, settle=None, min_gap=None,
        timeout=None, run=[], agent=None, action="show", to=None, target=None,
        expect_command=None, expect_pid=None, yes=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_wake(args)
    return code, out.getvalue()


# --- said only when somebody asked -------------------------------------------------

def test_a_reminder_nobody_configured_is_not_mentioned(profile, monkeypatch):
    code, out = _wake_show(profile, monkeypatch)
    assert code == 0 and "disarmed" in out
    assert "reminder" not in out, out


@pytest.mark.parametrize("key,value", [
    ("remind_every", 15),
    ("remind_host", "keep going"),
    ("remind_guest", "keep going"),
])
def test_a_reminder_somebody_configured_is_mentioned(profile, monkeypatch,
                                                     key, value):
    config.setting(key).write(value)
    code, out = _wake_show(profile, monkeypatch)
    assert code == 0 and "disarmed" in out
    assert "reminder" in out, f"{key} was written and nothing said so: {out}"


# --- one temporary file per writer --------------------------------------------------

def test_two_writers_of_the_reminder_do_not_share_a_temporary_file(tmp_path):
    waker = wake.Waker(tmp_path, "s_x")
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for _ in range(120):
                waker.write_prompt(None, f"reminder {n}")
        except BaseException as exc:                      # noqa: BLE001
            errors.append(exc)

    with _futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(6)))
    assert not errors, f"{len(errors)} writers raised, first: {errors[0]!r}"
    assert "reminder " in waker.write_prompt(None, "reminder x").read_text()


def test_no_temporary_files_are_left_behind(tmp_path):
    waker = wake.Waker(tmp_path, "s_x")
    for _ in range(20):
        path = waker.write_prompt(None, "a reminder")
    strays = list(path.parent.glob("*.writing*"))
    assert not strays, strays
