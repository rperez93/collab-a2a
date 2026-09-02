"""The count is one of the two figures the roster row exists for, so it is not
the thing the row gives up for width.

REPRODUCED ON A LIVE HUB before anything here was written. A host and a late
guest, a batch of nine with four done, two messages said: both daemons wrote
`messages: {total: 2, fetched_at: …}`, the snapshot refreshed every nine
seconds (worst age seen 6.6 s, threshold 30), and the row was drawn at every
height — and in a `collab watch --tmux` pane, which is 35 % of the terminal,
the row read `batch ██░░░░ 44% 4/9 +9` and nothing else. At 27 and 34 columns
(an 80- and a 100-column terminal) the count was gone on the host and on the
guest, in the split view and in the roster-only view; at 41 (a 120-column
terminal) it survived. `statusbar.fit` gives segments up from the right, the
count sits to the right of the batch, and neither offered a shorter form —
so the batch kept its six glyphs of bar and the row lost its other figure.

Two rules, and the second is the one that makes the first safe:

* NARROW BEFORE DROPPING, and both figures have a narrow form: `44% 4/9` and
  `128 msgs`. The bar's glyphs are decoration on a number that is still there
  without them, and the same trade the host agent's own status line already
  makes (`statusline.render._batch_segment(narrow=True)`).
* THE FIGURES ARE NOT DROPPED AT ALL. `fit` keeps the first `keep` parts
  whatever the width, and the roster row asks for every figure it composed —
  only the key legend, on the pane that carries one, is expendable. Where even
  the narrow forms will not fit, the row is CLIPPED, with an ellipsis that
  says so, rather than silently shortened by a whole figure: a clip is visible
  and a drop is not.
"""

from __future__ import annotations

import curses
import time

import pytest

from collab import batch, config
from collab.client import daemon, statusbar as sb, tui
from collab.config import SessionProfile
from test_roster_rule import Screen, _split_geometry

ROSTER = config.WATCH_ROSTER_SEGMENTS

#: What a `collab watch --tmux` pane is: 35 % of an 80-, 100- and 120-column
#: terminal, less the border tmux draws. The first two are where the count went.
PANE_WIDTHS = (27, 34, 41)


def _fit(parts, width, **kw):
    return sb.fit(parts, width, tui._w, tui._clip, **kw)


def _figures(*, moved: bool = False):
    """The shapes the live hub produced. `moved` adds the `+9` the host's row
    carried, because nine tasks had just been proposed."""
    now = time.time()
    figures = {"done": 4, "total": 9, "fetched_at": now}
    if moved:
        figures.update(total_delta=9, delta_at=now)
    return figures, {"total": 2, "fetched_at": now}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


# --- composition -------------------------------------------------------------

@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("width", PANE_WIDTHS)
def test_the_count_survives_a_tmux_pane(moved, width):
    batch_figures, messages = _figures(moved=moved)
    parts = sb.compose(batch=batch_figures, messages=messages, segments=ROSTER)
    line = _fit(parts, width - 1)
    assert "2 message" in line or "2 msgs" in line, f"the count went at {width}: {line!r}"
    assert "4/9" in line, f"and the batch is still there: {line!r}"
    assert tui._w(line) <= width - 1


def test_the_bar_glyphs_go_before_the_count_does():
    """At a width that holds the count only if the batch gives up its bar."""
    batch_figures, messages = _figures(moved=True)
    parts = sb.compose(batch=batch_figures, messages=messages, segments=ROSTER)
    wide = _fit(parts, 200)
    assert "██" in wide and "2 messages" in wide, wide
    narrow = _fit(parts, 26)
    assert "██" not in narrow and "batch" not in narrow, narrow
    assert "44% 4/9 +9" in narrow and ("2 msgs" in narrow or "2 messages" in narrow), narrow


def test_the_narrow_forms_are_what_the_segments_offer():
    batch_figures, messages = _figures()
    assert sb.batch_segment(batch_figures, narrow=True) == "44% 4/9"
    assert sb.messages_segment(messages, narrow=True) == "2 msgs"
    one = {"total": 1, "fetched_at": time.time()}
    assert sb.messages_segment(one, narrow=True) == "1 msg"
    stale = {"total": 2, "fetched_at": time.time() - 600}
    assert sb.messages_segment(stale, narrow=True) == "msgs ? 10m old"
    assert sb.messages_segment(stale) == "messages ? 10m old"


