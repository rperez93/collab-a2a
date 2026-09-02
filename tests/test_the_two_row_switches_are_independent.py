"""Two keys, two rows, and neither may switch the other off.

`watch_status` governs the row that carries the READER'S figures — their quota,
their spend, their own command. `watch_status_roster` governs the row that
carries the SESSION'S — the shared batch and the message count. They exist as
two keys because they answer two different questions, and somebody who turns
off the one about themselves has said nothing about the one about everybody.

`_hint` returned early on `watch_status` before it reached the roster branch,
so in the roster-only view — the pane the feature's own description argues
needs those figures most, having no title bar to carry them — turning off the
personal row silently took the session row with it.
"""

from __future__ import annotations

import pytest

from collab.client import tui as T


CASES = [
    # personal, session, does the roster-only pane draw a row?
    (True, True, True),
    (True, False, True),    # the personal row is still allowed to draw
    (False, True, True),    # THE REGRESSION: the session row must survive
    (False, False, False),  # nothing asked for, nothing drawn
]


@pytest.mark.parametrize("personal,session,expected", CASES)
def test_the_roster_only_pane_honours_its_own_switch(personal, session,
                                                     expected):
    """The switch that owns the row is the one that decides it."""
    drew = _draws_a_row(personal=personal, session=session)
    assert drew is expected, (
        f"watch_status={personal} watch_status_roster={session}: "
        f"{'no row' if not drew else 'a row'} where "
        f"{'a row' if expected else 'no row'} was wanted")


def _draws_a_row(*, personal: bool, session: bool) -> bool:
    """Whether `_hint` paints anything, with the two switches as given."""
    painted: list[tuple[int, str]] = []

    class Win:
        def addnstr(self, y, x, text, n, attr=0):
            if text.strip():
                painted.append((y, text))

        def __getattr__(self, name):
            return lambda *a, **kw: None

    tui = T.Tui.__new__(T.Tui)
    tui._bar = personal
    tui._settings = {"enabled": personal, "segments": ("batch", "keys")}
    tui._roster_settings = {"enabled": session, "segments": ("batch",
                                                             "messages",
                                                             "keys")}
    tui.model = _Model()
    tui.chat = _Chat()
    tui._command = _Command()
    tui.behind = lambda: 0
    tui._paint_bar = lambda win, y, width, parts, behind=0, keep=1: painted.append(
        (y, "".join(str(p) for p in parts) or "·"))
    T.Tui._hint(tui, Win(), 20, 80, notice=False, roster=True,
                keys=(T.ROSTER_KEYS, T.ROSTER_KEYS_SHORT))
    return bool(painted)


class _Model:
    status = {"batch": None, "messages": None}
    own_stats = {}


class _Chat:
    follow = True


class _Command:
    def text(self):
        return ""
