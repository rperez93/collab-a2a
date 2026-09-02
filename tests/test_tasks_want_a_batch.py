"""A task proposed with no batch open is work the shared figure cannot see.

The hub deliberately leaves such a task out of whichever batch is opened next —
a set nobody agreed on is not a set — so the work happens and the progress bar
every agent steers by does not move. The rules make opening the batch first the
host's duty; the CLI says so at the one moment it can still be fixed for the
task in hand.
"""

from __future__ import annotations

import types

import pytest

from collab import cli
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="me",
                       host_name="host", token="t", home=str(home),
                       participant_id="p_me")
    p.save()
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: p))
    return p


class _Hub:
    """The hub as `collab task` sees it, answering with the batch it chose."""

    def __init__(self, batch):
        self.batch = batch

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def task_action(self, action, *, task_id=None, title="", detail="",
                    room=None):
        return {"id": task_id or "T_1", "title": title or "a task",
                "state": "TASK_STATE_SUBMITTED", "owner": None,
                "batch": self.batch}

    def report_activity(self, payload):
        return payload


def _args(action, **kw):
    kw.setdefault("id", None)
    kw.setdefault("title", "a task")
    kw.setdefault("detail", None)
    kw.setdefault("room", None)
    kw.setdefault("session", None)
    kw.setdefault("json", False)
    kw.setdefault("open", False)
    kw.setdefault("files", None)       # a claim also announces what you hold
    return types.SimpleNamespace(action=action, **kw)


def test_a_proposal_into_no_batch_is_warned_about(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda p: _Hub(batch=None))
    assert cli.cmd_task(_args("propose")) == 0
    out = capsys.readouterr().out
    assert "no batch is open" in out
    assert "collab batch start" in out, "the fix is named, not implied"


def test_a_proposal_into_an_open_batch_is_not(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda p: _Hub(batch="B_1"))
    assert cli.cmd_task(_args("propose")) == 0
    assert "no batch" not in capsys.readouterr().out


def test_only_a_proposal_is_told(profile, monkeypatch, capsys):
    """A claim or a completion cannot change which batch a task is in, so the
    reminder there would be noise on every action."""
    monkeypatch.setattr(cli, "_client", lambda p: _Hub(batch=None))
    assert cli.cmd_task(_args("claim", id="T_1")) == 0
    assert "no batch" not in capsys.readouterr().out


def test_the_warning_does_not_change_the_exit_code(profile, monkeypatch):
    """The task WAS proposed; a reminder is not a failure."""
    monkeypatch.setattr(cli, "_client", lambda p: _Hub(batch=None))
    assert cli.cmd_task(_args("propose")) == 0
