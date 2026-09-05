"""The status line disappearing, and the four different things that caused it.

It is redrawn on every prompt, and it had four ways to draw nothing: no session
in this checkout, a session with no daemon behind it, a daemon whose status file
said nothing, and — through the bare `except` in `main` — anything at all that
raised. All four blanked the segment identically, so «my status line vanished»
was a question with four answers and no way to tell which.

Three of the four are momentary far more often than they are permanent.
`status.json` is replaced by an atomic rename, `is_running` asks a lock that a
restarting daemon holds for a fraction of a second, and a sandboxed process can
fail to read either. Every one of them blanked the whole segment for a redraw,
and a segment that vanishes and comes back reads as broken software rather than
as a file being written.

So the last line that WAS drawable stands in, for sixty seconds, with nothing
appended to it — it is the last thing that was true, and an «(stale)» would
make it a different claim. The fourth cause is not covered by that on purpose:
a profile that no longer exists is a session that has ENDED, and a status bar
still carrying it is the stale-badge problem this module refuses everywhere
else.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from collab.config import SessionProfile
from collab.statusline import render as r


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A live session with a daemon behind it, in a home of its own."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("NO_COLOR", "1")
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="alice",
                             host_name="alice", token="t", home=str(home),
                             is_host=True)
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps(
        {"session_id": "s", "name": "alice", "host": "alice", "is_host": True,
         "state": "live", "heartbeat": time.time(), "others_connected": 1}))
    monkeypatch.setattr(r, "is_running", lambda p: 4242)
    monkeypatch.setattr(r, "state_dir_label", lambda home, cwd=None: "")
    monkeypatch.setattr(r, "_update_available", lambda: False)
    return profile


# --- the four reasons, told apart --------------------------------------------

def test_a_drawn_line_has_no_reason_to_give(session):
    line, why = r.draw()
    assert "alice" in line and why == ""


def test_no_session_here_at_all_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "nothing"))
    line, why = r.draw()
    assert line == "" and why == r.NO_PROFILE


def test_a_session_with_no_daemon_is_named(session, monkeypatch):
    monkeypatch.setattr(r, "is_running", lambda p: None)
    assert r.reasoned()[1] == r.NO_DAEMON


def test_a_status_file_that_says_nothing_is_named(session):
    (session.dir / "status.json").write_text("")
    assert r.reasoned()[1] == r.NO_STATUS


def test_anything_that_raises_is_named_rather_than_swallowed(session, monkeypatch):
    def explode(profile):
        raise RuntimeError("the lock would not answer")

    monkeypatch.setattr(r, "is_running", explode)
    line, why = r.draw()
    assert line == "" and why == r.ERROR


# --- and the last line stands in for a moment --------------------------------

def _drawn_once(session):
    line, why = r.draw()
    assert why == "" and line
    return line


@pytest.mark.parametrize("break_it", ["no-daemon", "no-status", "error"])
def test_a_momentary_failure_keeps_the_last_line(break_it, session, monkeypatch):
    """Every one of these lasts a fraction of a second in the ordinary case:
    an atomic rename in flight, a lock a restarting daemon holds, a sandbox
    that could not read either."""
    kept = _drawn_once(session)
    if break_it == "no-daemon":
        monkeypatch.setattr(r, "is_running", lambda p: None)
    elif break_it == "no-status":
        (session.dir / "status.json").write_text("")
    else:
        def explode(profile):
            raise RuntimeError("nope")
        monkeypatch.setattr(r, "is_running", explode)

    line, why = r.draw()
    assert line == kept
    assert why == r.KEPT


def test_nothing_at_all_is_appended_to_a_kept_line(session, monkeypatch):
    """It is the last thing that was true. «(stale)» would make it a different
    claim, and a claim nobody asked this to make."""
    kept = _drawn_once(session)
    monkeypatch.setattr(r, "is_running", lambda p: None)
    line, _why = r.draw()
    assert line == kept, "it was annotated"


def test_the_kept_line_expires(session, monkeypatch):
    kept = _drawn_once(session)
    monkeypatch.setattr(r, "is_running", lambda p: None)
    just_inside = time.time() + r.KEEP_LAST_FOR - 1
    assert r.last_line(session, now=just_inside) == kept
    past = time.time() + r.KEEP_LAST_FOR + 1
    assert r.last_line(session, now=past) == ""


def test_past_the_window_the_line_blanks_with_its_real_reason(session, monkeypatch):
    _drawn_once(session)
    old = json.loads((session.dir / r.LAST_LINE_FILE).read_text())
    old["at"] = time.time() - r.KEEP_LAST_FOR - 5
    (session.dir / r.LAST_LINE_FILE).write_text(json.dumps(old))
    monkeypatch.setattr(r, "is_running", lambda p: None)
    line, why = r.draw()
    assert line == "" and why == r.NO_DAEMON


def test_a_dead_session_disappears_however_recent_the_last_line(
        session, tmp_path, monkeypatch):
    """A profile that no longer exists is a session that ENDED, not a file
    being written. Keeping its line is the stale badge this module refuses."""
    _drawn_once(session)
    monkeypatch.setattr(SessionProfile, "current",
                        classmethod(lambda cls, cwd=None: None))
    line, why = r.draw()
    assert line == "" and why == r.NO_PROFILE


def test_a_stamp_from_the_future_is_not_a_fresh_one(session):
    """The same answer the batch figures give a backward clock step, and for
    the same reason: an age that cannot be computed is not evidence of youth."""
    _drawn_once(session)
    kept = json.loads((session.dir / r.LAST_LINE_FILE).read_text())
    kept["at"] = time.time() + 3600
    (session.dir / r.LAST_LINE_FILE).write_text(json.dumps(kept))
    assert r.last_line(session) == ""


def test_a_coloured_line_is_never_handed_to_a_plain_caller(session, monkeypatch):
    """`--plain` is for a bar that cannot render escapes. Handing it a kept
    line full of them is the exact failure `--plain` exists to avoid, arriving
    only on the redraws where something else had already gone wrong."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    line, why = r.draw()
    assert why == "" and "\033[" in line, "the fixture's line has colour in it"
    assert r.last_line(session) == line, "a coloured reader gets it back"

    monkeypatch.setenv("NO_COLOR", "1")
    assert r.last_line(session) == "", "and a plain one does not"
    monkeypatch.setattr(r, "is_running", lambda p: None)
    assert r.draw() == ("", r.NO_DAEMON), "it blanks rather than painting escapes"


