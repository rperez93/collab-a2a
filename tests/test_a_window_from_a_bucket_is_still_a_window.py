"""A quota window keeps its LENGTH on the roster, whatever allowance it is in.

Codex names a model bucket's windows `<limit id>_<window>` —
`codex_bengalfox_five_hour` — because the limit id is the only thing that tells
two allowances of the same length apart. Two things read that key, and both of
them used to read only the first half of it:

* the label. It trimmed from the left against a fourteen-column budget, so the
  budget went on the opaque id and the length was dropped. A Codex account
  whose five-hour allowance lives in a bucket drew «quota codex 40% · codex
  12% · codex 3%»: three identical labels, no length anywhere, and nothing on
  the row to say which figure the next task should be split on.
* the flat `quota_five_hour`, derived only from a key spelled `five_hour`
  exactly — so on the same account it was never derived at all, and the
  documented one-liner integration read nothing back.

Both were reported from a live session against a real Codex participant on
2026-09-07.
"""

from __future__ import annotations

from collab import quotas
from collab.stats import (normalise, quota_summary, split_window, window_label,
                          window_labels)


def test_a_bucket_window_keeps_the_length_and_loses_the_id():
    """The length is what a reader wants; the id is what is trimmed for it."""
    assert split_window("codex_bengalfox_five_hour") == (
        "codex_bengalfox", "five_hour", 0)
    # And the label keeps the length whole, spending what is left on the tail
    # of the id — the half that separates `codex` from `codex_bengalfox`.
    assert window_label("codex_bengalfox_five_hour") == "bengalfox 5h"


def test_the_longest_tail_that_names_a_window_wins():
    """`seven_day_opus` is one window's name, not a day window in an opus
    bucket, so the split may not stop at the first tail that matches."""
    assert split_window("codex_seven_day_opus") == ("codex", "seven_day_opus", 0)
    assert split_window("seven_day") == ("", "seven_day", 0)


def test_a_number_at_the_end_is_a_copy_only_when_the_rest_is_a_window():
    """`quotas._free` appends `_2` when two limit ids reduce to one slug. A
    trailing digit that is part of a name is not that."""
    assert split_window("a_b_five_hour_2") == ("a_b", "five_hour", 2)
    assert split_window("daily_2") == ("", "daily", 2)
    # And a digit that is part of the name keeps its place: the guard is that
    # what is LEFT still names a window, not that a digit is at the end.
    assert split_window("gpt_5") == ("", "gpt_5", 0)
    assert split_window("30_day") == ("", "30_day", 0)


def test_an_id_every_window_shares_distinguishes_nothing_and_is_dropped():
    """One bucket, so the row reads like everybody else's."""
    got = quota_summary({"quotas": {
        "codex_bengalfox_five_hour": {"used_pct": 40.0},
        "codex_bengalfox_seven_day": {"used_pct": 12.0},
        "codex_bengalfox_3_hour": {"used_pct": 3.0}}})
    assert got == "quota 5h 40% · 7d 12% · 3h 3%"


def test_two_allowances_of_one_length_are_told_apart():
    """The whole reason the id is in the key. Dropping it here would report one
    figure where there are two."""
    got = quota_summary({"quotas": {
        "five_hour": {"used_pct": 40.0},
        "codex_bengalfox_five_hour": {"used_pct": 12.0}}})
    assert got == "quota 5h 40% · bengalfox 5h 12%"


def test_no_two_windows_on_a_row_ever_draw_the_same_label():
    """A collision gives both of them their whole key back. A clipped key is
    ugly; two windows wearing one name is wrong."""
    labels = window_labels(["seven_day", "weekly"])
    assert len(set(labels.values())) == 2
    assert labels["weekly"] == "weekly"


def test_a_length_collab_has_no_word_for_still_reads_as_a_length():
    assert window_label("90_minute") == "90m"
    assert window_label("30_day") == "30d"


