"""Pending decisions stay readable before disclosure in narrow participant cards."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from collab import config
from collab.client import participant_metrics, tui


def person(pending, **worker):
    return {"id": "owner", "name": "Alexandra-the-platform-owner", "stats": {
        "model": "gpt-6-astra", "worker": {"model": "gpt-6-luna", "pending": pending, **worker}}}


def card(data, width, *, opened=False, online=True, fields=()):
    return tui._participant_card(data, width, who="▸● " + data["name"],
        state="online" if online else "unknown", online=online, selected=False,
        fields=fields, detailed=opened, doing="working on narrow participant layout [T_R5]")


@pytest.mark.parametrize("width", [35, 38, 51, 54, 77, 80])
@pytest.mark.parametrize("pending,word", [(1, "decision"), (2, "decisions"), (1000, "decisions")])
def test_collapsed_pending_alert_keeps_identity_state_and_unknown_age_visible(width, pending, word):
    """Metric filters and a long name must not hide an explicit pending decision count."""
    rows = card(person(pending), width)
    assert rows[0].participant_header and "Alex" in rows[0].text
    assert rows[0].text.endswith("online")
    assert len(rows) == 2
    assert word + " pending" in rows[1].text
    assert "age unknown" in rows[1].text or "undated:" in rows[1].text
    assert rows[1].pair == tui.C_WARN and not rows[1].participant_header
    assert rows[1].participant == "owner"
    assert all(tui._w(row.text) <= width for row in rows)


@pytest.mark.parametrize("invalid", [None, 0, -1, True, False, "2", "unknown", [], {},
                                     float("nan"), float("inf"), 1.5])
def test_unknown_zero_or_invalid_pending_counts_do_not_invent_a_blocker(invalid):
    """Missing telemetry and malformed counts must never be treated as positive known work."""
    assert len(card(person(invalid), 38)) == 1


@pytest.mark.parametrize("width", [38, 54, 80])
@pytest.mark.parametrize("opened", [False, True])
def test_pending_alert_stays_above_metric_disclosure_and_uses_its_own_status_clock(width, opened):
    """A fresh token or cost reading cannot rejuvenate an undated decision count."""
    now = time.time()
    data = person(2, observed_at=now, tokens_observed_at=now, cost_observed_at=now)
    rows = card(data, width, opened=opened, fields=participant_metrics.FIELDS)
    assert "2 decisions pending" in rows[1].text
    assert "age unknown" in rows[1].text
    data["stats"]["worker"]["reported_at"] = now
    fresh = card(data, width, opened=opened, fields=participant_metrics.FIELDS)[1].text
    assert "2 decisions pending" in fresh and "ago" in fresh
    assert "unknown" not in fresh and "last:" not in fresh
    data["stats"]["worker"]["reported_at"] = now - 86400
    stale = card(data, width, opened=opened, fields=participant_metrics.FIELDS)[1].text
    assert "last:" in stale and "2 decisions pending" in stale
    offline = card(person(2, reported_at=now), width, online=False)[1].text
    assert "last:" in offline and "2 decisions pending" in offline


def test_pending_changes_rebuild_the_roster_but_cached_draws_do_not_poll(monkeypatch):
    """One warning row uses the existing roster invalidation and creates no new intake route."""
    data = person(0)
    settings = {"fields": [], "details": False}
    monkeypatch.setattr(config, "watch_participant_settings", lambda: settings)
    model = SimpleNamespace(profile=SimpleNamespace(name="me"), events=[],
        participants=lambda: [data], roster_is_current=lambda: True, snapshot_age=lambda: "")
    panel = tui.Tui(model, view="roster")
    initial = panel._roster(54)
    assert len(initial) == 1 and panel._roster(54) is initial
    data["stats"]["worker"]["pending"] = 2
    pending = panel._roster(54)
    assert pending is not initial and "2 decisions pending" in pending[1].text
    assert panel._roster(54) is pending


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
@pytest.mark.parametrize("width", [38, 54, 80])
def test_real_tmux_pending_alert_survives_keyboard_and_mouse_disclosure(tmp_path, width):
    """Ncurses must keep the count horizontal before and after a real user opens the card."""
    harness = tmp_path / "panel.py"
    harness.write_text('''from collab import demo
from collab.client import tui
m=demo.model()
m.snapshot['participants']=[{'id':'a','name':'Alexandra-the-platform-owner','connected':True,
 'stats':{'model':'gpt-6-astra','worker':{'pending':2,'model':'gpt-6-luna','running':True}}},
 {'id':'b','name':'Bob-unknown-measurements','connected':False,'stats':{}}]
m.refresh_side=lambda:None
raise SystemExit(tui.run(m.profile,view='roster',model=m))
''')
    env = dict(os.environ, COLLAB_CONFIG=str(tmp_path / "config.json"),
        COLLAB_STATE_DIR=str(tmp_path / "state"), COLLAB_HOME=str(tmp_path / "home"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        TERM="xterm-256color", NCURSES_NO_UTF8_ACS="1")
    env.pop("TMUX", None)
    command = ["tmux", "-S", str(tmp_path / "tmux.sock")]
    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                               capture_output=True, text=True, timeout=5).stdout
    def expect(marker):
        until = time.monotonic() + 5
        text = ""
        while time.monotonic() < until:
            text = call("capture-pane", "-p")
            if marker in text and "2 decisions pending" in text:
                return text
            time.sleep(.05)
        pytest.fail(f"Missing {marker!r} or pending count:\n{text}")
    try:
        call("-f", "/dev/null", "new-session", "-d", "-x", str(width), "-y", "30",
             shlex.join(["env", "TERM=xterm-256color", sys.executable, str(harness)]))
        initial = expect("▸◌ Alex")
        assert "age unknown" in initial and "Bob-unknown" in initial
        call("send-keys", "Enter")
        expect("▾◌ Alex")
        call("send-keys", "-l", "\x1b[<0;4;2M\x1b[<0;4;2m")
        expect("▸◌ Alex")
        call("send-keys", "Enter")
        expect("▾◌ Alex")
    finally:
        subprocess.run(command + ["kill-server"], env=env, capture_output=True, timeout=5)
