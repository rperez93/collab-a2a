"""A share written as `1` is one percent, whichever tool wrote it.

Sources disagree on whether a share is `0.42` or `42`, and for every value but
one the number says which. At exactly 1 the two readings are 1 % and 100 %,
and the rule «0..1 is a fraction» chose 100 % — so a Codex account at 1 % of
its week, which is what a fresh week looks like, was published to the whole
session as spent and would have been handed nothing. Read from the real
app-server on 2026-09-08: `usedPercent: 1`, drawn as `quota 7d 100%`. The
mirror was wrong too: `remaining_percentage: 1` — one percent LEFT — was read
as a whole fraction remaining and published as 0 % used.

The direction was never the problem. Codex's own schema names the field
`usedPercent`, int32, and collab reads it as used. The spelling was: an
integer is a count of percent, a key that says `fraction` or `percentage`
says which it is, and only a float from an unnamed key is judged by its range.
"""

from __future__ import annotations

import pytest

from collab import quotas
from collab.stats import normalise, quota_summary, sanitise

#: What `codex app-server` answered on 2026-09-08, minus the account id. A
#: Pro account in a fresh week: one percent of the weekly window used, the
#: Spark bucket untouched.
CODEX_ANSWER = {
    "rateLimits": {
        "limitId": "codex", "limitName": None,
        "primary": {"usedPercent": 1, "windowDurationMins": 10080,
                    "resetsAt": 1789435412},
        "secondary": None, "planType": "pro"},
    "rateLimitsByLimitId": {
        "codex_bengalfox": {
            "limitId": "codex_bengalfox", "limitName": "GPT-5.3-Codex-Spark",
            "primary": {"usedPercent": 0, "windowDurationMins": 300,
                        "resetsAt": 1788859099},
            "secondary": {"usedPercent": 0, "windowDurationMins": 10080,
                          "resetsAt": 1789445899}},
        "codex": {
            "limitId": "codex", "limitName": None,
            "primary": {"usedPercent": 1, "windowDurationMins": 10080,
                        "resetsAt": 1789435412},
            "secondary": None}}}


def test_what_codex_actually_answered_is_one_percent_all_the_way_through():
    """The probe, the file route, the wire route and the drawing, on the real
    answer. Before: `quota 7d 100%` from a window Codex said was 1 % used."""
    probe = quotas.codex_quotas(CODEX_ANSWER)
    assert probe["seven_day"]["used_pct"] == 1
    assert normalise({"quotas": probe})["quotas"]["seven_day"]["used_pct"] == 1.0
    assert sanitise({"quotas": probe})["quotas"]["seven_day"]["used_pct"] == 1.0
    assert quota_summary(normalise({"quotas": probe})) == \
        "quota 7d 1% · bengalfox 5h 0% · bengalfox 7d 0%"


def test_codex_s_integer_is_kept_an_integer_by_the_probe():
    """`usedPercent` is int32 in Codex's schema. `float(1)` in the probe was
    what handed the reader a `1.0` to multiply."""
    got = quotas.codex_quotas(CODEX_ANSWER)["seven_day"]["used_pct"]
    assert got == 1 and isinstance(got, int)


@pytest.mark.parametrize("given, used", [
    ({"used_pct": 1}, 1.0),                    # collab's own key: a percent as written
    ({"used_pct": 100}, 100.0),
    ({"used_pct": 0.42}, 0.42),                # … even below one
    ({"used_pct": 1.0}, 1.0),
    ({"used_pct": 42.5}, 42.5),
    ({"used": 1}, 1.0),                        # a foreign key: an integer is a count of percent
    ({"used": 0.42}, 42.0),                    # a float inside (0, 1] is a fraction
    ({"used": 1.0}, 100.0),                    # … including all of it
    ({"used_percentage": 1.0}, 1.0),           # the key says percentage: as written
    ({"remaining_percentage": 1}, 99.0),       # one percent LEFT
    ({"remaining_percentage": 100}, 0.0),
    ({"remaining_fraction": 1.0}, 0.0),        # the key says fraction: all left
    ({"remaining_fraction": 0.58}, 42.0),
    ({"remaining_fraction": 1}, 0.0),          # an integer under a fraction key is still a fraction
])
def test_every_spelling_of_a_window_s_share(given, used):
    assert normalise({"quotas": {"five_hour": given}})["quotas"]["five_hour"]["used_pct"] == used


