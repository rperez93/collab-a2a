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
