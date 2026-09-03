"""Reading usage from whatever agent the other person happens to run.

Most agents expose nothing a shell script can reach, so the canonical shape and
`collab stats --report` are the contract. These check the translations, and in
particular that "how much is left" never gets read as "how much is used".
"""

from __future__ import annotations

import pytest

from collab.stats import CANONICAL, normalise, sanitise


def test_the_flat_canonical_shape_passes_through():
    """The whole integration for any agent: emit these keys.

    The flat quota figure is also a window now, because the roster and
    `collab stats` draw the `quotas` map and nothing else — the documented
    one-liner was stored and never drawn, on both sides of the day the merge
    rule changed twice. `quota_five_hour: 42` IS `quotas.five_hour.used_pct`.
    """
    got = normalise({"model": "gpt-5", "quota_five_hour": 42, "cost_usd": 1.5})
    assert got["model"] == "gpt-5" and got["cost_usd"] == 1.5
    assert got["quota_five_hour"] == 42.0
    assert got["quotas"] == {"five_hour": {"used_pct": 42.0}}


def test_the_one_liner_is_drawn_where_the_quota_is_read():
    """On the roster row and in `collab stats`, which both read the map."""
    from collab.cli import _stat_bits
    from collab.client.tui import stat_line
    from collab.stats import quota_summary

    stats = normalise('{"model":"gpt-5","quota_five_hour":73}')
    assert quota_summary(stats) == "quota 5h 73%"
    assert "quota 5h 73%" in stat_line({"name": "bob", "stats": stats})
    assert any(b.startswith("quota 5h 73%") for b in _stat_bits({"stats": stats}))


def test_both_flat_windows_become_windows():
    got = normalise({"quota_five_hour": 73, "quota_seven_day": 12})
    assert got["quotas"] == {"five_hour": {"used_pct": 73.0},
                             "seven_day": {"used_pct": 12.0}}


def test_a_reset_alone_attaches_to_the_only_window():
    """`quota_reset_at` names no window, so it can only be one window's."""
    got = normalise({"quota_five_hour": 73, "quota_reset_at": "SOON"})
    assert got["quotas"] == {"five_hour": {"used_pct": 73.0, "resets_at": "SOON"}}
    two = normalise({"quota_five_hour": 73, "quota_seven_day": 12,
                     "quota_reset_at": "SOON"})
    assert not any("resets_at" in w for w in two["quotas"].values()), \
        "with two windows there is no saying whose reset it is"
    assert two["quota_reset_at"] == "SOON", "the flat field still travels"


def test_a_junk_flat_figure_makes_no_window():
    got = normalise({"model": "gpt-5", "quota_five_hour": "lots"})
    assert "quotas" not in got and "quota_five_hour" not in got
    assert got == {"model": "gpt-5"}


def test_the_map_wins_when_the_flat_figure_disagrees():
    """One number per window: the map is the statement and the flat field is
    derived from it, in both directions."""
    got = normalise({"quotas": {"five_hour": {"used_pct": 40}},
                     "quota_five_hour": 99})
    assert got["quotas"] == {"five_hour": {"used_pct": 40.0}}
    assert got["quota_five_hour"] == 40.0


def test_flat_to_map_to_flat_is_stable():
    """What normalise emits, normalise and sanitise both leave alone."""
    once = normalise({"quota_five_hour": 73, "quota_seven_day": 12,
                      "model": "gpt-5"})
    assert normalise(once) == once
    assert normalise(sanitise(once)) == once
    assert sanitise(once)["quotas"] == once["quotas"]


def test_claude_code_status_line_payload():
    got = normalise({
        "model": {"display_name": "Opus 5"},
        "cost": {"total_cost_usd": 1.24, "total_lines_added": 310,
                 "total_lines_removed": 44},
        "rate_limits": {"five_hour": {"used_percentage": 42.3},
                        "seven_day": {"used_percentage": 11.8}},
        "context_window": {"used_percentage": 18.4},
    })
    assert got["model"] == "Opus 5"
    assert got["cost_usd"] == 1.24
    assert got["quota_five_hour"] == 42.3
    assert got["quota_seven_day"] == 11.8
    assert got["context_pct"] == 18.4
    assert got["lines_added"] == 310


