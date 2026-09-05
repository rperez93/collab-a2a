"""Losing the message count off the roster row should not mean retyping the row.

`watch_status_roster_segments` was the only way to say anything about that
count, and it is an ORDER — so it answered the two questions people actually
asked with the same key, and got both of them wrong.

Somebody who wanted the count gone had to name `batch` and `keys`, two figures
they were happy with, in order to drop the third; and somebody who had written
an order of their own — `["batch"]`, on the day the row carried nothing else —
silently lost a figure they had never been asked about when the count shipped,
with nothing on screen to say a setting of theirs was the reason.

`watch_status_messages` is the switch that was missing. The list decides where
the count goes; the switch decides whether it is there at all. These tests hold
the two apart in both directions: an order that omits the count still gets it,
and a switch that is off removes it from an order that names it.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import config
from collab.client import statusbar as sb

ROSTER = config.WATCH_ROSTER_SEGMENTS


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config file of this test's own, with the read cache cleared around it."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


def _row(segments):
    """What the roster row renders as, given a resolved order of segments."""
    now = time.time()
    parts = sb.compose(keys="q: quit",
                       batch={"done": 6, "total": 10, "fetched_at": now},
                       messages={"total": 128, "fetched_at": now},
                       segments=segments)
    return [part[0] if isinstance(part, tuple) else part for part in parts]


def _resolved():
    """The order the viewer draws: the file's list under the file's switch."""
    settings = config.watch_roster_settings()
    return sb.roster_segments(settings["segments"], messages=settings["messages"])


# --- the switch --------------------------------------------------------------

def test_the_count_is_on_when_nobody_has_said_otherwise(cfg):
    """The row has carried it since it existed; a new key may not take it away."""
    assert config.watch_roster_settings()["messages"] is True
    assert _resolved() == ROSTER
    assert "128 messages" in _row(_resolved())


def test_turning_the_switch_off_removes_the_count(cfg):
    config.setting("watch_status_messages").write(False)
    assert _resolved() == ("batch", "keys")
    assert not any("message" in text for text in _row(_resolved()))


def test_the_switch_is_not_overruled_by_an_order_that_names_the_count(cfg):
    """A switch the order could contradict is not a switch, and the order is
    the surface most likely to still name the count: it is what people were
    told to edit before this key existed."""
    config.setting("watch_status_roster_segments").write(["batch", "messages"])
    config.setting("watch_status_messages").write(False)
    assert "messages" not in _resolved()


# --- the order ---------------------------------------------------------------

def test_an_order_written_before_the_count_existed_still_shows_it(cfg):
    """`["batch"]` said «the bar, here». It never said «and no count», and for
    a version it was read as though it had."""
    cfg.write_text(json.dumps({"watch_status_roster_segments": ["batch"]}))
    assert _resolved() == ("batch", "messages")
    assert "128 messages" in _row(_resolved())


def test_the_count_lands_behind_the_batch_when_the_order_does_not_place_it(cfg):
    """Where the default order puts it, and where `fit` narrows the two
    figures as a pair — ahead of the batch they would be narrowed apart."""
    cfg.write_text(json.dumps({"watch_status_roster_segments": ["keys", "batch"]}))
    assert _resolved() == ("keys", "batch", "messages")


def test_with_no_batch_on_the_row_the_count_goes_first(cfg):
    """There is nothing for it to sit behind, and a figure belongs ahead of the
    legend: the legend is the same six words every session."""
    cfg.write_text(json.dumps({"watch_status_roster_segments": ["keys"]}))
    assert _resolved() == ("messages", "keys")


def test_an_order_that_names_the_count_is_obeyed_exactly(cfg):
    """The switch decides presence and nothing else. Somebody who put the count
    in front of the bar meant it there."""
    config.setting("watch_status_roster_segments").write(["messages", "batch", "keys"])
    assert _resolved() == ("messages", "batch", "keys")
    assert _row(_resolved())[0] == "128 messages"


def test_an_empty_order_still_answers_to_the_switch(cfg):
    """A row somebody emptied by hand is a row they emptied; the count is the
    one thing on it whose absence is now a separate statement, so it comes
    back — and goes again when the switch says so."""
    cfg.write_text(json.dumps({"watch_status_roster_segments": []}))
    assert _resolved() == ("messages",)
    config.setting("watch_status_messages").write(False)
    assert _resolved() == ()
