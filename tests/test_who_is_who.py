"""Who the viewer and the status line say you are, and who they say the host is.

Both surfaces decided host-ness by comparing two names — «if my name is the
host's name, I am the host». Two agents on one machine resolve the same default
display name, so the guest was labelled `(host)` and two windows read
identically. `status.json` and the profile both carry `is_host`; that is the
fact, and the name clash is exactly the case the rule has to decide. The rule
lives in one place, `statusbar.who`, and these tests hold both surfaces to it.
"""

from __future__ import annotations

import curses
import time

import pytest

from collab.client import statusbar as sb
from collab.client import tui
from collab.config import SessionProfile


# --- the rule ----------------------------------------------------------------

def test_the_host_is_named_as_the_host():
    assert sb.who("alice", "alice", is_host=True) == "alice (host)"


def test_a_guest_points_at_the_host():
    assert sb.who("bob", "alice", is_host=False) == "bob → alice"


def test_a_guest_with_the_hosts_name_is_still_a_guest():
    """The whole reason the rule is not a name comparison."""
    assert sb.who("perez", "perez", is_host=False) == "perez (guest) → perez"


def test_without_the_fact_the_name_decides_as_it_used_to():
    """Files from a daemon that never wrote `is_host`."""
    assert sb.who("alice", "alice", is_host=None) == "alice (host)"
    assert sb.who("bob", "alice", is_host=None) == "bob → alice"


def test_a_shared_checkout_names_the_state_directory():
    """Two agents in one checkout, two windows, one login name: the directory
    is the one thing about them that is guaranteed to differ."""
    assert sb.who("perez", "perez", is_host=False, where=".collab-bob") \
        == "perez (guest) → perez [.collab-bob]"
    assert sb.who("perez", "perez", is_host=True, where=".collab") \
        == "perez (host) [.collab]"


def test_the_directory_is_named_only_when_there_is_more_than_one_claim(
        monkeypatch, tmp_path):
    """One agent in the repo needs no label; the label is for telling apart."""
    homes = [tmp_path / ".collab", tmp_path / ".collab-bob"]
    monkeypatch.setattr(sb, "candidate_homes", lambda cwd=None: homes)

    monkeypatch.setattr(sb, "_claim_held", lambda home: home == homes[0])
    assert sb.state_dir_label(str(homes[0])) == ""

    monkeypatch.setattr(sb, "_claim_held", lambda home: True)
    assert sb.state_dir_label(str(homes[1])) == ".collab-bob"
    assert sb.state_dir_label(str(homes[0])) == ".collab"


def test_the_label_never_raises_on_the_draw_path(monkeypatch):
    def explode(cwd=None):
        raise OSError("gone")
    monkeypatch.setattr(sb, "candidate_homes", explode)
    assert sb.state_dir_label("/nowhere/.collab") == ""


# --- the viewer's title bar --------------------------------------------------

class _Pane:
    def __init__(self, height=30, width=110):
        self.size = (height, width)
        self.rows: dict[int, str] = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, *a):
        self.rows[y] = self.rows.get(y, "") + text[:n]

    def __getattr__(self, _name):
        return lambda *a, **kw: None


@pytest.fixture(autouse=True)
def _no_terminal(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda n: 0)
    monkeypatch.setattr(curses, "ACS_HLINE", ord("-"), raising=False)


def _viewer(tmp_path, *, name, host, is_host, status=None):
    # One agent in a scratch checkout: the title carries no directory label,
    # whatever the test process's own ancestry happens to claim.
    sb_label, sb.state_dir_label = sb.state_dir_label, lambda home, cwd=None: ""
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="u", name=name,
                             host_name=host, token="t", home=str(home),
                             is_host=is_host)
    profile.save()
    model = tui.Model(profile=profile)
    model.snapshot = {"participants": [], "fetched_at": time.time()}
    model.status = dict(status or {})
    model._state = "live"
    viewer = tui.Tui(model, view="both")
    viewer._where = lambda: ""
    sb.state_dir_label = sb_label
    return viewer


def _title(viewer):
    win = _Pane()
    try:
        viewer._draw(win)
    except curses.error:
        pass
    return win.rows[0]


def test_the_title_calls_the_host_the_host(tmp_path):
    assert "perez (host)" in _title(
        _viewer(tmp_path, name="perez", host="perez", is_host=True))


def test_the_title_does_not_call_a_same_named_guest_the_host(tmp_path):
    title = _title(_viewer(tmp_path, name="perez", host="perez", is_host=False))
    assert "(host)" not in title
    assert "perez (guest) → perez" in title


def test_the_title_and_the_status_line_agree(tmp_path):
    """One rule, two surfaces — the reader has both on screen."""
    from collab.statusline import render as r
    title = _title(_viewer(tmp_path, name="perez", host="perez", is_host=False))
    line = r.render({"name": "perez", "host": "perez", "is_host": False,
                     "state": "live", "heartbeat": time.time()})
    assert "perez (guest) → perez" in title
    assert "perez (guest) → perez" in line


def test_the_title_names_an_outdated_daemon(tmp_path):
    title = _title(_viewer(tmp_path, name="bob", host="alice", is_host=False,
                           status={"version": "1.22.2"}))
    assert "daemon v1.22.2 — restart it" in title


def test_the_title_shows_a_current_daemons_version_plainly(tmp_path):
    from collab import __version__
    title = _title(_viewer(tmp_path, name="bob", host="alice", is_host=False,
                           status={"version": __version__}))
    assert f"v{__version__}" in title
    assert "restart" not in title