def test_the_flat_fields_read_one_as_one_percent_too():
    """The documented one-liner, `quota_five_hour: 73`, is an integer percent.
    So is `quota_five_hour: 1`, which used to be 100."""
    assert normalise({"quota_five_hour": 1})["quota_five_hour"] == 1.0
    assert normalise({"quotas": {"five_hour": 1}})["quotas"]["five_hour"]["used_pct"] == 1.0


def test_a_full_context_window_written_as_a_fraction_still_compacts():
    """The context share drives compaction. A source that writes fractions
    under its own key writes `1.0` for the whole window, and reading that as
    1 % would never compact. Under collab's own key it is a percent as written."""
    assert normalise({"context": 1.0})["context_pct"] == 100.0
    assert normalise({"context": 0.35})["context_pct"] == 35.0
    assert normalise({"context_pct": 1.0})["context_pct"] == 1.0
    # And Claude Code's own key, which says percentage, is read as it is.
    assert normalise({"context_window": {"used_percentage": 1.0}})["context_pct"] == 1.0


@pytest.mark.parametrize("report", [
    '{"quotas": {"seven_day": {"used_pct": 1}}}',       # what the probe prints
    '{"quota_used_pct": 1}',                             # the documented one-liner
    '{"quota": {"remaining_percentage": 99}}',           # one percent used, said as what is left
    '{"context_window": {"used_percentage": 0.5}}',      # half a percent of context
    '{"quota_five_hour": 0.5}',                          # half a percent, said as collab's own key
])
def test_what_the_agent_settled_is_what_the_hub_publishes(report):
    """THE SEAM. Every report crosses the reader twice: `normalise` on the
    agent's side, then — after the file, the daemon and the wire — `sanitise`
    on the hub's. The first pass settled `1` as one percent and stored `1.0`;
    the second read that `1.0` as the whole and published 100 % to every other
    participant. The agent's own `collab stats` was right. Nobody else's was.
    No test composed the two passes until this one."""
    import json
    settled = normalise(report)
    published = sanitise(json.loads(json.dumps(settled)))
    for field in ("quota_used_pct", "quota_five_hour", "context_pct"):
        if field in settled:
            assert published[field] == settled[field], (field, settled, published)
    if "quotas" in settled:
        assert published["quotas"] == settled["quotas"]


@pytest.mark.parametrize("key, given, used", [
    ("quota_five_hour", '"1"', 1.0),      # collab's own key, quoted: as written
    ("quota_five_hour", '" 1 "', 1.0),
    ("quota_five_hour", '"42"', 42.0),
    ("quota_five_hour", '"0.42"', 0.42),
    ("quota_five_hour", '"1.0"', 1.0),
    ("five_hour", '"1"', 1.0),            # a foreign key, quoted: an integer literal is an integer
    ("five_hour", '"0.42"', 42.0),        # … and a fraction keeps its point
    ("five_hour", '"1.0"', 100.0),
])
def test_a_quoted_integer_is_still_an_integer(key, given, used):
    """A `--source` script that prints `{"five_hour": "$PCT"}` sends the figure
    quoted. `"1"` is not an `int` instance, so the type rule missed it and
    `float("1")` was read as the whole. The digits decide, not the quoting."""
    assert normalise('{"%s": %s}' % (key, given))["quota_five_hour"] == used


@pytest.mark.parametrize("junk", ["²", "³", "①", "٣x", "", " ", "+", "-"])
def test_a_string_that_is_not_a_figure_never_raises(junk):
    """`"²".isdigit()` is true and `int("²")` raises, and that call ran outside
    the guard — so one superscript in a window's figure, posted by any
    participant to the hub's stats endpoint, raised out of `sanitise`. Junk is
    not a figure, and not a figure never raises."""
    assert sanitise({"quotas": {"five_hour": {"remaining_percentage": junk}}}) == {}
    assert normalise({"remaining_fraction": junk}) == {}
    assert normalise({"quota_five_hour": junk}) == {}


def test_a_boolean_is_not_a_share():
    """`True` is an int to Python and would read as 1 %."""
    assert "quotas" not in normalise({"quotas": {"five_hour": True}})