def test_remaining_quota_is_inverted_not_copied():
    """Antigravity reports what is *left*.

    Copying it across would say an agent with 58% of its allowance still free
    has burned 58% — the exact opposite, and the figure people use to decide
    who can take on more work.
    """
    got = normalise({"quota": {"remaining_fraction": 0.58}})
    assert got["quota_used_pct"] == 42.0

    assert normalise({"quota": {"remaining_percentage": 90}})["quota_used_pct"] == 10.0
    assert normalise({"quota": {"remaining_fraction": 1.0}})["quota_used_pct"] == 0.0
    assert normalise({"quota": {"remaining_fraction": 0.0}})["quota_used_pct"] == 100.0


def test_a_windowed_remaining_figure_is_inverted_too():
    got = normalise({"rate_limits": {"five_hour": {"remaining_percentage": 75}}})
    assert got["quota_five_hour"] == 25.0


def test_token_counts_from_a_loose_usage_wrapper():
    """What a Codex- or opencode-style reporter would send."""
    got = normalise({"usage": {"input_tokens": 184000, "output_tokens": 22400,
                               "model_name": "gpt-5-codex"}})
    assert got == {"tokens_in": 184000, "tokens_out": 22400, "model": "gpt-5-codex"}


def test_antigravity_token_field_names():
    got = normalise({"context_window": {"total_input_tokens": 1200,
                                        "total_output_tokens": 300,
                                        "used_percentage": 22}})
    assert got["tokens_in"] == 1200 and got["tokens_out"] == 300
    assert got["context_pct"] == 22.0


@pytest.mark.parametrize("given,expected", [
    ({"context_pct": 0.35}, 35.0),   # a fraction
    ({"context_pct": 35}, 35.0),     # already a percentage
    ({"context_pct": 100}, 100.0),
])
def test_fractions_and_percentages_both_work(given, expected):
    assert normalise(given)["context_pct"] == expected


def test_a_json_string_is_accepted():
    assert normalise('{"model": "gpt-5"}') == {"model": "gpt-5"}


@pytest.mark.parametrize("junk", ["", "not json", "[]", "null", '{"nothing": "here"}'])
def test_nothing_recognisable_yields_nothing(junk):
    assert normalise(junk) == {}


def test_unknown_fields_do_not_block_the_rest():
    """A newer agent reporting more than we know still gets its half through."""
    got = normalise({"model": "gpt-6", "some_future_metric": 12})
    assert got == {"model": "gpt-6"}


# --- what reaches everyone else's roster -------------------------------------

def test_sanitise_keeps_canonical_fields():
    assert sanitise({"model": "Opus 5", "quota_five_hour": 42})["quota_five_hour"] == 42.0


def test_sanitise_keeps_an_empty_quotas_map():
    """`{}` under `quotas` is a statement — «no quota» — and reaches the hub
    as one, where it clears; see hub.merge_stats."""
    assert sanitise({"quotas": {}}) == {"quotas": {}}
    assert sanitise({"quotas": {}, "model": "x"}) == {"quotas": {}, "model": "x"}


@pytest.mark.parametrize("junk", ["lots", ["five_hour"], None, 42, True])
def test_sanitise_drops_a_quotas_that_is_not_a_map(junk):
    """A non-dict `quotas` used to slip through as an opaque extra field and
    be published to the whole roster. It is not a quota statement: dropped,
    and the report is one that does not carry `quotas`."""
    out = sanitise({"quotas": junk, "model": "x"})
    assert "quotas" not in out, out
    assert out == {"model": "x"}


def test_sanitise_drops_nested_values():
    """Usage lands on every roster; it stays flat and small."""
    assert "payload" not in sanitise({"payload": {"deep": [1, 2, 3]}})
    assert "listy" not in sanitise({"listy": [1, 2, 3]})


def test_sanitise_caps_unknown_fields():
    noisy = {f"extra_{i}": i for i in range(50)}
    assert len(sanitise(noisy)) <= 6


def test_sanitise_truncates_long_strings():
    assert len(sanitise({"model": "x" * 5000})["model"]) <= 64


