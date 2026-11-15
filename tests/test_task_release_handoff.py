"""collab task release / handoff, and the notices an ownership change sends (T_b079)."""


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join", json={"protocol_major": 2, "version": "2.0.0",
                                                 "invite": session["invite"], "name": name, "hello": {}})
    body = r.json()
    return {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0",
            "Authorization": f"Bearer {body['token']}"}, body["id"]


def _act(client, headers, action, task_id=None, **extra):
    return client.post("/ext/collab/v1/tasks", headers=headers,
                       json={"action": action, "id": task_id, "title": "Ship the thing", **extra})


def _claimed(client, session, owner_headers):
    task = _act(client, owner_headers, "propose").json()["task"]
    assert _act(client, owner_headers, "claim", task["id"]).status_code == 200
    return task["id"]


def _notices(session, participant_id):
    return [e for e in session["store"].history(viewer=participant_id)
            if e.kind == "task" and (e.body or {}).get("action") == "ownership" and e.to_id == participant_id]


def test_the_owner_releases_and_anyone_may_claim_again(client, session):
    (bob, bob_id), (carol, _) = _join(client, session, "bob"), _join(client, session, "carol")
    task_id = _claimed(client, session, bob)
    released = _act(client, bob, "release", task_id).json()["task"]
    assert released["owner"] is None and released["state"] == "TASK_STATE_SUBMITTED"
    assert _act(client, carol, "claim", task_id).json()["task"]["owner"] == "carol"


def test_a_non_owner_non_host_cannot_release_or_hand_off(client, session):
    (bob, _), (carol, _) = _join(client, session, "bob"), _join(client, session, "carol")
    task_id = _claimed(client, session, bob)
    assert _act(client, carol, "release", task_id).status_code == 403
    assert _act(client, carol, "handoff", task_id, to="carol").status_code == 403
    assert session["store"].get_task(task_id)["owner"] == "bob"


def test_a_handoff_is_accepted_by_its_target_only_and_both_are_told(client, session):
    (bob, bob_id), (carol, carol_id), (dave, _) = (_join(client, session, n) for n in ("bob", "carol", "dave"))
    task_id = _claimed(client, session, bob)
    assert _act(client, bob, "handoff", task_id, to="carol").status_code == 200
    assert session["store"].get_task(task_id)["owner"] == "bob", "still bob's until carol accepts"
    assert _act(client, dave, "claim", task_id).status_code == 409, "an offer is not an open task"
    accepted = _act(client, carol, "claim", task_id).json()["task"]
    assert accepted["owner"] == "carol" and session["store"].get_task(task_id)["handoff_to"] is None
    assert [n.body["change"] for n in _notices(session, carol_id)] == ["handoff"]
    assert [n.body["change"] for n in _notices(session, bob_id)] == ["accept"]
    assert not _notices(session, _join(client, session, "erin")[1]), "private to the people involved"


def test_a_handoff_needs_another_participant(client, session):
    (bob, _), = (_join(client, session, "bob"),)
    task_id = _claimed(client, session, bob)
    assert _act(client, bob, "handoff", task_id, to="nobody").status_code == 400
    assert _act(client, bob, "handoff", task_id, to="bob").status_code == 400


def test_the_host_may_release_only_an_offline_participants_claim(client, session, host_headers):
    """A real feed, subscribed by participant id: while bob is online the host is refused."""
    (bob, bob_id), = (_join(client, session, "bob"),)
    task_id = _claimed(client, session, bob)
    hub = client.app.state.hub
    sub = client.portal.call(hub.subscribe, bob_id)
    try:
        assert _act(client, host_headers, "release", task_id).status_code == 403, "bob is online"
    finally:
        client.portal.call(hub.unsubscribe, sub)
    r = _act(client, host_headers, "release", task_id)
    assert r.status_code == 200 and r.json()["task"]["owner"] is None
    announced = [e for e in session["store"].history(viewer=bob_id)
                 if e.kind == "task" and (e.body or {}).get("action") == "release" and not e.to_id]
    assert announced and announced[-1].body["by_host"] is True and announced[-1].body["previous_owner_id"] == bob_id
    assert [n.body["change"] for n in _notices(session, bob_id)] == ["release"]


def test_a_handoff_to_a_removed_participant_is_refused(client, session, host_headers):
    (bob, _), (carol, _) = _join(client, session, "bob"), _join(client, session, "carol")
    task_id = _claimed(client, session, bob)
    assert client.post("/ext/collab/v1/revoke", json={"name": "carol"}, headers=host_headers).status_code == 200
    assert _act(client, bob, "handoff", task_id, to="carol").status_code == 400


def test_a_repeated_offer_is_not_news_and_notices_carry_no_title(client, session):
    (bob, _), (carol, carol_id) = _join(client, session, "bob"), _join(client, session, "carol")
    task_id = _claimed(client, session, bob)
    _act(client, bob, "handoff", task_id, to="carol")
    _act(client, bob, "handoff", task_id, to="carol")
    notices = _notices(session, carol_id)
    assert len(notices) == 1
    assert "Ship the thing" not in (notices[0].text or ""), "no participant-written title in a direct notice"


def test_releasing_clears_a_pending_offer(client, session):
    (bob, _), (carol, _), (dave, _) = (_join(client, session, n) for n in ("bob", "carol", "dave"))
    task_id = _claimed(client, session, bob)
    _act(client, bob, "handoff", task_id, to="carol")
    assert _act(client, bob, "release", task_id).status_code == 200
    assert session["store"].get_task(task_id)["handoff_to"] is None
    assert _act(client, dave, "claim", task_id).json()["task"]["owner"] == "dave", "open to anyone again"
