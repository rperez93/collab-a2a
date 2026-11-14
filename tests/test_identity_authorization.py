"""A display-name replacement must not inherit another participant's authority."""

from types import SimpleNamespace

import pytest


EXT = "/ext/collab/v1"


@pytest.fixture
def identities(session):
    """Only synthetic participants and tokens in the ordinary isolated hub fixture."""
    store = session["store"]
    people = {}
    for name in ("bob", "carol", "replacement"):
        token = "synthetic-identity-" + name
        people[name] = (store.add_participant(name, token), {
            "Authorization": "Bearer " + token,
            "Collab-Protocol-Major": "2", "Collab-Version": "2.0.0",
        })
    return people


def _swap(store, identities):
    store.rename(identities["bob"][0].id, "bobby")
    store.rename(identities["replacement"][0].id, "bob")


@pytest.mark.parametrize("action", ["assign", "archive", "delete"])
def test_a_replacement_cannot_inherit_a_project_creators_authority(
        session, client, identities, action):
    """An owner ID must not coexist with a creator-name authorization escape."""
    made = client.post(EXT + "/projects", headers=identities["bob"][1], json={
        "action": "propose", "title": "Synthetic private authority", "owner": "carol",
    })
    assert made.status_code == 200, made.text
    project = made.json()["project"]
    _swap(session["store"], identities)
    response = client.post(EXT + "/projects", headers=identities["replacement"][1],
                           json={"action": action, "id": project["id"], "owner": "bob"})
    assert response.status_code == 403, response.text
    original = client.post(EXT + "/projects", headers=identities["bob"][1],
                           json={"action": action, "id": project["id"], "owner": "carol"})
    assert original.status_code == 200, original.text


@pytest.mark.parametrize("actor,expected", [("bob", 200), ("replacement", 409)])
def test_a_task_claim_stays_with_its_original_identity_after_a_name_swap(
        session, client, host_headers, identities, actor, expected):
    """Both the HTTP guard and atomic Store claim guard must compare owner IDs."""
    made = client.post(EXT + "/tasks", headers=host_headers,
                       json={"action": "propose", "title": "Synthetic task"})
    assert made.status_code == 200, made.text
    task_id = made.json()["task"]["id"]
    claim = client.post(EXT + "/tasks", headers=identities["bob"][1],
                        json={"action": "claim", "id": task_id})
    assert claim.status_code == 200, claim.text
    _swap(session["store"], identities)
    response = client.post(EXT + "/tasks", headers=identities[actor][1],
                           json={"action": "claim", "id": task_id})
    assert response.status_code == expected, response.text


def test_a_private_message_replay_stays_with_the_original_identity(
        session, client, host_headers, identities):
    """Positive control: modern private-event history already uses stable IDs."""
    sent = client.post(EXT + "/messages", headers=host_headers,
                       json={"text": "synthetic-private-marker", "to": "bob"})
    assert sent.status_code == 200, sent.text
    _swap(session["store"], identities)
    for actor, visible in (("bob", True), ("replacement", False)):
        history = client.get(EXT + "/history", headers=identities[actor][1])
        assert history.status_code == 200, history.text
        assert ("synthetic-private-marker" in history.text) is visible


def test_a_decline_reply_never_follows_the_requesters_reused_display_name(
        session, client, identities, monkeypatch):
    """The real CLI may decline without consent, but may not leak to a replacement."""
    from collab import cli

    ask = client.post(EXT + "/messages", headers=identities["bob"][1], json={
        "text": "Synthetic skill request", "to": "carol",
        "body": {"skill_request": {"id": "sr_012345abcdef", "skill": "test-skill", "args": ""}},
    })
    assert ask.status_code == 200, ask.text
    _swap(session["store"], identities)
    profile = SimpleNamespace(name="carol", participant_id=identities["carol"][0].id)

    class ActualAPI:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def history(self, **_):
            return session["store"].history(viewer=profile.participant_id, limit=500)

        def send(self, envelope):
            response = client.post(EXT + "/messages", headers=identities["carol"][1],
                                   json=envelope.to_dict())
            assert response.status_code == 200, response.text

    monkeypatch.setattr(cli, "_require_own_profile", lambda _: profile)
    monkeypatch.setattr(cli, "_client", lambda _: ActualAPI())
    result = cli.cmd_skills_remote(SimpleNamespace(
        action="decline", target="sr_012345abcdef", note="Synthetic decline", session=None))
    assert result == 0
    original = session["store"].history(viewer=identities["bob"][0].id, limit=500)
    replacement = session["store"].history(viewer=identities["replacement"][0].id, limit=500)
    assert any((event.body or {}).get("skill_response") for event in original)
    assert not any((event.body or {}).get("skill_response") for event in replacement)


@pytest.mark.parametrize("missing_recipient", [False, True])
def test_a_legacy_private_event_without_a_proven_identity_is_not_made_public(
        tmp_path, missing_recipient):
    """Reopening cannot bind historical private text to a current name claimant."""
    from collab.protocol import Envelope
    from collab.server.store import Store

    path = tmp_path / "legacy-events.db"
    store = Store(path)
    try:
        sender = store.add_participant("sender", "synthetic-sender")
        old = store.add_participant("bob", "synthetic-old")
        other = store.add_participant("carol", "synthetic-other")
        store.append(Envelope(kind="chat", text="Synthetic private legacy marker",
                              sender="sender", sender_id=sender.id, to="bob", to_id=old.id))
        store.rename(old.id, "bobby")
        store.rename(other.id, "bob")
        # The existing pre-ID table had only recipient text. Its authorizing
        # identity cannot be recovered from whoever holds that name today.
        with store._lock:
            store._db.execute("ALTER TABLE events DROP COLUMN sender_id")
            store._db.execute("ALTER TABLE events DROP COLUMN recipient_id")
            if missing_recipient:
                store._db.execute("UPDATE events SET recipient='missing-legacy-peer'")
            store._db.commit()
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert not reopened.history(viewer=other.id)
        assert not reopened.since(0, viewer=other.id)
        assert not reopened.history(viewer=old.id)
    finally:
        reopened.close()


def test_an_unresolved_private_a2a_target_never_becomes_public_replay(
        client, host_headers, identities):
    """Live fanout and persisted history must both fail closed for unknown DMs."""
    response = client.post("/a2a", headers={**host_headers, "A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "synthetic-unresolved", "role": "ROLE_USER", "parts": [{
                "mediaType": "application/json", "data": {"collab": "v1", "kind": "chat",
                    "text": "Synthetic unresolved private marker", "to": "missing-synthetic-peer"},
            }]},
        },
    })
    assert response.status_code == 200, response.text
    history = client.get(EXT + "/history", headers=identities["carol"][1])
    assert history.status_code == 200, history.text
    assert "Synthetic unresolved private marker" not in history.text
