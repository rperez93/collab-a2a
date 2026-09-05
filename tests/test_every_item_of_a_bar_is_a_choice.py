"""Three bars, and until now only two of them had a list.

collab draws three status bars: the roster panel's row, the conversation
panel's row, and the segment in the coding agent's own prompt. The two in
`collab watch` could be composed from a list; the third could not, and it is the
one drawn most often — a window is opened on purpose, a prompt is not.

And the two that did have lists had one item apiece that was not on them. The
scrolled-back notice was written into the conversation row unconditionally,
which made it the only item on either bar nobody could turn off. That was an
accident of how it got its OTHER promise: `fit` will not trade it away for a
progress bar, because it is the only thing on the row saying the view is not
live. Undroppable for width and unhideable by choice are different promises,
and only the first was ever argued for.

So: `statusline_segments` for the third bar, `notice` as a real segment on the
second, and these tests hold both halves — that everything can be hidden, and
that hiding is the only thing the list decides about the notice.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import config
from collab.client import statusbar as sb
from collab.config import SessionProfile
from collab.statusline import render as r


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


# --- the notice is a segment now ------------------------------------------------

def test_the_notice_is_on_by_default(cfg):
    assert "notice" in config.WATCH_STATUS_SEGMENTS
    assert "notice" in config.DEFAULT_WATCH_STATUS_SEGMENTS
    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit",
                       segments=config.DEFAULT_WATCH_STATUS_SEGMENTS)
    assert parts[0] == "⏸ 4 new below"


def test_a_list_without_it_turns_it_off(cfg):
    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit",
                       segments=("stats", "keys"))
    assert parts == ["q: quit"]


def test_it_goes_first_wherever_the_list_puts_it(cfg):
    """Its position is not the list's to decide. The rule that protects it from
    being dropped for width holds the FIRST parts of the row, so a notice moved
    to the end would have been moved out from under its own protection with
    nothing to say so."""
    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit",
                       stats={"cost_usd": 1.0},
                       segments=("keys", "stats", "notice"))
    assert parts == ["⏸ 4 new below", "q: quit", "$1.00"]


def test_it_is_still_never_given_up_for_width(cfg):
    """The promise that was always kept, and the one this change is not about."""
    from collab.client import tui

    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit · G: newest",
                       batch={"done": 6, "total": 10, "fetched_at": time.time()},
                       stats={"cost_usd": 3.1}, command="main",
                       segments=config.WATCH_STATUS_SEGMENTS)
    line = sb.fit(parts, 24, tui._w, tui._clip)
    assert "⏸" in line


def test_the_command_accepts_it_and_the_config_reads_it_back(cfg):
    config.setting("watch_status_segments").write(["notice", "keys"])
    assert config.watch_status_settings()["segments"] == ("notice", "keys")


# --- the agent's own status line has a list too ----------------------------------

@pytest.fixture
def line(tmp_path, monkeypatch, cfg):
    """A live session whose status line can be rendered."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("NO_COLOR", "1")
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps(
        {"session_id": "s", "name": "bob", "host": "alice", "is_host": False,
         "state": "live", "heartbeat": time.time(), "others_connected": 3,
         "unread_messages": 2, "version": "9.9.9",
         "batch": {"done": 6, "total": 10, "fetched_at": time.time()}}))
    monkeypatch.setattr(r, "is_running", lambda p: 4242)
    monkeypatch.setattr(r, "state_dir_label", lambda home, cwd=None: "")
    monkeypatch.setattr(r, "_update_available", lambda: True)
    monkeypatch.setattr(r, "daemon_note", lambda status: "")
    monkeypatch.setattr(r, "hub_note", lambda status: "")
    return profile


def test_everything_is_on_by_default(line):
    drawn = r.render(width=200)
    assert "●" in drawn                       # state
    assert "collab" in drawn                  # label
    assert "v9.9.9" in drawn                  # version
    assert "bob → alice" in drawn             # who
    assert "+3" in drawn                      # others
    assert "✉ 2" in drawn                     # unread
    assert "6/10" in drawn                    # batch
    assert "↑update" in drawn                 # update


