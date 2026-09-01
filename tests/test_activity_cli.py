"""The commands an agent actually types, and what they print.

`collab task show` shipped a NameError the first time it ran: it called a helper
that lives in the viewer, not in the CLI. Nothing exercised the printing, so
nothing caught it — the same shape as the command list that raised half way
down. Anything that formats for a reader is worth one test that reads it.
"""

from __future__ import annotations

import json
import time
import types

import pytest

from collab import activity, cli
from collab.config import SessionProfile


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="me", host_name="host",
                       token="t", home=str(home), participant_id="p_me")
    p.save()
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: p))
    return p


class FakeClient:
    """The hub, as far as these commands are concerned."""

    def __init__(self, people=(), tasks=()):
        self.people = list(people)
        self._tasks = list(tasks)
        self.reported = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def snapshot(self):
        return {"participants": self.people}

    def tasks(self, open_only=False):
        return self._tasks

    def report_activity(self, payload):
        self.reported.append(payload)
        return payload

    def task_action(self, action, *, task_id=None, title="", detail="", room=None):
        return {"id": task_id or "T_1", "title": title or "migrate sessions",
                "state": "TASK_STATE_WORKING", "owner": "me"}


def _args(**kw):
    kw.setdefault("session", None)
    kw.setdefault("json", False)
    return types.SimpleNamespace(**kw)


# --- saying it --------------------------------------------------------------

def test_working_records_it_locally_and_publishes(profile, monkeypatch, capsys):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda p: client)

    assert cli.cmd_working(_args(what=["the", "token", "refresh"],
                                 files=["src/api/auth.py"], task=None)) == 0

    assert client.reported[0]["what"] == "the token refresh"
    assert activity.read_local(profile)["files"] == ["src/api/auth.py"]
    assert "the token refresh" in capsys.readouterr().out


def test_a_hub_that_cannot_be_reached_does_not_lose_it(profile, monkeypatch, capsys):
    """The daemon carries it up on the next reconnect — but only if it was
    written down first, so it is written first and sent second."""
    def boom(_profile):
        raise cli.HubError("hub unreachable")

    monkeypatch.setattr(cli, "_client", boom)
    cli.cmd_working(_args(what=["the refresh"], files=[], task=None))

    assert activity.read_local(profile)["state"] == "working"
    assert "the listener will carry it up" in capsys.readouterr().out


def test_working_with_nothing_to_say_is_refused(profile, capsys):
    assert cli.cmd_working(_args(what=[], files=[], task=None)) == 1
    assert "say what you are working on" in capsys.readouterr().err


def test_idle_clears_what_you_were_on(profile, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda p: client)
    cli.cmd_working(_args(what=["the refresh"], files=["a.py"], task=None))

    cli.cmd_idle(_args(note=[]))

    assert client.reported[-1]["state"] == "idle"
    assert "files" not in client.reported[-1]


def test_an_idle_note_survives(profile, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda p: client)

    cli.cmd_idle(_args(note=["waiting", "on", "your", "review"]))

    assert client.reported[-1]["what"] == "waiting on your review"


# --- reading it -------------------------------------------------------------

WORKING = {"state": "working", "what": "the token refresh",
           "files": ["src/api/auth.py"], "since": time.time() - 700}


def test_activity_says_who_is_on_what(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda p: FakeClient(people=[
        {"name": "me", "connected": True, "activity": WORKING},
        {"name": "bob", "connected": True, "activity": {"state": "idle",
                                                        "since": time.time()}},
        {"name": "old", "connected": False, "last_seen": time.time() - 1200},
    ]))

    assert cli.cmd_activity(_args()) == 0
    out = capsys.readouterr().out
    assert "the token refresh" in out and "11m" in out
    assert "idle" in out
    assert "offline" in out and "20m" in out


def test_connected_but_silent_is_not_reported_as_idle(profile, monkeypatch, capsys):
    """Silence is a gap worth asking about; idle is an answer."""
    monkeypatch.setattr(cli, "_client", lambda p: FakeClient(people=[
        {"name": "bob", "connected": True, "activity": {}}]))

    cli.cmd_activity(_args())
    assert "has not said" in capsys.readouterr().out


def test_activity_json_is_for_a_program(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda p: FakeClient(people=[
        {"name": "bob", "connected": True, "activity": WORKING}]))

    cli.cmd_activity(_args(json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["activity"]["what"] == "the token refresh"


# --- the board --------------------------------------------------------------

TASK = {"id": "T_9d63", "title": "migrate sessions", "state": "TASK_STATE_SUBMITTED",
        "owner": None, "created_by": "alice", "room": "general",
        "updated_at": time.time() - 120, "detail": "move it behind the interface"}


def test_show_prints_the_whole_task(capsys):
    """`task list` is one line each; claiming from it is claiming a title."""
    assert cli._describe_task(TASK) == 0
    out = capsys.readouterr().out
    assert "migrate sessions" in out
    assert "move it behind the interface" in out, "the detail is the point of it"
    assert "unclaimed" in out
    assert "claim --id T_9d63" in out


def test_show_says_when_somebody_holds_it(capsys):
    cli._describe_task({**TASK, "owner": "bob", "state": "TASK_STATE_WORKING"})
    assert "bob has it" in capsys.readouterr().out


def test_show_says_when_it_is_finished(capsys):
    cli._describe_task({**TASK, "state": "TASK_STATE_COMPLETED"})
    assert "propose a new task" in capsys.readouterr().out


def test_claiming_a_task_also_says_you_are_working_on_it(profile, monkeypatch, capsys):
    """One act, not two: the bookkeeping nobody does twice is the one that
    stays true."""
    client = FakeClient(tasks=[TASK])
    monkeypatch.setattr(cli, "_client", lambda p: client)

    cli.cmd_task(_args(action="claim", id="T_9d63", title=None, detail=None,
                       files=["src/store.py"], room=None, open=False))

    published = client.reported[-1]
    assert published["state"] == "working"
    assert published["task"] == "T_9d63"
    assert published["files"] == ["src/store.py"]


def test_completing_it_sets_you_idle_again(profile, monkeypatch):
    client = FakeClient(tasks=[TASK])
    monkeypatch.setattr(cli, "_client", lambda p: client)
    cli.cmd_task(_args(action="claim", id="T_9d63", title=None, detail=None,
                       files=[], room=None, open=False))

    cli.cmd_task(_args(action="complete", id="T_9d63", title=None, detail=None,
                       files=[], room=None, open=False))

    assert client.reported[-1]["state"] == "idle"


def test_finishing_somebody_elses_task_leaves_your_own_activity_alone(
        profile, monkeypatch):
    """You were on something else; completing an unrelated task is not the end
    of what you are doing."""
    client = FakeClient(tasks=[TASK])
    monkeypatch.setattr(cli, "_client", lambda p: client)
    cli.cmd_working(_args(what=["the refresh"], files=[], task=None))

    cli.cmd_task(_args(action="complete", id="T_other", title=None, detail=None,
                       files=[], room=None, open=False))

    assert client.reported[-1]["state"] == "working"
