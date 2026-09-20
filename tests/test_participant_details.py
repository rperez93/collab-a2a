"""Disclosure makes each participant's measurements reachable in a narrow pane."""
from __future__ import annotations

import curses
import time
from types import SimpleNamespace

import pytest

from collab import config
from collab.client import participant_metrics as metrics, tui
from collab.config import SessionProfile


@pytest.fixture
def viewer(monkeypatch):
    settings = {"fields": list(metrics.FIELDS), "details": False}
    monkeypatch.setattr(config, "watch_participant_settings", lambda: settings, raising=False)
    people = [
        {"id": "a", "name": "alice", "connected": True, "stats": {
            "model": "example-model", "context_pct": 64, "cost_usd": 1.25,
            "cost_kind": "estimated", "cost_scope": "session",
            "source": "test-log", "observed_at": time.time(),
            "subagents": {"active": 2, "total": 3},
            "worker": {"enabled": True, "running": True, "turns": 4,
                       "cost_usd": .15, "cost_kind": "estimated", "cost_scope": "worker"},
        }},
        {"id": "b", "name": "bob", "connected": True},
    ]
    model = SimpleNamespace(
        profile=SessionProfile(session_id="s", url="u", name="alice", host_name="alice", token="t", home="/tmp"),
        participants=lambda: people, roster_is_current=lambda: True,
        snapshot_age=lambda: "just now", events=[],
    )
    panel = tui.Tui(model, view="roster")
    panel.roster.rows = 8
    panel._roster_top = 1
    panel._roster(38)
    return panel, people, settings


def redraw(panel, width=38):
    rows = panel._roster(width)
    panel.roster.total = len(rows)
    panel.roster.settle()
    return rows


def test_keyboard_opens_the_selected_person_and_reaches_the_next(viewer):
    """Expanding a long first participant must not trap selection inside it."""
    panel, _, _ = viewer
    panel.handle(curses.KEY_RIGHT)
    rows = redraw(panel)
    assert panel.participant_overrides == {"a": True}
    assert "est. cost $1.25" in "\n".join(row.text for row in rows)
    assert "worker est. cost $0.15" in "\n".join(row.text for row in rows)
    panel.handle(ord("J"))
    rows = redraw(panel)
    assert panel.selected_participant == "b"
    assert any(row.participant == "b" and row.participant_header for row in rows[panel.roster.offset:panel.roster.offset + panel.roster.rows])
    panel.handle(10)
    assert panel.participant_overrides["b"] is True
    panel.handle(curses.KEY_LEFT)
    assert panel.participant_overrides["b"] is False


def test_clicking_a_header_discloses_but_clicking_a_fact_only_selects(viewer, monkeypatch):
    """Mouse disclosure uses the row's identity, including after scrolling."""
    panel, _, _ = viewer
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 3, 1, 0, curses.BUTTON1_PRESSED))
    panel.handle(curses.KEY_MOUSE)
    assert panel.participant_overrides == {"a": True}
    redraw(panel)
    panel.roster.offset = 2
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 5, 1, 0, curses.BUTTON1_PRESSED))
    panel.handle(curses.KEY_MOUSE)
    assert panel.participant_overrides == {"a": True}
    assert panel.focus == "roster"


def test_mouse_controls_match_keyboard_controls(viewer, monkeypatch):
    """Fields and global disclosure are manageable without typing a command."""
    panel, _, _ = viewer
    panel._roster_controls = (0, 3, 15, 27)
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 3, 0, 0, curses.BUTTON1_PRESSED))
    panel.handle(curses.KEY_MOUSE)
    assert panel.participant_details is True
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 15, 0, 0, curses.BUTTON1_PRESSED))
    panel.handle(curses.KEY_MOUSE)
    assert panel.participant_field_mode == 1
    panel.handle(ord("f"))
    assert panel.participant_field_mode == 2
    panel.handle(ord("v"))
    assert panel.participant_details is False