def test_an_unreadable_or_missing_keepsake_is_simply_no_keepsake(session, tmp_path):
    assert r.last_line(session) == ""
    (session.dir / r.LAST_LINE_FILE).write_text("{not json")
    assert r.last_line(session) == ""
    (session.dir / r.LAST_LINE_FILE).write_text('"a string"')
    assert r.last_line(session) == ""


def test_remembering_never_raises_over_a_directory_it_cannot_write(session):
    class Nowhere:
        dir = "/proc/nowhere/at/all"

    r.remember_line(Nowhere(), "a line")          # must not raise
    r.remember_line(None, "a line")


# --- and the keepsake is never caught half written -------------------------------

def test_the_keepsake_is_written_somewhere_else_and_renamed_into_place(
        session, monkeypatch):
    """The file a reader opens is only ever whole.

    This one is worth pinning rather than trusting, because the failure is
    invisible on the machine that writes it: the window is one buffer flush
    wide, and the reader who lands in it is somebody else's prompt on somebody
    else's redraw. What makes it worth closing at all is that the keepsake is
    read at exactly the moment something has already gone wrong — a truncated
    line is handed to the prompt as the guard against a missing one.
    """
    wrote: list[str] = []
    renamed: list[tuple[str, str]] = []
    real_write, real_replace = Path.write_text, Path.replace

    def watched_write(self, *a, **k):
        wrote.append(self.name)
        return real_write(self, *a, **k)

    def watched_replace(self, target):
        renamed.append((self.name, Path(target).name))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "write_text", watched_write)
    monkeypatch.setattr(Path, "replace", watched_replace)

    r.remember_line(session, "a line worth keeping")

    assert wrote == [f"statusline-last.{os.getpid()}.tmp"], wrote
    assert renamed == [(f"statusline-last.{os.getpid()}.tmp", r.LAST_LINE_FILE)]
    assert r.last_line(session) == "a line worth keeping"


