"""The panel status follows the board without anyone running `collab working`
(T_fec283bc8922): the hub moves it when the board moves, whoever moved it, and
the roster never shows a status about work that is over or someone else's."""

from __future__ import annotations

import time

from collab import activity

from test_activity_hub import _headers, _join, _task, _who


def _status(client, headers, name):
    return _who(client, headers, name)["activity"]


def test_claiming_sets_the_status_and_completing_returns_it_to_idle(client, session, host_headers):
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="migrate sessions").json()["task"]
    assert _task(client, _headers(bob), "claim", id=task["id"]).status_code == 200
    shown = _status(client, host_headers, "bob")
    assert (shown["state"], shown["task"], shown["what"]) == ("working", task["id"], "migrate sessions")
    _task(client, _headers(bob), "complete", id=task["id"])
    assert _status(client, host_headers, "bob")["state"] == "idle"


def test_a_status_naming_another_task_is_not_touched_by_completing(client, session, host_headers):
    bob = _join(client, session)
    one = _task(client, host_headers, "propose", title="one").json()["task"]
    two = _task(client, host_headers, "propose", title="two").json()["task"]
    _task(client, _headers(bob), "claim", id=one["id"])
    _task(client, _headers(bob), "claim", id=two["id"])           # now shown on two
    _task(client, _headers(bob), "complete", id=one["id"])
    assert _status(client, host_headers, "bob")["task"] == two["id"]


def test_releasing_returns_the_old_owner_to_idle(client, session, host_headers):
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="hand me over").json()["task"]
    _task(client, _headers(bob), "claim", id=task["id"])
    assert _task(client, _headers(bob), "release", id=task["id"]).status_code == 200
    assert _status(client, host_headers, "bob")["state"] == "idle"


def test_a_status_about_a_closed_or_foreign_task_is_dropped_from_the_roster(client, session, host_headers):
    """Set by hand (or by an older client) and then overtaken by the board."""
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="done elsewhere").json()["task"]
    # Not bob's: the host holds it.
    _task(client, host_headers, "claim", id=task["id"])
    client.post("/ext/collab/v1/activity", headers=_headers(bob),
                json={"state": "working", "what": "done elsewhere", "task": task["id"]})
    assert _status(client, host_headers, "bob") == {}
    # Closed: gone from the board's open list.
    _task(client, host_headers, "complete", id=task["id"])
    assert _status(client, host_headers, "bob") == {}


def test_attaching_a_pr_shows_it_on_the_owners_status(client, session, host_headers):
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="the fix").json()["task"]
    _task(client, _headers(bob), "claim", id=task["id"])
    r = client.post("/ext/collab/v1/task-prs", headers=_headers(bob),
                    json={"id": task["id"], "url": "https://github.com/o/r/pull/42"})
    assert r.status_code == 200, r.text
    assert _status(client, host_headers, "bob")["what"] == "the fix (PR #42)"


def test_a_status_its_owner_has_talked_past_is_shown_as_stale(monkeypatch):
    now = time.time()
    said = {"state": "working", "what": "x", "updated_at": now - 3600, "since": now - 3600}
    busy = activity.as_seen(said, owner_id="p_b", open_tasks={}, last_spoke=now)
    assert busy["stale"] and activity.is_stale(busy)
    assert "last said working on x" in activity.describe(busy)
    quiet = activity.as_seen(said, owner_id="p_b", open_tasks={}, last_spoke=now - 3500)
    assert "stale" not in quiet, "talking within the window does not make it stale"
