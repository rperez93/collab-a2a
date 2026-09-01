"""Activity over the wire, and the task board it moves with.

Claiming a task is already the statement «I am doing this». Making an agent say
it twice — once on the board, once on the roster — is how the two drift apart,
and the one that gets forgotten is always the second. So the board is the
source, and the roster is its present tense.
"""

from __future__ import annotations

import pytest

from collab import activity


def _join(client, session, name="bob", **hello):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": name, "hello": hello or {"focus": "client"},
    })
    assert r.status_code == 200, r.text
    return r.json()


def _headers(joined):
    return {"Authorization": f"Bearer {joined['token']}"}


def _roster(client, headers):
    return client.get("/ext/collab/v1/snapshot", headers=headers).json()["participants"]


def _who(client, headers, name):
    return next(p for p in _roster(client, headers) if p["name"] == name)


# --- publishing -------------------------------------------------------------

def test_what_an_agent_says_it_is_doing_reaches_everyone(client, session, host_headers):
    bob = _join(client, session)

    r = client.post("/ext/collab/v1/activity", headers=_headers(bob), json={
        "state": "working", "what": "the token refresh",
        "files": ["src/api/auth.py"]})
    assert r.status_code == 200

    # The host reads it without asking bob anything.
    assert _who(client, host_headers, "bob")["activity"]["what"] == "the token refresh"


def test_it_is_announced_on_the_feed_as_well_as_stored(client, session, host_headers):
    """The roster answers whoever looks; the feed tells whoever is not looking."""
    bob = _join(client, session)
    client.post("/ext/collab/v1/activity", headers=_headers(bob),
                json={"state": "working", "what": "the client side"})

    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    said = [e for e in events if e["kind"] == "activity"]
    assert len(said) == 1
    assert said[0]["body"]["what"] == "the client side"


def test_a_later_activity_replaces_rather_than_merges(client, session, host_headers):
    """Usage figures accumulate; an activity is a statement about NOW. Merging
    would leave the last piece of work's files attached to the next one."""
    bob = _join(client, session)
    client.post("/ext/collab/v1/activity", headers=_headers(bob), json={
        "state": "working", "what": "the refresh", "files": ["auth.py"]})
    client.post("/ext/collab/v1/activity", headers=_headers(bob), json={
        "state": "working", "what": "the roster"})

    doing = _who(client, host_headers, "bob")["activity"]
    assert doing["what"] == "the roster"
    assert "files" not in doing


def test_nonsense_is_refused_rather_than_stored(client, session):
    bob = _join(client, session)
    r = client.post("/ext/collab/v1/activity", headers=_headers(bob),
                    json={"state": "pondering"})
    assert r.status_code == 400


def test_a_roster_from_before_this_existed_still_reads(client, session, host_headers):
    """Nothing in the database changed shape, so an old session simply has
    nobody saying anything yet."""
    _join(client, session)
    assert _who(client, host_headers, "bob")["activity"] == {}


# --- the board ---------------------------------------------------------------

def _task(client, headers, action, **body):
    return client.post("/ext/collab/v1/tasks", headers=headers,
                       json={"action": action, **body})


def test_a_task_somebody_else_holds_is_refused_with_a_reason(client, session, host_headers):
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="migrate sessions").json()["task"]
    _task(client, host_headers, "claim", id=task["id"])

    r = _task(client, _headers(bob), "claim", id=task["id"])
    assert r.status_code == 409
    assert "already claimed by alice" in r.json()["detail"]
    assert "ask them" in r.json()["detail"]


def test_finished_work_cannot_be_claimed_back_into_progress(client, session, host_headers):
    """Claiming a completed task put it back into WORKING and told the room
    somebody was on it — so the board showed finished work under way again,
    and the agent that claimed it was about to redo it."""
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="migrate sessions").json()["task"]
    _task(client, host_headers, "claim", id=task["id"])
    _task(client, host_headers, "complete", id=task["id"])

    r = _task(client, _headers(bob), "claim", id=task["id"])
    assert r.status_code == 409
    assert "completed" in r.json()["detail"]
    assert "propose a new task" in r.json()["detail"]


def test_a_cancelled_task_is_finished_too(client, session, host_headers):
    bob = _join(client, session)
    task = _task(client, host_headers, "propose", title="drop the old store").json()["task"]
    _task(client, host_headers, "cancel", id=task["id"])

    assert _task(client, _headers(bob), "claim", id=task["id"]).status_code == 409


def test_an_unclaimed_task_is_still_free_to_take(client, session):
    """The validation must not make the board unusable."""
    bob = _join(client, session)
    task = _task(client, _headers(bob), "propose", title="the client side").json()["task"]

    r = _task(client, _headers(bob), "claim", id=task["id"])
    assert r.status_code == 200
    assert r.json()["task"]["owner"] == "bob"


def test_a_task_you_already_hold_can_be_updated(client, session):
    bob = _join(client, session)
    task = _task(client, _headers(bob), "propose", title="the client side").json()["task"]
    _task(client, _headers(bob), "claim", id=task["id"])

    assert _task(client, _headers(bob), "claim", id=task["id"]).status_code == 200
