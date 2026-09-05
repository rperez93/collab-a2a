"""Stopping a session is not leaving it, and two things prove it.

A wake is a command stored on disk, and the daemon is the only thing that runs
it. One armed against a session nothing is serving fires nothing and looks like
nothing — until somebody resumes that session weeks later and their agent is
woken by a batch of messages from a conversation they had forgotten. A followed
stream is worse in the other direction: it is a process somebody started, it
goes on holding a terminal after the session behind it is gone, and its reader
goes on believing they are being told things.

Neither is removed by `collab kill`, and until now neither said so. Stopping
what collab started is easy and it is not the whole job; the rest of the job is
the things collab was told to point AT this session, and the only honest thing
to do about them is name them and say what removes each.

`--disarm` does the one of the two that is collab's to do. A reader is a
process belonging to whatever armed it, so it is named and never signalled.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time
from pathlib import Path

import pytest

from collab import cli, wake
from collab.client import daemon as d
from collab.config import SessionProfile

SKILLS = Path(__file__).resolve().parent.parent / "src" / "collab" / "skills"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="bob",
                           host_name="alice", token="t", home=str(home))
    saved.save()
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: saved))
    return saved


def _arm(profile):
    wake.write_config(d.DaemonPaths(profile.dir).root,
                      wake.WakeConfig(command=["true"]))


def _watcher(profile, pid=None):
    """A watcher record for a process that is genuinely running: our own."""
    from collab.client import daemon_files as df

    where = df.watchers_dir(profile)
    where.mkdir(parents=True, exist_ok=True)
    mine = pid or os.getpid()
    (where / str(mine)).write_text(df._started_at(mine))


# --- what is still pointed at a stopped session ------------------------------------

def test_a_wake_armed_on_disk_is_named(profile):
    assert cli._still_armed(profile) == []
    _arm(profile)
    left = cli._still_armed(profile)
    assert len(left) == 1
    what, how = left[0]
    assert "wake is still armed" in what
    assert "wake off" in how


def test_a_reader_still_following_is_named_with_its_pid(profile):
    """It is a process somebody started, so the answer names it rather than
    claiming to have dealt with it."""
    _watcher(profile)
    what, how = cli._still_armed(profile)[0]
    assert str(os.getpid()) in what
    assert "listen --follow" in how


def test_a_dead_reader_is_not_named(profile):
    """The records are pruned by being read, so what is left is a process that
    is genuinely running. Naming a dead one would send somebody hunting."""
    from collab.client import daemon_files as df

    where = df.watchers_dir(profile)
    where.mkdir(parents=True, exist_ok=True)
    (where / "999999").write_text("nonsense")
    assert cli._still_armed(profile) == []


# --- what a stop says about them -----------------------------------------------------

def _say(profile, disarm=False):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        cli._say_what_is_left(profile, disarm)
    return out.getvalue()


def test_a_clean_stop_says_nothing_extra(profile):
    """Silence is the right output when there is nothing left behind."""
    assert _say(profile) == ""


def test_a_stop_names_what_it_did_not_take_and_how_to_take_it(profile):
    _arm(profile)
    _watcher(profile)
    said = _say(profile)
    assert "this session is stopped, and these are not" in said
    assert "wake off" in said and "listen --follow" in said
    assert "--disarm" in said, "and how to have collab do the half that is its"


def test_disarm_turns_the_wake_off(profile):
    _arm(profile)
    said = _say(profile, disarm=True)
    assert "wake disarmed" in said
    assert not wake.read_config(d.DaemonPaths(profile.dir).root).enabled


def test_disarm_names_the_reader_and_does_not_touch_it(profile):
    """A reader belongs to whatever armed it. Signalling somebody else's
    process because they stopped a session is not collab's to do."""
    _watcher(profile)
    said = _say(profile, disarm=True)
    assert str(os.getpid()) in said
    assert os.kill(os.getpid(), 0) is None, "we are still here"


# --- and what the loop says about it ---------------------------------------------------

def test_check_flags_a_wake_armed_on_a_session_with_no_listener(profile, monkeypatch):
    """It fires nothing and looks like nothing, until somebody resumes the
    session and their agent is woken by a conversation they had forgotten."""
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "stopped", "heartbeat": time.time() - 9999}))
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    _arm(profile)
    found = {r["check"]: r for r in cli._checks(profile)}
    assert found["listener"]["verdict"] == cli.CHECK_FAIL
    assert found["wake"]["verdict"] == cli.CHECK_WARN
    assert "no listener" in found["wake"]["detail"]
    assert "wake off" in found["wake"]["fix"]


def test_check_says_nothing_about_a_wake_on_a_live_session(profile, monkeypatch):
    """That is the wake doing its job, and the loop is silent when nothing is
    wrong — a line per run is how a check stops being read."""
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread_messages": 0,
         "wake": {"armed": True, "last_wake": time.time()}}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    _arm(profile)
    found = {r["check"]: r for r in cli._checks(profile)}
    assert found["wake"]["verdict"] != cli.CHECK_WARN


# --- and what the skills say ------------------------------------------------------------

@pytest.mark.parametrize("skill", ["collab-host", "collab-join", "collab-activity"])
def test_the_skill_says_how_to_leave(skill):
    """«Close the session» is a thing a user asks for, and an agent that does
    only the obvious half leaves a wake armed and a monitor running."""
    text = (SKILLS / skill / "SKILL.md").read_text()
    assert "## Closing the session, or leaving it" in text, f"{skill}"
    section = text.split("## Closing the session, or leaving it", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "collab idle" in section, "say you have stopped"
    assert "collab wake off" in section, "disarm the wake"
    assert "listen --follow" in section, "and stop the stream"
    assert "collab kill" in section, "then the stop command itself"
    assert "collab check" in section, "and confirm nothing is still armed"


@pytest.mark.parametrize("skill", ["collab-host", "collab-join"])
def test_the_skill_says_which_of_the_two_leaving_means(skill):
    """A guest disconnecting leaves the session running for everybody else; a
    host closing ends it for all of them. The commands are nearly the same and
    the consequences are not."""
    text = (SKILLS / skill / "SKILL.md").read_text()
    section = text.split("## Closing the session, or leaving it", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "guest" in section and "host" in section
    assert "keeps running" in section or "for everybody" in section
