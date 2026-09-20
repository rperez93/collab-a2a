"""Renaming yourself mid-session must not break routing.

Names are what people see; they are not identity. Anything that routes on a
display name breaks the moment someone changes theirs.
"""

from __future__ import annotations


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join",
                    json={"protocol_major": 2, "version": "2.0.0", "invite": session["invite"], "name": name, "hello": {}})
    assert r.status_code == 200, r.text
    return {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {r.json()['token']}"}


def _texts(client, headers):
    return [e.get("text") for e in
            client.get("/ext/collab/v1/history", headers=headers).json()["events"]]


def test_a_dm_reaches_someone_who_renamed_themselves(client, session, host_headers):
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=bob)

    r = client.post("/ext/collab/v1/messages",
                    json={"text": "after your rename", "to": "roberto"},
                    headers=host_headers)
    assert r.status_code == 200
    assert "after your rename" in _texts(client, bob)


def test_a_dm_to_the_host_survives_the_host_renaming(client, session, host_headers):
    """The host is the common case: guests hold its name from join time."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/rename", json={"name": "alicia"}, headers=host_headers)

    r = client.post("/ext/collab/v1/messages",
                    json={"text": "still reaches the host", "to": "alicia"},
                    headers=bob)
    assert r.status_code == 200
    assert "still reaches the host" in _texts(client, host_headers)


def test_a_dm_sent_to_the_old_name_still_arrives(client, session, host_headers):
    """Everyone else's copy of your name is stale the instant you rename."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=bob)

    r = client.post("/ext/collab/v1/messages",
                    json={"text": "addressed to the old name", "to": "bob"},
                    headers=host_headers)
    assert r.status_code == 200
    assert "addressed to the old name" in _texts(client, bob)


def test_old_dms_stay_visible_after_a_rename(client, session, host_headers):
    """History addressed to your old name is still yours."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages",
                json={"text": "sent before the rename", "to": "bob"},
                headers=host_headers)
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=bob)

    assert "sent before the rename" in _texts(client, bob)


def test_a_rename_does_not_strand_your_own_messages(client, session, host_headers):
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages", json={"text": "mine", "to": "alice"},
                headers=bob)
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=bob)
    assert "mine" in _texts(client, bob), "you can still see what you sent"


def test_a_dm_is_still_private_after_a_rename(client, session, host_headers):
    carol = _join(client, session, "carol")
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=bob)
    client.post("/ext/collab/v1/messages",
                json={"text": "private after rename", "to": "roberto"},
                headers=host_headers)
    assert "private after rename" not in _texts(client, carol)


async def test_a_renamed_participant_is_still_reported_as_connected(session):
    """A stale roster shows everyone offline, which the status line reads as 'alone'.

    The live subscription is keyed on identity, so renaming must not orphan it.
    """
    from collab.server.hub import Hub

    store = session["store"]
    hub = Hub(store, session_id="s", host_name="alice")

    alice = store.participant_for_token(session["host_token"])
    await hub.subscribe(alice.id)
    assert alice.name in {p["name"] for p in hub.snapshot()["participants"]
                          if p["connected"]}

    store.rename(alice.id, "alicia")

    connected = {p["name"] for p in hub.snapshot()["participants"] if p["connected"]}
    assert "alicia" in connected, "a rename must not make you look offline"
    assert hub.host_name == "alicia", "the host's current name has to follow too"


def test_names_still_work_alongside_ids(client, session, host_headers):
    """Ids are how it routes; names remain how people and older clients address.

    A client that knows nothing about ids must still be able to send a DM.
    """
    bob = _join(client, session, "bob")
    r = client.post("/ext/collab/v1/messages",
                    json={"text": "addressed by name only", "to": "bob"},
                    headers=host_headers)
    assert r.status_code == 200
    assert "addressed by name only" in _texts(client, bob)

    events = client.get("/ext/collab/v1/history", headers=bob).json()["events"]
    dm = next(e for e in events if e.get("text") == "addressed by name only")
    assert dm["to"] == "bob", "the human-readable name stays on the wire"
    assert dm["from"] == "alice"
    assert dm.get("toId"), "and the routing id travels with it"


def test_the_current_holder_of_a_name_wins(client, session, host_headers):
    """If someone renames away and a newcomer takes the name, it means them."""
    first = _join(client, session, "bob")
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=first)
    second = _join(client, session, "bob")

    client.post("/ext/collab/v1/messages",
                json={"text": "for the new bob", "to": "bob"}, headers=host_headers)

    assert "for the new bob" in _texts(client, second)
    assert "for the new bob" not in _texts(client, first)