def test_compose_hands_fit_both_forms_widest_first():
    batch_figures, messages = _figures()
    parts = sb.compose(batch=batch_figures, messages=messages, segments=ROSTER)
    assert parts == [("batch ██░░░░ 44% 4/9", "44% 4/9"), ("2 messages", "2 msgs")]


def test_kept_parts_are_clipped_rather_than_dropped():
    """`keep=2`: the row may be cut short, with the ellipsis to say so, but a
    whole figure is never taken off it in silence."""
    batch_figures, messages = _figures()
    parts = sb.compose(batch=batch_figures, messages=messages, segments=ROSTER)
    line = _fit(parts, 12, keep=2)
    assert line.startswith(" 44% 4/9"), line
    assert line.endswith("…"), f"clipped without saying so: {line!r}"
    assert tui._w(line) <= 12
    # And with the default the last part is what goes — the old behaviour,
    # which the conversation's own row still relies on.
    assert _fit(parts, 12) == " 44% 4/9"


def test_the_legend_is_still_given_up_before_either_figure():
    batch_figures, messages = _figures(moved=True)
    parts = sb.compose(batch=batch_figures, messages=messages, segments=ROSTER,
                       keys=(tui.ROSTER_KEYS, tui.ROSTER_KEYS_SHORT))
    line = _fit(parts, 26, keep=2)
    assert "quit" not in line, line
    assert "4/9" in line and "msgs" in line, line


# --- on a real draw ----------------------------------------------------------

def _viewer(tmp_path, view: str, *, moved: bool, me: str = "bob") -> tui.Tui:
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True, exist_ok=True)
    profile = SessionProfile(session_id="s", url="u", name=me, host_name="alice",
                             token="t", home=str(home))
    profile.save()
    model = tui.Model(profile=profile)
    batch_figures, messages = _figures(moved=moved)
    model.snapshot = {"participants": [{"name": "alice", "connected": True},
                                       {"name": "bob", "connected": True}],
                      "fetched_at": time.time()}
    model.status = {"batch": batch_figures, "messages": messages,
                    "name": me, "host": "alice", "is_host": me == "alice"}
    model._state = "live"
    return tui.Tui(model, view=view)


def _draw(viewer, win):
    try:
        viewer._draw(win)
    except curses.error:
        pass


@pytest.mark.parametrize("me", ["alice", "bob"])
@pytest.mark.parametrize("view", ["both", "roster"])
@pytest.mark.parametrize("width", PANE_WIDTHS + (80, 100, 120))
@pytest.mark.parametrize("height", [24, 30])
def test_the_count_is_on_the_row_for_host_and_guest_at_every_pane_width(
        tmp_path, cfg, me, view, width, height):
    viewer = _viewer(tmp_path, view, moved=me == "alice", me=me)
    win = Screen(height, width)
    _draw(viewer, win)
    head = "PARTICIPANTS" in win.row(2 if view == "both" else 0)
    assert head, "nothing drawn"
    assert not win.overruns, win.overruns[:1]
    y = (_split_geometry(height)[0] - 1) if view == "both" else height - 1
    # The row is the panel's last row, or the one above the bottom padding.
    rows = [win.row(y), win.row(y - 1)]
    line = next((r for r in rows if "4/9" in r), None)
    assert line is not None, f"no roster row near the foot: {rows!r}"
    assert "2 message" in line or "2 msgs" in line, f"the count is missing: {line!r}"


# --- the other leads, ruled out and pinned -----------------------------------

def test_the_snapshot_refresh_leaves_the_count_fresh_with_margin():
    """Measured on the live hub: the count's age cycled 0.5–6.6 s on both
    sides. Two missed refreshes and a status heartbeat still land inside the
    threshold, so the row reads a number and not `messages ?` while the hub is
    up. Pinned so that a slower refresh has to argue with this."""
    assert daemon.SNAPSHOT_REFRESH * 2 + daemon.STATUS_HEARTBEAT < batch.STALE_AFTER
