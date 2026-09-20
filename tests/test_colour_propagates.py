"""Changing your colour reaches everybody else's screen.

Declared once and seen by everyone is the whole promise of `collab color`, and
it crosses four layers to get there: the CLI stores it, the client publishes it,
the hub keeps it, the roster carries it and the viewer paints it. It was broken
in the middle — the hub stored it and the roster dropped it — and every layer
tested on its own still passed. So this walks the whole path.
"""
from __future__ import annotations

import pytest

from collab.client import tui


def join(client, invite, name):
    return client.post("/ext/collab/v1/join",
                       json={"protocol_major": 2, "version": "2.0.0", "name": name, "invite": invite, "meta": {}})


def token_of(response):
    body = response.json()
    return body.get("token") or body.get("access_token")


def roster(client, token):
    r = client.get("/ext/collab/v1/snapshot",
                   headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {token}"})
    assert r.status_code < 400, r.text
    return r.json()["participants"]


def person(people, name):
    return next((p for p in people if p["name"] == name), None)


@pytest.fixture
def bob(client, session):
    r = join(client, session["invite"], "bob")
    assert r.status_code < 400, r.text
    return token_of(r)


def set_colour(client, token, value):
    # The wire shape the client actually sends: hub_client.report_stats flattens
    #  into the top level, so it is {"stats": {...}, "color": ...}.
    # My first version of this posted {"figures", "identity"} and failed against
    # correct code — a test that invents its own protocol proves nothing.
    return client.post("/ext/collab/v1/stats",
                       headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {token}"},
                       json={"stats": {}, "color": value})


# --- the whole path ----------------------------------------------------------

def test_a_colour_bob_sets_is_visible_to_alice(client, session, bob):
    """The one that matters. It was failing at the roster and nothing said so."""
    assert set_colour(client, bob, "#00cccc").status_code < 400

    seen = person(roster(client, session["host_token"]), "bob")
    assert seen is not None
    assert seen.get("color") == "#00cccc", \
        f"alice does not see bob's colour: {seen}"


def test_the_viewer_turns_that_into_a_painted_colour(client, session, bob,
                                                     monkeypatch):
    """Carrying it is not enough — the renderer has to pick it up.

    Both halves have failed separately: once the roster dropped the key, once
    the viewer only seeded its own name.
    """
    asked: list = []
    monkeypatch.setattr(tui, "_pair_for", lambda v: asked.append(v) or 55)
    tui._CHOSEN.clear()
    try:
        set_colour(client, bob, "#00cccc")
        tui.record_colours(roster(client, session["host_token"]))
        assert tui._CHOSEN.get("bob") == "#00cccc"
        assert tui._speaker_pair("bob") == 55
        assert asked == ["#00cccc"]
    finally:
        tui._CHOSEN.clear()


def test_changing_it_again_replaces_it(client, session, bob):
    """A colour that only ever gets set once is a colour you cannot correct."""
    set_colour(client, bob, "#00cccc")
    set_colour(client, bob, "#ff7f50")
    assert person(roster(client, session["host_token"]), "bob").get("color") == "#ff7f50"


def test_clearing_it_reaches_everybody_too(client, session, bob):
    """Setting without clearing is half a feature."""
    set_colour(client, bob, "#00cccc")
    set_colour(client, bob, "")
    seen = person(roster(client, session["host_token"]), "bob")
    assert not seen.get("color"), f"the cleared colour is still published: {seen}"

    tui._CHOSEN.clear()
    tui.record_colours(roster(client, session["host_token"]))
    assert "bob" not in tui._CHOSEN


def test_setting_a_colour_does_not_wipe_the_rest_of_the_identity(
        client, session, bob):
    """`/stats` merges rather than replaces — reporting one number used to
    erase everything else, and a colour must not bring that back."""
    client.post("/ext/collab/v1/stats",
                headers={"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {bob}"},
                json={"stats": {"spend_usd": 1.5}, "machine": "webapp-box"})
    set_colour(client, bob, "#00cccc")

    seen = person(roster(client, session["host_token"]), "bob")
    assert seen.get("color") == "#00cccc"
    assert seen.get("machine") == "webapp-box", f"the colour wiped the machine: {seen}"
    assert seen.get("stats", {}).get("spend_usd") == 1.5


def test_nobody_elses_colour_moves(client, session, bob):
    """The control: a colour is for one person, not for the room."""
    set_colour(client, bob, "#00cccc")
    alice = person(roster(client, session["host_token"]), "alice")
    assert not alice.get("color")
