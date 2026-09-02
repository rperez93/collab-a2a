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


def test_later_stats_merge_except_the_quota_which_is_replaced(client, session,
                                                             host_headers):
    """Non-quota figures merge; the quota is whatever the last report said.

    This test used to be `test_later_stats_merge_rather_than_replace`, and it
    held that an update never drops what it omits. It still does for the
    model, the spend and the token counts — figures an agent learns one at a
    time. It stopped being true for the quota, because the merge was the
    defect: an agent that could no longer see a quota window kept showing its
    old one to everybody, and work was split on a number nobody had reported
    for an hour. What an agent reports is now the whole truth about its
    quota — see `test_a_report_is_the_whole_truth_about_the_quota`.
    """
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "a", "stats": {"model": "Opus 5", "cost_usd": 1.0,
                                             "quota_five_hour": 40.0}})
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "b", "stats": {"cost_usd": 2.0}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["cost_usd"] == 2.0
    assert stats["model"] == "Opus 5", "an update must not drop the model it omits"
    assert "quota_five_hour" not in stats, \
        "but a quota it omits is a quota it no longer has"


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
    """Non-quota figures merge everywhere, not only on the message path.

    The endpoint replaced wholesale, so telling the hub one new figure erased
    everything else that agent had shared. The quota is the exception, on
    purpose: a report that says nothing about it is read as «none», not as
    «unchanged».
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
    assert "quota_five_hour" not in stats, "except the quota, which it omitted"


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


# --- the quota is the whole truth, every time --------------------------------
#
# Windows used to merge one at a time, so that an agent reporting only its
# five-hour window did not erase the weekly one. The consequence was the
# opposite defect, and the worse one: an agent that could no longer see a
# window kept showing its old figure to everyone, and people split work on it.
# A report now carries every window the agent knows, and one it leaves out is
# read as gone. The instructions that tell agents how to report say the same.

def test_a_report_is_the_whole_truth_about_the_quota(client, session,
                                                     host_headers):
    """Report two windows, then one: the other is gone — for a third party too."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {
        "five_hour": {"used_pct": 55, "resets_at": "SOON"},
        "seven_day": {"used_pct": 20},
    }}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {
        "five_hour": {"used_pct": 60},
    }}})

    windows = _person(client, host_headers, "bob")["stats"]["quotas"]
    assert set(windows) == {"five_hour"}, windows
    assert windows["five_hour"] == {"used_pct": 60.0}, \
        "and the window itself is what was said, not the old reset merged in"
    carol = _join(client, session, "carol")
    assert set(_person(client, _headers(carol), "bob")["stats"]["quotas"]) == {"five_hour"}


def test_an_empty_quotas_map_clears_the_quota(client, session, host_headers):
    """`quotas: {}` is a statement — «I have no quota to report» — and not a
    report with nothing in it. Every quota field goes, the flat ones too."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "model": "gpt-5", "quota_five_hour": 73, "quota_seven_day": 12,
        "quota_used_pct": 73, "quota_reset_at": "SOON",
        "quotas": {"five_hour": {"used_pct": 73}, "seven_day": {"used_pct": 12}}}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {}}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert not any(key.startswith("quota") for key in stats), stats
    assert stats["model"] == "gpt-5", "the model is not quota and stays"


def test_a_report_with_no_quota_at_all_clears_it(client, session, host_headers):
    """Absent is «gone», not «unchanged»: the stored windows and the flat
    figures both go, on the message path as on the endpoint."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/messages", headers=h, json={
        "text": "x", "stats": {"quotas": {"five_hour": {"used_pct": 91}},
                               "quota_five_hour": 91}})
    assert _person(client, host_headers, "bob")["stats"]["quota_five_hour"] == 91.0
    client.post("/ext/collab/v1/messages", headers=h, json={
        "text": "y", "stats": {"cost_usd": 2.0}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert "quotas" not in stats and "quota_five_hour" not in stats, stats
    assert stats["cost_usd"] == 2.0


def test_the_model_survives_a_quota_only_report(client, session, host_headers):
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"model": "Opus 5", "cost_usd": 1.0}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 10}}}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["model"] == "Opus 5" and stats["cost_usd"] == 1.0
    assert stats["quotas"] == {"five_hour": {"used_pct": 10.0}}


def test_the_stamp_moves_when_a_report_clears_the_quota(client, session,
                                                        host_headers):
    """`reported_at` is when the agent last spoke, and «no quota» was said
    then; a stamp that stayed put would date the absence to the old figure."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quota_five_hour": 40.0}})
    first = float(_person(client, host_headers, "bob")["stats"]["reported_at"])
    time.sleep(0.02)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {}}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert float(stats["reported_at"]) > first
    assert "quota_five_hour" not in stats


def test_an_identity_update_is_not_a_usage_report(client, session, host_headers):
    """`collab color` posts `{"color": …, "stats": {}}`, and a daemon with no
    stats file posts `{"stats": {}}` beside its machine. Neither says a word
    about usage, so neither may be read as «my quota is gone»."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quota_five_hour": 40.0, "model": "x"}})
    first = float(_person(client, host_headers, "bob")["stats"]["reported_at"])
    client.post("/ext/collab/v1/stats", headers=h,
                json={"color": "#008080", "stats": {}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"machine": "bobs-laptop", "stats": {}})

    seen = _person(client, host_headers, "bob")
    assert seen["stats"]["quota_five_hour"] == 40.0, "an identity update wiped the quota"
    assert float(seen["stats"]["reported_at"]) == first, "and moved the stamp"
    assert seen["machine"] == "bobs-laptop" and seen.get("color") == "#008080"


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
