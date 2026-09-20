"""A colour change is pushed, not waited for — and only when it really changed.

Every viewer re-reads the roster when a presence event arrives. Without the hub
saying anything, the only thing that moved a colour was the daemon's 9-second
poll: you change it, look at the other screen, see nothing, and change it again.

The other half matters just as much. The daemon reports stats on that same
heartbeat, so treating every report as a change would put an event in the
channel six times a minute per participant and refresh every roster in the room
for nothing.

Counted by totals rather than by a cursor: the first version of this used a
"last seq" mark and an `after` filter, and failed against correct code because
the mark did not mean what I assumed. Totals cannot be misread.
"""
from __future__ import annotations

import pytest


def join(client, invite, name):
    return client.post("/ext/collab/v1/join",
                       json={"protocol_major": 2, "version": "2.0.0", "name": name, "invite": invite, "meta": {}})


@pytest.fixture
def bob(client, session):
    r = join(client, session["invite"], "bob")
    assert r.status_code < 400, r.text
    body = r.json()
    return body.get("token") or body.get("access_token")


def report(client, token, **fields):
    return client.post("/ext/collab/v1/stats",
                       headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {token}"},
                       json={"stats": {}, **fields})


def presence_count(client, token):
    r = client.get("/ext/collab/v1/history",
                   headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {token}"},
                   params={"limit": 200})
    if r.status_code >= 400:
        pytest.skip(f"no history to read events from ({r.status_code})")
    body = r.json()
    events = body.get("events") or body.get("messages") or []
    return sum(1 for e in events if e.get("kind") == "presence")


def test_changing_the_colour_publishes_an_event(client, session, bob):
    before = presence_count(client, session["host_token"])
    report(client, bob, color="#00cccc")
    assert presence_count(client, session["host_token"]) == before + 1, \
        "nobody was told the colour changed"


def test_reporting_the_same_colour_again_publishes_nothing(client, session, bob):
    """The one that keeps the channel usable.

    The daemon reports on a heartbeat. If a repeat counted as a change, every
    participant would emit an event every nine seconds, forever.
    """
    report(client, bob, color="#00cccc")
    before = presence_count(client, session["host_token"])
    for _ in range(3):
        report(client, bob, color="#00cccc")
    assert presence_count(client, session["host_token"]) == before, \
        "the heartbeat is publishing events for an unchanged colour"


def test_a_second_change_publishes_again(client, session, bob):
    """The control: silence on repeats must not be silence on everything."""
    report(client, bob, color="#00cccc")
    before = presence_count(client, session["host_token"])
    report(client, bob, color="#ff7f50")
    assert presence_count(client, session["host_token"]) == before + 1


def test_plain_stats_with_no_identity_publish_nothing(client, session, bob):
    """Reporting figures is not an identity change."""
    before = presence_count(client, session["host_token"])
    client.post("/ext/collab/v1/stats",
                headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {bob}"},
                json={"stats": {"spend_usd": 2.0}})
    assert presence_count(client, session["host_token"]) == before


def test_clearing_the_colour_publishes_too(client, session, bob):
    """Clearing is a change, and the other screens have to hear about it."""
    report(client, bob, color="#00cccc")
    before = presence_count(client, session["host_token"])
    report(client, bob, color="")
    assert presence_count(client, session["host_token"]) == before + 1, \
        "clearing the colour told nobody"
