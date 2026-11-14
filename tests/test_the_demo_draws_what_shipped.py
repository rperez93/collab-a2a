"""The demo — and so every screenshot — shows what collab draws today.

The screenshots are captured from `collab watch --demo` (scripts/capture-docs.py),
so a feature the demo's fixture does not carry is a feature no picture of collab
shows. These tests hold the fixture to the features merged since 2.1.2: a batch
with its ETA, shared skill counts, a peer's worker waiting on its human, a
worker pacing its budget, a setting's layer — and names that are an agent's,
never a person's.
"""

from __future__ import annotations

import re

from collab import demo
from collab.client import participant_metrics, statusbar


def _host() -> dict:
    return next(p for p in demo.snapshot()["participants"] if p["name"] == demo.HOST)


def test_the_demo_batch_draws_its_eta():
    """The status row and the batch line carry the ETA from the batch's pace."""
    segment = statusbar.batch_segment(demo.status()["batch"])
    assert "60% 6/10" in segment and re.search(r"ETA ~3\dm", segment), segment


def test_the_demo_participants_show_how_many_skills_they_share():
    line = participant_metrics.metric_lines(_host(), ["location"], detailed=False)[0]
    assert "7 skills shared" in line


def test_a_demo_peer_is_waiting_on_its_human_and_pacing_its_budget():
    lines = participant_metrics.metric_lines(_host(), ["worker"], detailed=True)
    text = "\n".join(lines)
    assert "waiting on their human (1) · 7m" in text
    assert re.search(r"worker budget 9 calls left · resets in (29|30)m", text), text


def test_a_count_that_is_not_one_is_not_drawn():
    """These figures come off the hub; a remote party chose them."""
    person = {"repo": "r", "machine": "m", "skills_count": "lots"}
    assert "skill" not in participant_metrics.metric_lines(person, ["location"], detailed=False)[0]
    worker = {"enabled": True, "running": True, "budget_remaining": True}
    lines = participant_metrics.metric_lines({"stats": {"worker": worker}}, ["worker"],
                                             detailed=True)
    assert not any("budget" in line for line in lines)


def test_the_settings_editor_names_each_settings_layer():
    """`collab config --tui` shows where the selected value comes from, the
    answer `collab config KEY` prints (agent, kind, machine, legacy, default)."""
    from collab.client import settings_tui

    assert settings_tui._layer("theme") in ("agent", "kind", "machine", "legacy", "default")


def test_the_demo_names_agents_never_people():
    """A demo is a picture people copy. Its third participant is named the way
    an agent without a name of its own is named now — kind and suffix — and
    nothing in the fixture is a person's first name."""
    names = {p["name"] for p in demo.snapshot()["participants"]}
    assert re.fullmatch(r"codex-[0-9a-f]{8}", demo.PEER)
    assert "mila" not in names
    assert all(e.sender in {demo.HOST, demo.YOU, demo.PEER} for e in demo.events())


def test_negative_counts_are_not_drawn():
    """S2 and B2: a peer's negative skill count or call budget is not a count."""
    person = {"repo": "r", "machine": "m", "skills_count": -3}
    assert "skill" not in participant_metrics.metric_lines(person, ["location"], detailed=False)[0]
    worker = {"enabled": True, "running": True, "budget_remaining": -1}
    lines = participant_metrics.metric_lines({"stats": {"worker": worker}}, ["worker"],
                                             detailed=True)
    assert not any("budget" in line for line in lines)


def test_huge_figures_are_drawn_bounded():
    """A peer's `10**60` calls drew sixty-one digits into everybody's panel and
    a reset at 1e308 about three hundred; both are bounded on the way out."""
    worker = {"enabled": True, "running": True, "budget_remaining": 10 ** 60,
              "budget_reset_at": 1e308}
    text = "\n".join(participant_metrics.metric_lines({"stats": {"worker": worker}},
                                                     ["worker"], detailed=True))
    assert "worker budget ≥1000 calls left · resets in >1d" in text
    person = {"repo": "r", "machine": "m", "skills_count": 10 ** 60}
    line = participant_metrics.metric_lines(person, ["location"], detailed=False)[0]
    assert "≥1000 skills shared" in line and len(line) < 80


def test_the_settings_footer_shows_the_layer_even_when_narrow():
    """T1: not only `_layer`'s answer — the footer DRAWS «set by», first, so a
    long default at 52 columns cannot clip it off the end."""
    import sys

    from collab.client.settings_tui import SettingsEditor

    sys.path.insert(0, __import__("os").path.dirname(__file__))
    from test_settings_tui import Screen

    editor = SettingsEditor()
    longest = max(editor.filtered, key=lambda item: len(str(item.default)))
    editor.selected = longest.name
    screen = Screen(height=24, width=52)
    editor.draw(screen)
    heading = [text for _, _, text in screen.writes if text.startswith("set by: ")]
    assert heading and longest.name[:10] in heading[-1], screen.writes
    assert any("r: Reset to default:" in text for _, _, text in screen.writes)