def test_every_canonical_field_survives_a_round_trip():
    sample = {"model": "m", "cost_usd": 1.0, "quota_used_pct": 5.0,
              "quota_five_hour": 6.0, "quota_seven_day": 7.0,
              "quota_reset_at": "2026-09-01T00:00Z", "context_pct": 8.0,
              "tokens_in": 9, "tokens_out": 10, "lines_added": 11,
              "lines_removed": 12}
    assert set(sample) == set(CANONICAL), "a new field needs a test"
    got = sanitise(normalise(sample))
    for key, value in sample.items():
        assert got[key] == value, key
    # And the two flat windows came through as windows; the reset stays flat
    # because two windows leave it nobody's.
    assert got["quotas"] == {"five_hour": {"used_pct": 6.0},
                             "seven_day": {"used_pct": 7.0}}
    assert set(got) == set(sample) | {"quotas"}


# --- every quota window, not a fixed two -------------------------------------

def test_all_three_claude_code_windows_survive():
    """The spend limit was being dropped entirely."""
    got = normalise({"rate_limits": {
        "five_hour": {"used_percentage": 42.3, "resets_at": "2026-09-01T14:00:00Z"},
        "seven_day": {"used_percentage": 11.8, "resets_at": "2026-09-05T00:00:00Z"},
        "spend_limit": {"used_percentage": 30.0, "resets_at": "2026-10-01T00:00:00Z"},
    }})
    assert set(got["quotas"]) == {"five_hour", "seven_day", "spend_limit"}
    assert got["quotas"]["spend_limit"]["used_pct"] == 30.0


def test_each_window_keeps_its_own_reset():
    """One shared reset cannot say *which* window rolls over in ten minutes."""
    got = normalise({"rate_limits": {
        "five_hour": {"used_percentage": 90, "resets_at": "SOON"},
        "seven_day": {"used_percentage": 10, "resets_at": "LATER"},
    }})
    assert got["quotas"]["five_hour"]["resets_at"] == "SOON"
    assert got["quotas"]["seven_day"]["resets_at"] == "LATER"


def test_an_agent_can_report_windows_we_have_never_heard_of():
    """The list of windows keeps growing; unknown ones must not be dropped."""
    got = normalise({"quotas": {"requests_per_minute": {"used_pct": 5},
                                "monthly": {"used_pct": 60}}})
    assert got["quotas"]["requests_per_minute"]["used_pct"] == 5.0
    assert got["quotas"]["monthly"]["used_pct"] == 60.0


def test_the_old_flat_fields_are_still_emitted():
    """Anything reading quota_five_hour keeps working."""
    got = normalise({"rate_limits": {"five_hour": {"used_percentage": 42},
                                     "seven_day": {"used_percentage": 11}}})
    assert got["quota_five_hour"] == 42.0
    assert got["quota_seven_day"] == 11.0


def test_the_old_flat_input_still_works():
    got = normalise({"quota_five_hour": 73})
    assert got["quota_five_hour"] == 73.0


def test_remaining_is_inverted_per_window_too():
    got = normalise({"rate_limits": {"weekly": {"remaining_percentage": 75}}})
    assert got["quotas"]["seven_day"]["used_pct"] == 25.0


def test_window_names_are_normalised():
    got = normalise({"quotas": {"7d": {"used_pct": 1}, "opus_weekly": {"used_pct": 2}}})
    assert "seven_day" in got["quotas"]
    assert "seven_day_opus" in got["quotas"]


def test_the_number_of_windows_is_capped():
    """A roster line is not a dashboard."""
    many = {"quotas": {f"w{i}": {"used_pct": i} for i in range(40)}}
    assert len(sanitise(normalise(many))["quotas"]) <= 8


def test_sanitise_keeps_the_map_but_drops_junk_inside_it():
    dirty = {"quotas": {"five_hour": {"used_pct": 42, "resets_at": "x",
                                      "nested": {"no": 1}}}}
    kept = sanitise(dirty)["quotas"]["five_hour"]
    assert kept == {"used_pct": 42.0, "resets_at": "x"}


def test_the_busiest_window_is_shown_first():
    """The one that will actually stop you is the one you are looking for."""
    from collab.stats import quota_summary

    line = quota_summary({"quotas": {
        "five_hour": {"used_pct": 42}, "spend_limit": {"used_pct": 88},
        "seven_day": {"used_pct": 12}}})
    assert line.index("spend") < line.index("5h") < line.index("7d")


def test_a_single_figure_agent_still_renders():
    from collab.stats import quota_summary

    assert "42%" in quota_summary({"quota_used_pct": 42.0})
    assert quota_summary({}) == ""
