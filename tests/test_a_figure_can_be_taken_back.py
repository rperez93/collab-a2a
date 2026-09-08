"""A field an agent can no longer report is removed, not left standing.

Everything that is not quota MERGES at the hub, deliberately: an agent learns
its figures one at a time and a report carrying one says nothing about the
others. The cost of that rule was that a figure once reported stood for the
life of the session however wrong it had become, because there was no value
meaning «none» — omitting the field said nothing about it, and every other
value was a claim.

Found on 2026-09-07: a Codex participant showed `Opus 5` as its model on
everybody's roster. It reports no model at all; the string was written weeks
earlier by the agent whose state directory it had reused. Nothing the agent
could send would take it down, and the honest answers — invent a model, or
leave the lie up — are both wrong.

`null` is the value that means none. It is the merging half of the rule the
quota already had in `--clear-quota`: losing sight of a figure is said on
purpose, never guessed at from silence.
"""

from __future__ import annotations

from collab.stats import CANONICAL, QUOTA_FIELDS, normalise, sanitise


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


def test_null_is_not_the_word_none():
    """`str(None)` is `"None"`, and every text field coerced through it — so
    the erase read as a model called «None» on everybody's roster."""
    assert normalise({"model": None})["model"] is None
    assert sanitise({"model": None})["model"] is None


def test_an_erase_survives_sanitising():
    """Alone among the things that coerce to nothing. It has to reach the hub
    to do anything at all."""
    got = sanitise({"model": None, "cost_usd": 2.5, "something_odd": None})
    assert got["model"] is None
    assert got["cost_usd"] == 2.5
    assert got["something_odd"] is None


def test_a_null_quota_still_says_nothing():
    """`--clear-quota` posts an empty MAP, and that is the only spelling. A
    `quotas: null` is a report that is not about the quota, as it always was."""
    assert "quotas" not in sanitise({"quotas": None})


def test_the_hub_drops_a_field_an_agent_takes_back(client, session, host_headers):
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"model": "Opus 5", "cost_usd": 1.24}})
    assert _person(client, host_headers, "bob")["stats"]["model"] == "Opus 5"

    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"model": None}})
    seen = _person(client, host_headers, "bob")["stats"]
    assert "model" not in seen, "the residue is gone, not blanked"
    # And nothing else went with it: an erase is about the field it names.
    assert seen["cost_usd"] == 1.24


def test_an_erase_is_still_the_agent_speaking(client, session, host_headers):
    """It moves `reported_at`, like every other report. «I have no model» is
    something the agent said just now."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"model": "Opus 5"}})
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"model": None}})
    assert _person(client, host_headers, "bob")["stats"].get("reported_at")


def test_a_quota_is_not_erased_by_a_null_beside_it(client, session, host_headers):
    """The quota's own rule is untouched: only a `quotas` map replaces it."""
    bob = _join(client, session, "bob")
    client.post("/ext/collab/v1/stats", headers=_headers(bob), json={
        "stats": {"model": "Opus 5", "quotas": {"five_hour": {"used_pct": 42}}}})
    client.post("/ext/collab/v1/stats", headers=_headers(bob),
                json={"stats": {"model": None}})
    seen = _person(client, host_headers, "bob")["stats"]
    assert seen["quotas"]["five_hour"]["used_pct"] == 42.0
    assert "model" not in seen


def test_every_field_but_the_quota_can_be_taken_back():
    """Not just the model — any of them can be residue from a reused seat. The
    quota is the exception, and it is the quota's own rule rather than a gap:
    it is stated by the `quotas` map and erased by `--clear-quota`."""
    got = sanitise({field: None for field in CANONICAL})
    assert set(got) == set(CANONICAL) - set(QUOTA_FIELDS)
    assert all(value is None for value in got.values())


def test_a_null_on_a_quota_field_is_ignored_rather_than_taken():
    """Taken, it was worse than a contradiction. `field in out` became true for
    a null, so the derivation injected `five_hour: {used_pct: null}` as a
    window, `--report` REPLACED the agent's own stats file with that one junk
    window, and the sanitiser then dropped it — so nothing carried `quotas` and
    the hub kept the stale figure indefinitely, which is work split on a number
    nobody had reported."""
    assert normalise('{"quota_five_hour": null}') == {}
    assert sanitise({"quota_five_hour": None, "quota_seven_day": None}) == {}
    # Beside a real figure the erase is dropped and the figure is kept.
    got = normalise({"model": None, "quota_five_hour": 42})
    assert got["model"] is None
    assert got["quotas"] == {"five_hour": {"used_pct": 42.0}}
