"""Two figures off the wire, held to the rule the file beside them states.

`statusbar._counted` exists because a count that is not a count must render
nothing rather than raise: this runs on the draw path of a curses program, and
`render.main` wraps the whole call in a bare `except Exception: return 0`, so a
hostile or merely broken `unread_messages` did not blank the badge — it blanked
the ENTIRE status line. Three lines below the guard for `messages`, the sibling
field was read with `int(x or 0)`, which turns `True` into `✉ 1` and `"lots"`
into a traceback.

And `columns.clip` answered a budget of zero with a one-column ellipsis, which is
a one-column over-run of a zero-column budget.
"""

from __future__ import annotations

import time

import pytest

from collab import columns
from collab.statusline import render as r


def _status(**over):
    base = {"state": "live", "heartbeat": time.time(), "name": "bob",
            "host": "alice", "is_host": False, "others_connected": 1,
            "version": r.__version__ if hasattr(r, "__version__") else ""}
    base.update(over)
    return base


@pytest.mark.parametrize("junk", [
    True, False, "lots", "3", 3.7, -1, [3], {"n": 3}, None,
])
def test_a_junk_unread_count_costs_the_badge_and_not_the_line(junk):
    """The line must still say who you are; only the badge is withheld."""
    out = r.render(_status(unread_messages=junk), width=200)
    assert out, f"the whole status line vanished for unread_messages={junk!r}"
    assert "✉" not in out, f"a badge was drawn for unread_messages={junk!r}: {out}"
    assert "bob" in out


def test_a_real_unread_count_is_still_drawn():
    out = r.render(_status(unread_messages=2), width=200)
    assert "✉ 2" in out


def test_a_zero_unread_count_draws_no_badge():
    out = r.render(_status(unread_messages=0), width=200)
    assert "✉" not in out


@pytest.mark.parametrize("limit", [0, -1, -40])
def test_no_budget_means_no_output_not_an_ellipsis(limit):
    """A zero-column budget answered with «…» is a one-column over-run."""
    assert columns.clip("anything at all", limit) == ""
    assert columns.width(columns.clip("機能追加", limit)) <= max(limit, 0)


def test_a_budget_of_one_is_an_ellipsis_at_most():
    assert columns.width(columns.clip("anything at all", 1)) <= 1