def test_two_prompts_writing_at_once_do_not_share_a_temporary(session):
    """A fixed temporary name would let two of them interleave into one file
    and rename the mixture into place. The daemon's `status.json` has one
    writer and needs no such thing; this file has as many writers as the user
    has prompts open on the session."""
    seen = set()
    real = Path.write_text

    def note(self, *a, **k):
        seen.add(self.name)
        return real(self, *a, **k)

    with mock.patch.object(Path, "write_text", note):
        with mock.patch("os.getpid", lambda: 111):
            r.remember_line(session, "from one shell")
        with mock.patch("os.getpid", lambda: 222):
            r.remember_line(session, "from another")

    assert seen == {"statusline-last.111.tmp", "statusline-last.222.tmp"}
    assert r.last_line(session) == "from another"


def test_a_write_that_fails_leaves_the_previous_keepsake_and_no_litter(
        session, monkeypatch):
    """Interrupted mid-write, the reader still gets the last whole line. And
    the scratch file goes: this runs on every redraw, so a directory filling
    with temporaries is a worse failure than the keepsake it came from."""
    r.remember_line(session, "the line before")
    real = Path.write_text

    def fails(self, *a, **k):
        real(self, "{half of a js")          # a genuine partial write
        raise OSError("the disk went away")

    monkeypatch.setattr(Path, "write_text", fails)
    r.remember_line(session, "the line that never lands")   # must not raise

    assert r.last_line(session) == "the line before"
    assert list(Path(session.dir).glob("*.tmp")) == []


# --- what --json says about all of it ------------------------------------------

def _json(monkeypatch, *argv):
    written = []
    monkeypatch.setattr(r.sys.stdout, "write", written.append)
    assert r.main(["--json", "--plain", *argv]) == 0
    return json.loads(written[0])


def test_the_json_says_why_when_a_line_was_drawn(session, monkeypatch):
    assert _json(monkeypatch)["why"] == ""


def test_the_json_names_the_blanking_cause(session, monkeypatch):
    monkeypatch.setattr(r, "is_running", lambda p: None)
    assert _json(monkeypatch)["why"] == r.NO_DAEMON


def test_the_json_says_when_the_reader_is_seeing_a_kept_line(session, monkeypatch):
    """A host formatting its own line has to be told that what it is looking at
    is the last one rather than the current one."""
    _json(monkeypatch)                              # draws it, and keeps it
    monkeypatch.setattr(r, "is_running", lambda p: None)
    assert _json(monkeypatch)["why"] == r.KEPT


def test_the_json_says_no_profile_where_there_is_no_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "nothing"))
    payload = _json(monkeypatch)
    assert payload["why"] == r.NO_PROFILE
    assert payload["active"] is False


# --- the wait on the payload -----------------------------------------------------

def test_it_waits_a_second_for_the_payload_rather_than_a_sixth_of_one():
    """Nothing pays for the wait where nothing is piped: a terminal
    short-circuits on `isatty` and a closed stdin on `closed`, both before it.
    What was paying was the host that DOES pipe — a payload a fraction late
    read as no payload at all, losing that prompt's usage figures and whichever
    working directory the payload named."""
    import inspect

    default = inspect.signature(r._read_stdin_if_ready).parameters["timeout"].default
    assert default >= 1.0


def test_a_terminal_on_stdin_is_never_waited_on(monkeypatch):
    class Tty:
        closed = False

        def isatty(self):
            return True

        def read(self):
            raise AssertionError("it read from a terminal")

    monkeypatch.setattr(r.sys, "stdin", Tty())

    def never(*a, **kw):
        raise AssertionError("it waited on a terminal")

    monkeypatch.setattr(r.select, "select", never)
    assert r._read_stdin_if_ready() == ""
