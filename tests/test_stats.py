"""Usage sharing: how it travels, and what it is for.

The point is that an agent can look at who has quota left before handing out
the next task, so the figures have to reach everyone, not just the host.
"""

from __future__ import annotations

import time

import pytest

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


def test_later_stats_merge_and_a_report_without_quotas_leaves_the_quota(
        client, session, host_headers):
    """Non-quota figures merge; the quota changes only when a report carries it.

    This test has changed twice in one day, and the history is the point.

    It began as `test_later_stats_merge_rather_than_replace`: an update never
    drops what it omits, quota included. That let an agent which could no
    longer see a window go on showing its old figure to everybody, and work
    was split on it.

    It became `…_except_the_quota_which_is_replaced`: a report was the whole
    truth about the quota, and one with no quota in it cleared it. That closed
    the first trap and opened a worse one: any tool that reports cost every
    turn but never sees quota — most of them — would keep everyone's quota
    wiped, and the agent could do nothing about it short of never reporting.

    It is now this: a report that does not carry `quotas` says nothing about
    the quota and leaves it; one that does carry `quotas` — even empty — is
    the agent's whole statement and replaces it. An agent that has lost sight
    of its quota says so on purpose, with `collab stats --clear-quota`, and
    the whole-picture routes (the status line, the usage command) carry
    `quotas: {}` for it when their payload has none.
    """
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "a", "stats": {"model": "Opus 5", "cost_usd": 1.0,
                                             "quotas": {"five_hour": {"used_pct": 40}}}})
    client.post("/ext/collab/v1/messages", headers=h,
                json={"text": "b", "stats": {"cost_usd": 2.0}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["cost_usd"] == 2.0
    assert stats["model"] == "Opus 5", "an update must not drop the model it omits"
    assert stats["quotas"] == {"five_hour": {"used_pct": 40.0}}, \
        "nor the quota it says nothing about"


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
    everything else that agent had shared. For one afternoon the quota was an
    exception — a report without it cleared it — and that turned every
    cost-only report into a wipe; see the test above. A report that carries no
    `quotas` leaves the quota as it was.
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
    assert stats["quota_five_hour"] == 73.0, "the quota included"


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


# --- the quota changes only when a report carries `quotas` -------------------
#
# Windows used to merge one at a time, so that an agent reporting only its
# five-hour window did not erase the weekly one; an agent that could no longer
# see a window then kept showing its old figure to everyone. For one afternoon
# the rule was the reverse — a report was the whole truth, and one with no
# quota cleared it — which meant any tool reporting cost every turn and never
# seeing quota kept everybody's quota wiped. The rule now: a report that
# CARRIES `quotas` (even empty) replaces the stored quota with exactly that;
# one that does not carry it leaves the quota alone. Losing sight of a quota
# is said on purpose: `collab stats --clear-quota`.

def test_a_cost_only_report_leaves_the_quota_for_everyone(client, session,
                                                          host_headers):
    """The case that flipped: a report without `quotas` is not about the quota."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "quotas": {"five_hour": {"used_pct": 55, "resets_at": "SOON"},
                   "seven_day": {"used_pct": 20}},
        "quota_five_hour": 55, "quota_seven_day": 20}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"cost_usd": 2.0}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"model": "x"}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"tokens_in": 10}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert set(stats["quotas"]) == {"five_hour", "seven_day"}, stats
    assert stats["quotas"]["five_hour"]["resets_at"] == "SOON"
    assert stats["quota_five_hour"] == 55.0 and stats["quota_seven_day"] == 20.0
    assert stats["cost_usd"] == 2.0 and stats["model"] == "x" and stats["tokens_in"] == 10
    carol = _join(client, session, "carol")
    assert set(_person(client, _headers(carol), "bob")["stats"]["quotas"]) == \
        {"five_hour", "seven_day"}


def test_an_empty_quotas_map_clears_the_quota(client, session, host_headers):
    """`quotas: {}` is the statement «I have no quota to report». Every quota
    field goes — the flat ones too — and nothing else does."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "model": "gpt-5", "cost_usd": 1.0,
        "quota_five_hour": 73, "quota_seven_day": 12,
        "quota_used_pct": 73, "quota_reset_at": "SOON",
        "quotas": {"five_hour": {"used_pct": 73}, "seven_day": {"used_pct": 12}}}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {}}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert not any(key.startswith("quota") for key in stats), stats
    assert stats["model"] == "gpt-5" and stats["cost_usd"] == 1.0
    carol = _join(client, session, "carol")
    assert "quotas" not in _person(client, _headers(carol), "bob")["stats"]