def test_the_flat_field_is_derived_from_the_window_s_length():
    """`quota_five_hour` for an account whose five-hour window is in a bucket.
    Every reader of the older flat form saw nothing for such an agent."""
    got = normalise({"quotas": {
        "codex_bengalfox_five_hour": {"used_pct": 40.0},
        "codex_bengalfox_seven_day": {"used_pct": 12.0}}})
    assert got["quota_five_hour"] == 40.0
    assert got["quota_seven_day"] == 12.0
    # The map is untouched: the key still names the allowance it came from.
    assert set(got["quotas"]) == {"codex_bengalfox_five_hour",
                                  "codex_bengalfox_seven_day"}


def test_two_windows_of_one_length_derive_no_flat_field():
    """It would have to pick one and could not say which. The map says both."""
    got = normalise({"quotas": {
        "codex_bengalfox_five_hour": {"used_pct": 40.0},
        "other_five_hour": {"used_pct": 9.0}}})
    assert "quota_five_hour" not in got
    assert len(got["quotas"]) == 2


def test_the_whole_way_through_from_what_codex_answers():
    """The shape measured from `codex-cli` on 2026-09-07, on an account with no
    account-level `rateLimits` — which is the account the defect was found on:
    every window it has carries a bucket prefix."""
    answer = {"rateLimitsByLimitId": {"codex_bengalfox": {
        "limitId": "codex_bengalfox",
        "primary": {"usedPercent": 40, "windowDurationMins": 300},
        "secondary": {"usedPercent": 12, "windowDurationMins": 10080}}}}
    figures = normalise({"quotas": quotas.codex_quotas(answer)})
    assert figures["quota_five_hour"] == 40.0
    assert quota_summary(figures) == "quota 5h 40% · 7d 12%"


def test_a_key_too_long_to_keep_loses_the_allowance_id_not_the_window():
    """`quotas._slug` allows a forty-character limit id, so `<id>_seven_day_opus`
    is fifty-five characters against a thirty-two character cap. Cut from the
    right, the length left the datum altogether and no label could recover it."""
    from collab.stats import MAX_WINDOW_KEY, _window_name

    key = _window_name("codex_" + "a" * 34 + "_seven_day_opus")
    assert len(key) <= MAX_WINDOW_KEY
    assert split_window(key)[1] == "seven_day_opus"
    assert window_label(key).endswith("7d opus")


def test_resolving_one_collision_may_not_open_another():
    """A key handed back can land where another window's label already was, so
    the pass repeats until nothing collides rather than running once."""
    labels = window_labels(["7d", "weekly", "seven_day"])
    assert len(set(labels.values())) == 3


def test_the_windows_everybody_else_has_are_labelled_as_they_were():
    """The change is about bucket-prefixed keys. Nothing an ordinary agent
    reports may read differently because of it."""
    got = quota_summary({"quotas": {
        "five_hour": {"used_pct": 1}, "seven_day": {"used_pct": 2},
        "spend_limit": {"used_pct": 3}, "credits": {"used_pct": 4},
        "daily": {"used_pct": 5}, "monthly": {"used_pct": 6},
        "seven_day_opus": {"used_pct": 7}}})
    assert got == ("quota 7d opus 7% · 30d 6% · 24h 5% · credits 4% · "
                   "spend 3% · 7d 2% · 5h 1%")


def test_a_window_name_nobody_has_a_word_for_is_left_alone():
    """No split, so the old word-trim still answers — `requests per` reads as a
    name where `requests pe` reads as a mistake."""
    assert window_label("requests_per_minute") == "requests per"


def test_an_id_cut_short_says_it_was_cut():
    """An allowance id is an opaque codename with no words to stop at, so
    `bengalfox` cut to `bengalfo` reads as a different codename rather than as
    a shortening — on the one row whose job is telling two allowances apart."""
    assert window_label("codex_bengalfox_five_hour_2") == "bengalf… 5h #2"


def test_a_sibling_settles_a_window_the_grammar_cannot_parse():
    """`requests_per_minute` is outside `_names_a_window`'s grammar, so the key
    cannot be split and the id ate the whole budget: `codex 12%`, verbatim the
    failure this change removes, on a row where the window beside it drew
    correctly. Another window on the row names the allowance, and a key
    starting with that id is that allowance's — evidence off the row rather
    than a guess at where an opaque name ends."""
    assert quota_summary({"quotas": {
        "codex_bengalfox_five_hour": {"used_pct": 40},
        "codex_bengalfox_requests_per_minute": {"used_pct": 12}}}) == \
        "quota 5h 40% · requests per 12%"


