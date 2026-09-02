"""The header row of a quoted collab message fits the pane in columns.

`rows()` clips the ``✉ collab · <sender> · <time>`` line like every other row,
but the demo's fixed script only ever quotes two short senders, so the clip on
that one line was never exercised: deleting it left every test green. A sender
name is a remote string — as wide as somebody chose to make it, in whatever
script — and a row written past the last cell is what ends a curses viewer
rather than the frame.
"""

from __future__ import annotations

import pytest

from collab.client import demo_agent
from collab.columns import width as _w

LONG_SENDERS = [
    "田中太郎とその同僚たちのチーム名がとても長い場合",     # CJK, two columns each
    "👩‍💻👨‍💻🧑‍💻 the whole platform guild 🚀🚀🚀",         # ZWJ emoji
    "a" * 90,                                              # plainly too long
    "ｆｕｌｌｗｉｄｔｈ　ｌａｔｉｎ　ｎａｍｅ",             # fullwidth
]


@pytest.mark.parametrize("who", LONG_SENDERS)
@pytest.mark.parametrize("width", [24, 40, 60, 80, 120])
@pytest.mark.parametrize("kind", ["inbound", "outbound"])
def test_a_long_sender_never_over_runs_the_header_row(who, width, kind):
    line = demo_agent.Line(kind, "the message itself", who=who,
                           ts="2026-09-02T14:05:00Z")
    drawn = demo_agent.rows([line], width)
    head = drawn[0]
    assert who[:3] in head.text or "…" in head.text, "the header row is missing"
    for row in drawn:
        assert _w(row.text) <= width, (
            f"{kind} at {width}: {_w(row.text)} columns: {row.text!r}")


def test_a_short_sender_is_untouched():
    """Clipping must not eat a name that fits."""
    line = demo_agent.Line("inbound", "hello", who="mila",
                           ts="2026-09-02T14:05:00Z")
    head = demo_agent.rows([line], 80)[0]
    assert "mila" in head.text and "…" not in head.text