@pytest.mark.parametrize("hidden,gone", [
    ("state", "●"),
    ("label", "collab"),
    ("version", "v9.9.9"),
    ("who", "bob → alice"),
    ("others", "+3"),
    ("unread", "✉ 2"),
    ("batch", "6/10"),
    ("update", "↑update"),
])
def test_any_one_of_them_can_be_hidden(hidden, gone, line):
    """`state` and `who` included. The request was that EVERY item be a choice,
    and a list that quietly excepted the two biggest ones would be answering a
    different question."""
    kept = [s for s in config.STATUSLINE_SEGMENTS if s != hidden]
    config.setting("statusline_segments").write(kept)
    assert gone not in r.render(width=200)


def test_the_order_is_the_lists(line):
    config.setting("statusline_segments").write(["unread", "who", "state"])
    drawn = r.render(width=200)
    assert drawn.index("✉ 2") < drawn.index("bob → alice") < drawn.index("●")


def test_an_unknown_name_costs_that_segment_and_not_the_line(line, cfg):
    """This file swallows its own errors, so a name it refused would be a
    status line that vanished with nothing anywhere to say why."""
    cfg.write_text(json.dumps({"statusline_segments": ["state", "nope", "who"]}))
    drawn = r.render(width=200)
    assert "●" in drawn and "bob → alice" in drawn
    assert "collab" not in drawn


def test_a_typo_is_refused_at_the_command_where_somebody_can_read_it(line):
    with pytest.raises(ValueError) as bad:
        config.setting("statusline_segments").write(["state", "nope"])
    assert "nope" in str(bad.value)


@pytest.mark.parametrize("raw", [
    '{"statusline_segments": "state"}',
    '{"statusline_segments": 1e400}',
    '{"statusline_segments": {"state": true}}',
])
def test_nothing_in_a_hand_edited_file_can_stop_the_line_being_drawn(raw, line, cfg):
    cfg.write_text(raw)
    assert r.render(width=200), "the line went away entirely"


def test_the_narrow_fallback_keeps_the_same_filter(line):
    """A segment turned off is off at every width. A fallback that put `who`
    back on a cramped terminal would make the setting look like it worked until
    the terminal was resized."""
    config.setting("statusline_segments").write(["state", "others", "batch"])
    drawn = r.render(width=20)
    assert "bob → alice" not in drawn
    assert "●" in drawn


def test_hiding_everything_draws_nothing_and_is_not_a_fault(line):
    """An empty line built from an empty list is what its reader asked for, so
    the last-line fallback must not stand back up over it."""
    r.draw()                                  # a line is drawn, and kept
    config.setting("statusline_segments").write([])
    drawn, why = r.draw()
    assert drawn == ""
    assert why == "", "it is not a failure, and not a kept line"


# --- the two version warnings ------------------------------------------------------

def test_the_version_warnings_ride_the_version_segment(line, monkeypatch):
    """They are the same fact as the number — something running other code than
    this — drawn where the number would be. Two settings for one idea would be
    one more thing to get half right."""
    monkeypatch.setattr(r, "daemon_note", lambda status: "daemon v1 — restart it")
    monkeypatch.setattr(r, "hub_note", lambda status: "hub v1 — the host re-hosts")
    assert "daemon v1" in r.render(width=300)

    config.setting("statusline_segments").write(["state", "who", "others"])
    drawn = r.render(width=300)
    assert "daemon v1" not in drawn and "hub v1" not in drawn


def test_the_hub_warning_still_follows_the_identity(line, monkeypatch):
    """A reader has to know whose session this is before being told what the
    host of it has to do."""
    monkeypatch.setattr(r, "hub_note", lambda status: "hub v1 — the host re-hosts")
    drawn = r.render(width=300)
    assert drawn.index("bob → alice") < drawn.index("hub v1")