def test_with_no_sibling_to_learn_from_the_old_trim_still_answers():
    """The limit of the fix, recorded rather than left to be rediscovered.

    Alone on a row, nothing distinguishes `<opaque id>_requests_per_minute`
    from a four-word window name, so the left trim stands and the length is
    lost — exactly as on `origin/main`, verified against it. The claims in the
    module and in the docs are narrowed to match rather than the behaviour
    being guessed at."""
    assert window_label("codex_bengalfoxxxxxx_requests_per_minute") == "codex"
    # And the reading that motivates keeping the left trim: a real multi-word
    # window name is still readable.
    assert window_label("requests_per_minute") == "requests per"


def test_two_ids_the_cap_shortens_into_one_key_are_still_two_windows():
    """The cap keeps the window and shortens the allowance id, which leaves 22
    characters of id in front of a `five_hour` tail — so two limit ids differing
    only after that reduce to one key and the reader storing them would drop a
    window without saying so. That is the failure `quotas._free` exists to
    prevent one layer down; `_free_window` is the same answer at this one."""
    a = "org_abc123_model_gpt_5_codex_high_five_hour"
    b = "org_abc123_model_gpt_5_codex_low_five_hour"
    from collab.stats import collect_quotas, sanitise

    got = collect_quotas({"quotas": {a: {"used_pct": 11}, b: {"used_pct": 77}}})
    assert len(got) == 2, got
    assert sorted(f["used_pct"] for f in got.values()) == [11.0, 77.0]
    # And on the wire, which is a different reader with the same hazard.
    assert len(sanitise({"quotas": {a: {"used_pct": 11},
                                    b: {"used_pct": 77}}})["quotas"]) == 2


def test_two_spellings_of_one_window_are_still_one_window():
    """The dedup above must not split a window reported twice. `Five Hour` and
    `five_hour` are one identity, and the sources are read one after another."""
    from collab.stats import collect_quotas

    got = collect_quotas({"quotas": {"Five Hour": {"used_pct": 40}},
                          "rate_limits": {"five_hour": {"resets_at": "SOON"}}})
    assert list(got) == ["five_hour"]
    assert got["five_hour"]["resets_at"] == "SOON"


def test_a_free_spelling_exists_even_for_a_name_nothing_can_parse():
    """`_free_window` re-caps rather than appends, and a name with no window to
    protect loses the marker to the cap and comes back unchanged — which would
    spin and then overwrite. Room comes off the tail there instead."""
    from collab.stats import _free_window, _window_name

    key = _window_name("x" * 40 + "_requests_per_minute")
    assert _free_window({key: {}}, key) != key


def test_what_every_key_on_the_row_begins_with_is_not_an_allowance_claim():
    """The row settles an unsplittable key by what the keys have in COMMON, and
    not by lending it another window's allowance id.

    That was tried the other way round first: reading
    `codex_bengalotter_burst_limit` as `codex`'s, because `codex` was an id the
    row had shown, named an allowance the key does not belong to — and beside a
    genuine `codex 5h` it put two different allowances under one id, which is
    the error the id in the key exists to prevent."""
    labels = window_labels(["codex_five_hour",
                            "codex_bengalfox_five_hour",
                            "codex_bengalotter_burst_limit"])
    assert labels["codex_five_hour"] == "5h"
    assert labels["codex_bengalfox_five_hour"] == "bengalfox 5h"
    # Not «codex»: the key's own remainder, which claims nothing.
    assert labels["codex_bengalotter_burst_limit"] == "bengalotter"


def test_the_shared_head_never_reaches_into_a_window():
    """`five_hour` and `five_hour_2` share the segment `five`, and sharing it
    away leaves `hour` and `hour_2` — a window's length cut in half to save five
    columns, which is the trim this whole change exists to undo."""
    assert window_labels(["five_hour", "five_hour_2"]) == {
        "five_hour": "5h", "five_hour_2": "5h #2"}