def test_a_map_naming_one_window_is_the_whole_statement(client, session,
                                                        host_headers):
    """Report five-hour and weekly, then a map with only five-hour: the weekly
    window is gone, the flat weekly figure with it, and the five-hour window
    is what was said — not the old reset merged back in."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "quotas": {"five_hour": {"used_pct": 55, "resets_at": "SOON"},
                   "seven_day": {"used_pct": 20}},
        "quota_five_hour": 55, "quota_seven_day": 20}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "quotas": {"five_hour": {"used_pct": 60}}, "quota_five_hour": 60}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["quotas"] == {"five_hour": {"used_pct": 60.0}}, stats
    assert stats["quota_five_hour"] == 60.0
    assert "quota_seven_day" not in stats, "the flat figure goes with its window"


def test_a_window_update_refreshes_that_window(client, session, host_headers):
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 10}}}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 90}}}})
    windows = _person(client, host_headers, "bob")["stats"]["quotas"]
    assert windows["five_hour"]["used_pct"] == 90.0


def test_the_model_survives_a_quota_only_report(client, session, host_headers):
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"model": "Opus 5", "cost_usd": 1.0}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 10}}}})
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"quotas": {}}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["model"] == "Opus 5" and stats["cost_usd"] == 1.0
    assert "quotas" not in stats


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
    about usage, so neither moves the stamp or touches the quota."""
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


@pytest.mark.parametrize("junk", ["lots", ["five_hour"], None, 42])
def test_a_quotas_that_is_not_a_map_is_not_a_quota_statement(client, session,
                                                              host_headers, junk):
    """A string, a list, `null` or a number under `quotas` is dropped on the
    way in: it is not published to the roster as an opaque extra, and the
    report is read as one that does not carry `quotas` — the stored quota
    stays, the rest of the report merges."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"used_pct": 40}}}})
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": junk, "cost_usd": 2.0}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["quotas"] == {"five_hour": {"used_pct": 40.0}}, stats
    assert stats["cost_usd"] == 2.0


def _seed_two_windows(client, h):
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "model": "gpt-5",
        "quotas": {"five_hour": {"used_pct": 55, "resets_at": "SOON"},
                   "seven_day": {"used_pct": 20}},
        "quota_five_hour": 55, "quota_seven_day": 20}})


def test_a_bare_number_window_is_a_figure_on_the_wire(client, session,
                                                      host_headers):
    """The wire endpoint runs `sanitise` only, and `sanitise` used to accept
    a narrower set of window shapes than `normalise` — a bare number, or a
    remaining-style key, was dropped. Dropped windows are what turned a
    meaningful map into an empty one, so the shapes are the same now."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    _seed_two_windows(client, h)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": 42}}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["quotas"] == {"five_hour": {"used_pct": 42.0}}, \
        "a bare number is that window's used_pct, and the map it came in replaces"

    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": {"remaining_percentage": 58}}}})
    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["quotas"] == {"five_hour": {"used_pct": 42.0}}


@pytest.mark.parametrize("junk", ["lots", None, {"note": "x"}, [1, 2], {}])
def test_a_map_of_junk_windows_is_noise_and_not_a_clear(client, session,
                                                        host_headers, junk):
    """`{}` is a statement; `{"five_hour": "lots"}` is noise. A non-empty map
    from which no window survives must not come out as `{}` and wipe the
    stored quota for everyone — the reviewer's live reproduction. Nothing
    usable arrived, so the whole body is a non-report: the quota stays, the
    stamp stays."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    _seed_two_windows(client, h)
    before = _person(client, host_headers, "bob")["stats"]
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": junk}}})

    after = _person(client, host_headers, "bob")["stats"]
    assert after["quotas"] == before["quotas"], after
    assert after["quota_seven_day"] == 20.0 and after["quota_five_hour"] == 55.0
    assert after["reported_at"] == before["reported_at"], "nothing usable was said"
    carol = _join(client, session, "carol")
    assert set(_person(client, _headers(carol), "bob")["stats"]["quotas"]) == \
        {"five_hour", "seven_day"}


def test_a_junk_map_beside_a_real_figure_merges_the_figure_only(
        client, session, host_headers):
    """The cost is a report; the junk map is not a quota statement."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    _seed_two_windows(client, h)
    before = _person(client, host_headers, "bob")["stats"]
    time.sleep(0.02)
    client.post("/ext/collab/v1/stats", headers=h,
                json={"stats": {"quotas": {"five_hour": "lots"}, "cost_usd": 2.0}})

    after = _person(client, host_headers, "bob")["stats"]
    assert after["quotas"] == before["quotas"]
    assert after["cost_usd"] == 2.0
    assert float(after["reported_at"]) > float(before["reported_at"])


def test_one_good_window_beside_junk_is_the_whole_statement(client, session,
                                                            host_headers):
    """A map with something usable in it is a real map: the good window is
    kept, the junk one is dropped, and the map replaces the stored one — the
    weekly window it did not name usably is gone."""
    bob = _join(client, session, "bob")
    h = _headers(bob)
    _seed_two_windows(client, h)
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {
        "quotas": {"five_hour": {"used_pct": 60}, "seven_day": "lots"}}})

    stats = _person(client, host_headers, "bob")["stats"]
    assert stats["quotas"] == {"five_hour": {"used_pct": 60.0}}, stats
    assert "quota_seven_day" not in stats


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