def test_arrows_select_participants_without_changing_chat_follow(viewer):
    """Metric controls cannot turn off following new conversation messages."""
    panel, _, _ = viewer
    panel.handle(ord("v"))
    redraw(panel)
    panel.handle(curses.KEY_DOWN)
    assert panel.selected_participant == "b"
    assert panel.chat.follow is True
    panel.handle(ord("\t"))
    assert panel.focus == "roster", "a single pane cannot focus an invisible conversation"


def test_live_settings_reset_local_overrides_and_rebuild_quiet_rosters(viewer):
    """A saved choice must take effect even when no heartbeat arrives."""
    panel, _, settings = viewer
    panel.handle(ord("v"))
    panel.handle(ord("f"))
    redraw(panel)
    settings.update(fields=["context"], details=True)
    rows = redraw(panel)
    text = "\n".join(row.text for row in rows)
    assert panel.participant_field_mode == 0
    assert "ctx 64%" in text
    assert "est. cost" not in text
    assert "m:example-model" in text  # models remain on every heading


def test_a_departed_participant_does_not_transfer_its_disclosure_to_a_replacement(viewer):
    """Names may be reused; a new participant id cannot inherit old details."""
    panel, people, _ = viewer
    panel.handle(10)
    people[0] = {"id": "new-a", "name": "alice", "connected": True}
    redraw(panel)
    assert panel.selected_participant == "new-a"
    assert panel.participant_overrides == {}


@pytest.mark.parametrize("width", [19, 24, 38, 80])
def test_expanded_measurements_wrap_in_terminal_columns(viewer, width):
    """Long paths and wide names may not write outside the terminal pane."""
    panel, people, _ = viewer
    people[0]["machine"] = "機能追加" * 20
    panel.handle(ord("v"))
    rows = redraw(panel, width)
    assert all(tui._w(row.text) <= width for row in rows)


def test_unknown_is_distinct_from_zero_and_worker_cost_is_separate():
    unknown = metrics.metric_lines({}, metrics.FIELDS, detailed=True)
    assert "cost unknown" in unknown
    assert "subagents unknown active / unknown total" in unknown
    assert "worker unknown" in unknown
    assert all("$0.00" not in line for line in unknown)
    measured = metrics.metric_lines({"stats": {"cost_usd": 0, "cost_kind": "reported", "cost_scope": "session", "subagents": {"active": 0, "total": 0}, "worker": {"enabled": False}}}, metrics.FIELDS, detailed=True)
    assert "reported cost $0.00 · session" in measured
    assert "subagents 0 active / 0 total" in measured
    assert "worker disabled" in measured


def test_a_fresh_heartbeat_does_not_refresh_an_old_measurement():
    """A daemon may keep reporting the same provider observation for hours."""
    stats = {"reported_at": time.time(), "observed_at": time.time() - 7200, "source": "old-log"}
    assert "stale" in metrics.provenance(stats)
    assert "old-log" in metrics.provenance(stats)
    assert metrics.freshness({"reported_at": time.time()}) == "age unknown"


def test_nonfinite_and_boolean_amounts_cannot_look_like_money():
    for value in (float("nan"), float("inf"), True, -1, "broken"):
        assert metrics.cost_text({"cost_usd": value}) == "cost unknown"


def test_quota_freshness_is_independent_of_a_new_token_report():
    """Capacity decisions must not reuse the token stream's newer clock."""
    stats = {"observed_at": time.time(), "quota_observed_at": time.time() - 7200,
             "quotas": {"5h": {"used_pct": 73}}}
    lines = metrics.metric_lines({"stats": stats}, ["quota"], detailed=True)
    assert "quota 5h 73%" in lines
    assert any(line.startswith("quota stale") for line in lines)
    del stats["quota_observed_at"]
    lines = metrics.metric_lines({"stats": stats}, ["quota"], detailed=True)
    assert "quota age unknown" in lines