def test_the_wire_reader_merges_two_spellings_like_the_other_one_does():
    """`sanitise` assigned where `collect_quotas` updates, so one window given
    in two spellings — each carrying half of it — was stored with only the
    half that came last. No window is drawn without a `used_pct`, and a
    non-empty map is a whole quota statement, so the 40% went off every roster
    in the session."""
    from collab.stats import sanitise

    got = sanitise({"quotas": {"five_hour": {"used_pct": 40},
                               "5h": {"resets_at": "2026-09-07T10:00:00Z"}}})
    assert got["quotas"] == {"five_hour": {"used_pct": 40.0,
                                           "resets_at": "2026-09-07T10:00:00Z"}}


def test_a_reset_that_is_null_is_not_the_word_none():
    """`str(None)` is `"None"` and `"None"` is truthy, so the guard let it
    through and the panel drew «quota 5h 40% (→None)» — the same defect
    `_coerce` refuses at the top, one function away."""
    from collab.stats import _window_figures

    assert _window_figures({"used_pct": 40, "resets_at": None}) == {"used_pct": 40.0}
    assert quota_summary({"quotas": {"five_hour": {"used_pct": 40,
                                                   "resets_at": None}}},
                         with_resets=True) == "quota 5h 40%"


def test_the_shared_head_never_takes_all_of_a_key():
    """`tokens` beside `tokens_pro_five_hour` had all six of its characters
    taken as «shared», and the roster drew a bare percentage after a double
    space — a figure with no label at all, which reads as a rendering fault
    rather than as a window."""
    labels = window_labels(["tokens", "tokens_pro_five_hour"])
    assert labels["tokens"] == "tokens"
    assert labels["tokens_pro_five_hour"] == "tokens_pro 5h"
    assert quota_summary({"quotas": {"tokens": {"used_pct": 40},
                                     "tokens_pro_five_hour": {"used_pct": 30}}}) == \
        "quota tokens 40% · tokens_pro 5h 30%"


def test_a_window_name_may_begin_part_way_along_a_key():
    """`_split_at` only looks at the tail, so `five_hour_gpt5` — a five-hour
    window of one model — reads as unsplittable, and a row of them had
    `five_hour_` taken off as «shared». That left `gpt5` and `o3` with no length
    between them: the very failure this module exists to remove, reappearing for
    a key written the other way round. Each of them draws its length correctly
    alone, so the row was making its own input worse."""
    assert window_labels(["five_hour_gpt5", "five_hour_o3"]) == {
        "five_hour_gpt5": "five hour gpt5", "five_hour_o3": "five hour o3"}
    assert window_labels(["daily_gpt5", "daily_o3"]) == {
        "daily_gpt5": "daily gpt5", "daily_o3": "daily o3"}


def test_a_doubled_underscore_does_not_desynchronise_the_strip():
    """`quotas._slug` turns each space into an underscore, so a limit id with
    two spaces in it arrives as `a__b`. The strip counts segments and slices
    characters, and those two have to agree."""
    assert window_labels(["a__b_five_hour", "a__b_seven_day"]) == {
        "a__b_five_hour": "5h", "a__b_seven_day": "7d"}


def test_no_window_on_any_row_is_ever_drawn_without_a_label():
    """`"codex_".split("_")` is `['codex', '']`, so keeping one SEGMENT back
    keeps back no characters: the shared head reconstructed the whole key and
    the roster drew a bare percentage with no label at all. The floor is
    checked on the characters that are actually sliced."""
    for row in (["codex_", "codex_five_hour"],
                ["a__", "a__b_five_hour"],
                ["tokens_", "tokens_pro_five_hour"],
                ["x_", "x_y_"]):
        labels = window_labels(row)
        assert all(labels.values()), (row, labels)
    assert window_labels(["codex_", "codex_five_hour"]) == {
        "codex_": "codex", "codex_five_hour": "codex 5h"}
