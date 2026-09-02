"""Two misses a reviewer found after the fact.

Both are the same shape as everything else in this area: a value that is right
in the common case and quietly wrong in the one nobody typed out.
"""

from __future__ import annotations

import curses
import time
import types
from urllib.parse import urlsplit

import pytest

from collab.client import tui
from collab.client.daemon import _is_loopback
from collab.config import SessionProfile
from collab.server.session import HubConfig


def _cfg(bind):
    return HubConfig(session_id="s", host_name="alice", port=9000, bind=bind,
                     invite="inv", host_token="t")


# --- an address has to be one a client can dial -----------------------------

@pytest.mark.parametrize("bind, expected_host", [
    ("127.0.0.1", "127.0.0.1"),
    ("localhost", "127.0.0.1"),
    ("0.0.0.0", "127.0.0.1"),       # answers on loopback as well
    ("::", "127.0.0.1"),
    ("::1", "::1"),                 # IPv6 loopback, the case the policy serves
    ("fe80::1", "fe80::1"),
    ("192.168.1.50", "192.168.1.50"),
    ("[::1]", "::1"),               # already bracketed, left alone
])
def test_every_bind_form_parses_back_to_the_host_that_was_typed(bind, expected_host):
    """`--bind` is free-form. Unbracketed, `::1` composed to `http://::1:9000`,
    which no client can parse; and `fe80::1` composed to something that parses
    perfectly well as the host `fe80` — not an error, just somewhere else."""
    assert urlsplit(_cfg(bind).local_url).hostname == expected_host


def test_the_ipv6_loopback_is_recognised_as_loopback():
    """Otherwise the guard refuses the very address it exists to allow."""
    assert _is_loopback(_cfg("::1").local_url)
    assert _is_loopback(_cfg("::").local_url)


def test_a_link_local_address_is_still_refused():
    """Bracketing makes it parseable, not local."""
    assert not _is_loopback(_cfg("fe80::1").local_url)
    assert not _is_loopback(_cfg("192.168.1.50").local_url)


# --- and the count above the roster is a claim like any other ---------------

class _Pane:
    """Just enough curses window to capture what was written where."""

    def __init__(self, height=30, width=110):
        self.size = (height, width)
        self.text: list[str] = []

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, *a):
        self.text.append(text[:n])

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def _model(tmp_path, *, state):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="u", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save()
    model = tui.Model(profile=profile)
    model.snapshot = {"participants": [
        {"name": "alice", "connected": True, "is_host": True},
        {"name": "bob", "connected": True},
        {"name": "edith", "connected": False},
    ], "fetched_at": time.time() - 300}
    model._state = state
    return model


def _drawn(tmp_path, state):
    pane = tui.Tui(_model(tmp_path, state=state))
    win = _Pane()
    try:
        pane._draw(win)
    except curses.error:
        pass                       # no real terminal; the text is what matters
    return " ".join(win.text)


def test_while_connected_the_count_is_a_count(tmp_path):
    assert "2/3 online" in _drawn(tmp_path, "live")


def test_once_the_feed_is_gone_it_stops_claiming_who_is_online(tmp_path):
    """It sat one column from a badge reading `offline`, saying `2/3 online`."""
    drawn = _drawn(tmp_path, "offline")
    assert "online" not in drawn
    assert "3 here, none confirmed" in drawn


def test_reconnecting_counts_as_not_knowing(tmp_path):
    assert "online" not in _drawn(tmp_path, "reconnecting")
