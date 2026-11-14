"""The hub publishes both models in use, and when the pair last changed.

The contract with the dashboard is four fields: `main_model`, `worker_model`,
`model_observed_at` and `model_changed_at`, with null meaning unknown. The
faults these tests prevent: a switch that does not show within one refresh, a
first sighting drawn as a change that never happened, a disabled worker shown
with the last model it ran, and a participant-posted value standing in for the
derived one.
"""
import pytest

from collab.server.hub import Hub
from collab.server.store import Store


@pytest.fixture
def room(tmp_path):
    store = Store(tmp_path / "hub.db")
    person = store.add_participant("test", "token")
    hub = Hub(store, session_id="s", host_name="test")

    def report(figures):
        hub.merge_stats(person.id, figures)
        return store.participant_by_id(person.id).meta.get("stats")
    return report


def test_a_first_model_is_published_but_is_not_a_change(room):
    """Nothing was seen before it, so «changed just now» would be invented."""
    stats = room({"model": "Opus 5"})
    assert stats["main_model"] == "Opus 5"
    assert stats["worker_model"] is None, "no worker block: unknown, not absent"
    assert stats["model_changed_at"] is None
    assert isinstance(stats["model_observed_at"], (int, float))


def test_a_switch_shows_on_the_next_report_with_its_moment(room):
    """`/model` in Claude Code reaches the hub as the next status-line report."""
    room({"model": "Opus 5"})
    stats = room({"model": "Sonnet 5"})
    assert stats["main_model"] == "Sonnet 5"
    assert stats["model_changed_at"] == stats["reported_at"]


def test_a_report_about_something_else_moves_nothing(room):
    """A cost-only report still has a model, the one merged from before."""
    room({"model": "Opus 5"})
    switched = room({"model": "Sonnet 5"})["model_changed_at"]
    stats = room({"cost_usd": 1.5})
    assert stats["main_model"] == "Sonnet 5"
    assert stats["model_changed_at"] == switched


def test_the_worker_model_is_its_own_and_off_is_unknown(room):
    """A disabled worker has no model in use; the last one it ran is history."""
    stats = room({"model": "gpt-x", "worker": {"enabled": True, "last_model": "small-1"}})
    assert stats["worker_model"] == "small-1"
    stats = room({"worker": {"enabled": True, "last_model": "small-2"}})
    assert stats["worker_model"] == "small-2"
    assert stats["model_changed_at"] == stats["reported_at"]
    stats = room({"worker": {"enabled": False, "last_model": "small-2"}})
    assert stats["worker_model"] is None


def test_a_withdrawn_model_is_unknown_and_is_a_change(room):
    """`{"model": null}` is how a switch to a tool with no source says «unknown»."""
    room({"model": "gpt-x"})
    stats = room({"model": None, "quotas": {}})
    assert stats["main_model"] is None
    assert stats["model_changed_at"] == stats["reported_at"]


def test_a_posted_summary_is_overwritten_by_the_derived_one(room):
    """These fields restate `model`; a reported copy could only disagree with it."""
    stats = room({"model": "Opus 5", "main_model": "something else", "model_changed_at": 1})
    assert stats["main_model"] == "Opus 5"
    assert stats["model_changed_at"] is None


def test_a_body_that_says_nothing_grows_no_model_fields(room):
    """`collab color` posts `stats: {}`; four nulls would read as figures."""
    assert "main_model" not in (room({}) or {})


def test_a_model_name_cannot_carry_terminal_commands_or_bidi(room):
    """Drawn on everyone's terminal: an ESC is a command, a bidi override a disguise."""
    stats = room({"model": "\x1b[31mRED\x1b]0;title\x07‮gpt​",
                  "worker": {"enabled": True, "last_model": "small\x9b2J⁦"}})
    for value in (stats["model"], stats["main_model"], stats["worker_model"]):
        assert not any(ord(ch) < 32 or 0x7f <= ord(ch) < 0xa0 for ch in value), repr(value)
        assert not set(value) & {"‮", "​", "⁦"}, repr(value)
    assert stats["main_model"].endswith("gpt")
    assert stats["worker_model"] == "small2J"


def test_a_worker_call_on_several_models_is_not_a_change_of_model(room):
    """«multiple models» describes one call; publishing it flipped the field every time."""
    room({"model": "m", "worker": {"enabled": True, "last_model": "small-1"}})
    settled = room({"cost_usd": 1})
    stats = room({"worker": {"enabled": True, "last_model": "multiple models"}})
    assert stats["worker_model"] == "small-1"
    assert stats["model_changed_at"] == settled["model_changed_at"]
    stats = room({"worker": {"enabled": True, "last_model": "small-1"}})
    assert stats["model_changed_at"] == settled["model_changed_at"]


# --- every layer a reported string passes through is printable -------------------

import json
import unicodedata

from collab import stats as statmod

EVIL = "\x1b[31mRED\x1b]0;t\x07‮gpt​\ud800͸x"


def _printable(value):
    """No control, format, separator, surrogate, private or unassigned code point."""
    bad = [ch for ch in value if unicodedata.category(ch) in
           ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn")]
    assert not bad, repr(value)
    value.encode("utf-8")            # what the roster encoder does to it
    return value


def _strings(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _strings(value)
    elif isinstance(obj, str):
        yield obj


def test_extended_text_fields_are_printable():
    """D7: `last_model` and `source` take the TEXT path; tui.py draws last_model raw."""
    out = statmod.sanitise({"last_model": EVIL, "source": EVIL, "cost_scope": EVIL})
    for key in ("last_model", "source", "cost_scope"):
        assert _printable(out[key]).endswith("gptx")


def test_strings_nested_in_the_worker_and_subagents_are_printable():
    """D8: the worker block and subagent block each carry their own strings."""
    out = statmod.sanitise({"worker": {"enabled": True, "model": EVIL, "last_model": EVIL,
                                       "source": EVIL, "agent": EVIL},
                            "subagents": {"active": 1, "source": EVIL}})
    seen = list(_strings(out))
    assert any("gptx" in s for s in seen)
    for s in seen:
        _printable(s)


def test_extra_keys_and_values_are_printable():
    """D11: a handful of unknown fields are kept; neither their names nor values may command."""
    out = statmod.sanitise({"note" + EVIL: EVIL})
    assert out, "the extra field is kept"
    for s in _strings(out):
        _printable(s)


def test_the_model_summary_cleans_what_it_is_given_directly():
    """D12: `name()` is its own layer, for a merged dict that bypassed sanitise."""
    out = statmod.model_summary({}, {"model": EVIL, "worker": {"last_model": EVIL}}, 1.0)
    assert _printable(out["main_model"]).endswith("gptx")
    assert _printable(out["worker_model"]).endswith("gptx")


def test_a_lone_surrogate_cannot_break_the_roster_for_everyone(room, tmp_path):
    """N2: `"gpt\\ud800x"` survived as a model and the roster's UTF-8 encode raised."""
    from collab.server.roster_transport import _roster_parts
    store = Store(tmp_path / "roster.db")
    person = store.add_participant("p", "token")
    hub = Hub(store, session_id="s", host_name="p")
    hub.merge_stats(person.id, json.loads('{"model": "gpt\\ud800x"}'))
    parts, _tag = _roster_parts(hub.snapshot())
    body = b"".join(parts).decode("utf-8")
    assert '"main_model":"gptx"' in body
