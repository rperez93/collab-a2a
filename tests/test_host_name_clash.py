"""What happens when somebody joins under the host's own name.

bob reported from the other side that "a stranger's join took my host name,
and with it my ability to close the session". Run against this hub, that does
not happen: the join is refused outright. These tests pin that down, because the
refusal *is* the defence — if it ever softens into "take the name and suffix the
old holder", the host role goes with the name and the real host finds out by
discovering they can no longer stop their own session.
"""
from __future__ import annotations


def join(client, invite, name):
    return client.post("/ext/collab/v1/join",
                       json={"protocol_major": 2, "version": "2.0.0", "name": name, "invite": invite, "meta": {}})


def snapshot(client, token):
    return client.get("/ext/collab/v1/snapshot",
                      headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {token}"})


def test_joining_under_a_taken_name_is_refused(client, session):
    """409, and the message says how to get in anyway.

    Refusing without saying what to do instead would just move the problem: the
    person joining knows the name is taken and not that `--name` exists.
    """
    r = join(client, session["invite"], "alice")          # alice is the host
    assert r.status_code == 409, r.text
    assert "--name" in r.text, "refused without saying how to get in"


def test_the_host_keeps_the_role_after_the_attempt(client, session):
    """Nobody joins, so nobody takes anything — and it is worth asserting.

    If a join could take the name, whoever knows the host's name and holds an
    invite could take the session over.
    """
    join(client, session["invite"], "alice")
    snap = snapshot(client, session["host_token"]).json()
    hosts = [p["name"] for p in snap["participants"] if p.get("is_host")]
    assert hosts == ["alice"], f"the host role moved or split: {hosts}"
    assert snap["host"] == "alice"


def test_the_roster_never_holds_two_of_the_same_name(client, session):
    """Two people under one name make the roster unreadable, and the colours
    and the message sides along with it — everything keys off the name."""
    join(client, session["invite"], "alice")
    join(client, session["invite"], "bob")
    join(client, session["invite"], "bob")
    snap = snapshot(client, session["host_token"]).json()
    names = [p["name"] for p in snap["participants"]]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_a_different_name_still_gets_in(client, session):
    """The control: the refusal must not have shut the door on everyone."""
    r = join(client, session["invite"], "carol")
    assert r.status_code < 400, r.text
    snap = snapshot(client, session["host_token"]).json()
    assert "carol" in [p["name"] for p in snap["participants"]]
