"""Usage sharing: how it travels, and what it is for.

The point is that an agent can look at who has quota left before handing out
the next task, so the figures have to reach everyone, not just the host.
"""

from __future__ import annotations

import time

from collab.config import share_stats_enabled


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join",
                    json={"invite": session["invite"], "name": name, "hello": {}})
    assert r.status_code == 200, r.text
    return r.json()


def _headers(joined):
    return {"Authorization": f"Bearer {joined['token']}"}


def _person(client, headers, name):
    people = client.get("/ext/collab/v1/participants",
                        headers=headers).json()["participants"]
    return next(p for p in people if p["name"] == name)


def test_sharing_is_on_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    assert share_stats_enabled() is True


def test_sharing_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    from collab.config import set_share_stats

    set_share_stats(False)
    assert share_stats_enabled() is False


def test_stats_posted_to_the_hub_reach_everyone(client, session, host_headers):
    """Sent to the host, but the whole session can read them."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob), json={
        "machine": "bobs-laptop",
        "stats": {"model": "Opus 5", "cost_usd": 1.24, "quota_five_hour": 42.0},
    })

    seen = _person(client, host_headers, "bob")
    assert seen["machine"] == "bobs-laptop"
    assert seen["stats"]["quota_five_hour"] == 42.0

    # And a third party sees them too, not just the host.
    carol = _join(client, session, "carol")
    assert _person(client, _headers(carol), "bob")["stats"]["cost_usd"] == 1.24


def test_stats_ride_along_with_an_ordinary_message(client, session, host_headers):
    """Piggybacking keeps them current without a separate heartbeat."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages", headers=_headers(bob), json={
        "text": "on it",
        "stats": {"quota_five_hour": 88.5, "model": "Opus 5"},
    })
    assert _person(client, host_headers, "bob")["stats"]["quota_five_hour"] == 88.5


def test_later_stats_merge_rather_than_replace(client, session, host_headers):
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "a", "stats": {"model": "Opus 5", "cost_usd": 1.0}})
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "b", "stats": {"cost_usd": 2.0}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["cost_usd"] == 2.0
    assert stats["model"] == "Opus 5", "an update must not drop what it omits"


def test_quota_is_readable_for_balancing_work(client, session, host_headers):
    """The whole point: pick whoever has headroom left."""
    bob = _join(client, session, "bob")
    carol = _join(client, session, "carol")
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "x", "stats": {"quota_five_hour": 91.0}})
    client.post("/ext/collab/v1/messages", headers=_headers(carol),
                json={"text": "y", "stats": {"quota_five_hour": 12.0}})

    people = client.get("/ext/collab/v1/participants",
                        headers=host_headers).json()["participants"]
    with_quota = [(p["name"], p["stats"]["quota_five_hour"])
                  for p in people if (p.get("stats") or {}).get("quota_five_hour")]
    assert min(with_quota, key=lambda pair: pair[1])[0] == "carol"


def test_an_agent_that_shares_nothing_is_not_a_problem(client, session, host_headers):
    _join(client, session, "bob")
    assert _person(client, host_headers, "bob").get("stats") in ({}, None)


def test_a_partial_report_to_the_endpoint_merges(client, session, host_headers):
    """Reports merge everywhere, not only on the message path.

    The endpoint replaced wholesale, so telling the hub one new figure erased
    everything else that agent had shared.
    """
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={
        "stats": {"model": "gpt-5-codex", "quota_five_hour": 73}})
    client.post("/ext/collab/v1/stats", headers=h, json={
        "stats": {"tokens_in": 184000}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["tokens_in"] == 184000
    assert stats["model"] == "gpt-5-codex", "a partial update must not erase the rest"
    assert stats["quota_five_hour"] == 73.0


def test_the_endpoint_still_records_the_machine(client, session, host_headers):
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"machine": "bobs-laptop", "stats": {"model": "x"}})
    assert _person(client, host_headers, "bob")["machine"] == "bobs-laptop"


def test_the_endpoint_normalises_like_every_other_path(client, session, host_headers):
    """Remaining-quota inverted here too, not just on the status line path."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"quota_used_pct": 42.0}})
    assert _person(client, host_headers, "bob")["stats"]["quota_used_pct"] == 42.0


def test_reporting_one_window_does_not_erase_the_others(client, session,
                                                        host_headers):
    """An agent that can only see one window right now must not lose the rest.

    Merging the map wholesale meant a five-hour update wiped the weekly figure
    and the spend cap reported a minute earlier.
    """
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {
        "five_hour": {"used_pct": 55, "resets_at": "SOON"},
        "seven_day": {"used_pct": 20},
    }}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {
        "spend_limit": {"used_pct": 91},
    }}})

    windows = _person(client, host_headers, "bob")["stats"]["quotas"]
    assert set(windows) == {"five_hour", "seven_day", "spend_limit"}
    assert windows["five_hour"]["resets_at"] == "SOON", "the reset survived too"
    assert windows["spend_limit"]["used_pct"] == 91.0


def test_a_window_update_refreshes_that_window(client, session, host_headers):
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 10}}}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 90}}}})
    windows = _person(client, host_headers, "bob")["stats"]["quotas"]
    assert windows["five_hour"]["used_pct"] == 90.0


# --- when was this true? -------------------------------------------------------
#
# A quota reading is a fact about a moment. `collab stats` printed the number
# and nothing about the moment, so a 91 % five-hour window reported three hours
# ago read exactly like one reported just now — and the two call for opposite
# decisions about who takes the next task.

def test_the_hub_stamps_when_usage_arrived(client, session, host_headers):
    """The hub's clock, at merge time: the one clock every participant shares."""
    bob = _join(client, session, "bob")
    before = time.time()
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "x", "stats": {"quota_five_hour": 40.0}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert "reported_at" in stats, "the roster does not say when this was true"
    assert before - 1 <= float(stats["reported_at"]) <= time.time() + 1


def test_a_client_cannot_backdate_or_postdate_its_own_report(client, session,
                                                             host_headers):
    """A participant's own `reported_at` is a remote party's choice of value;
    the hub's clock overrides it."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "x", "stats": {"quota_five_hour": 40.0,
                                             "reported_at": 1.0}})
    stamped = float(_person(client, host_headers, "bob")["stats"]["reported_at"])
    assert stamped > 1_000_000_000, "the client's stamp was believed"


def test_a_later_report_moves_the_stamp(client, session, host_headers):
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "x", "stats": {"quota_five_hour": 40.0}})
    first = float(_person(client, host_headers, "bob")["stats"]["reported_at"])
    time.sleep(0.02)
    client.post("/ext/collab/v1/messages", headers=_headers(bob),
                json={"text": "y", "stats": {"cost_usd": 1.0}})
    second = float(_person(client, host_headers, "bob")["stats"]["reported_at"])
    assert second > first
