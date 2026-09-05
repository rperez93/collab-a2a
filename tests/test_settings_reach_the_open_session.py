"""A setting changed in one terminal reaches the session already open in another.

`load_config` re-reads the file whenever its stamp moves, and the viewer, the
daemon and the wake all ask it live — so for nearly every setting, writing the
file IS the change. These tests are about the four that were not: the fold
and the timezone, which the row cache did not key on, so a change waited for
the next message to show; the roster's share of the window and the built-in
layout, read once at launch; and the name and the colour, which
`collab config` wrote to the file and never told the hub about, unlike
`collab name` and `collab color`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from collab import cli, config
from collab.client import tui as tui_mod
from collab.client.tui import Tui
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope

from test_tui_scroll import FakeModel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(tui_mod, "ROSTER_SHARE_PINNED", None)
    config._CACHE.clear()
    yield tmp_path
    config._CACHE.clear()


def _viewer(**kw) -> Tui:
    model = FakeModel()
    model.events = [Envelope(kind=KIND_CHAT, sender="bob", seq=n,
                             ts="2026-09-05T10:00:00Z",
                             text="\n".join(f"line {i}" for i in range(12)))
                    for n in range(1, 4)]
    return Tui(model, **kw)


# --- the fold and the timezone rebuild the rows on their own ------------------

def test_a_fold_change_reaches_the_rows_with_nobody_talking():
    viewer = _viewer()
    before = viewer._conversation(80)
    config.set_fold_override(2)
    after = viewer._conversation(80)
    assert after is not before, "the rows were served from the cache"
    assert len(after) < len(before), "a tighter fold did not shorten the rows"


def test_a_timezone_change_reaches_the_rows_with_nobody_talking():
    viewer = _viewer()
    # Two pinned zones 26 hours apart, so the clocks differ whatever the
    # machine's own zone happens to be.
    config.set_timezone("Pacific/Kiritimati")
    before = viewer._conversation(80)
    config.set_timezone("Etc/GMT+12")
    after = viewer._conversation(80)
    assert after is not before, "the rows were served from the cache"
    assert [r.text for r in after] != [r.text for r in before], \
        "the clock did not move with the zone"


def test_the_rows_are_still_cached_when_nothing_changed():
    """The other half, which is what makes the cache worth having."""
    viewer = _viewer()
    first = viewer._conversation(80)
    for _ in range(5):
        assert viewer._conversation(80) is first


# --- the roster's share follows the setting --------------------------------

def test_the_roster_share_follows_the_setting_while_open():
    assert tui_mod.roster_share() == pytest.approx(0.30)
    config.save_watch_settings(roster_size=45)
    assert tui_mod.roster_share() == pytest.approx(0.45)


def test_a_share_given_on_the_command_line_is_not_overruled(monkeypatch):
    monkeypatch.setattr(tui_mod, "ROSTER_SHARE_PINNED", 0.60)
    config.save_watch_settings(roster_size=45)
    assert tui_mod.roster_share() == pytest.approx(0.60)


# --- the built-in layout follows the setting ----------------------------------

def test_the_view_follows_watch_layout_while_open():
    viewer = _viewer(view="both", follow_layout=True)
    config.save_watch_settings(layout="chat")
    assert viewer.adopt_layout() is True
    assert viewer.view == "chat" and viewer.focus == "chat"

    config.save_watch_settings(layout="roster")
    assert viewer.adopt_layout() is True
    assert viewer.view == "roster" and viewer.focus == "roster"

    config.unset_setting("watch_layout")
    assert viewer.adopt_layout() is True
    assert viewer.view == "both"


def test_tmux_reads_as_the_split_inside_one_window():
    """The second pane is tmux's to open, at the next `collab watch`."""
    viewer = _viewer(view="both", follow_layout=True)
    config.save_watch_settings(layout="tmux")
    assert viewer.adopt_layout() is False
    assert viewer.view == "both"
    assert config.layout_view("tmux", "chat") == "chat"


def test_a_view_given_on_the_command_line_is_not_overruled():
    viewer = _viewer(view="chat", follow_layout=False)
    config.save_watch_settings(layout="roster")
    assert viewer.adopt_layout() is False
    assert viewer.view == "chat"


def test_the_viewer_does_not_follow_the_layout_unless_told():
    """`--layout`, `--view` and each half of a tmux pair pin their view; only
    a viewer that took its layout from the config follows the config."""
    assert Tui(FakeModel()).follow_layout is False


# --- the name and the colour reach the hub --------------------------------------

class _Hub:
    def __init__(self):
        self.reports: list[tuple[dict, dict | None]] = []
        self.renamed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def report_stats(self, figures, identity=None):
        self.reports.append((figures, identity))
        return {}

    def rename(self, name):
        self.renamed.append(name)
        return name


@pytest.fixture
def session(tmp_path, monkeypatch):
    """An active session of our own, and a hub that records what it is told."""
    home = tmp_path / "state"
    monkeypatch.setenv("COLLAB_HOME", str(home))
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    hub = _Hub()
    monkeypatch.setattr(cli, "_client", lambda profile: hub)
    profile = SessionProfile(session_id="s1", url="http://h", name="rafael",
                             host_name="rafael", token="t", is_host=True,
                             home=str(home))
    profile.save()
    return hub


def test_config_color_is_published_to_the_open_session(session, capsys):
    assert cli.main(["config", "color", "#00cccc"]) == 0
    assert session.reports == [({}, {"color": "#00cccc"})]
    assert "published to the session" in capsys.readouterr().out


def test_unsetting_the_colour_clears_it_in_the_session_too(session):
    config.set_default_color("#00cccc")
    assert cli.main(["config", "color", "--unset"]) == 0
    assert session.reports == [({}, {"color": ""})]


def test_the_two_ways_of_setting_a_colour_publish_the_same_thing(session):
    cli.main(["color", "#ff7f50"])
    cli.main(["config", "color", "#ff7f50"])
    assert session.reports[0] == session.reports[1]


def test_config_display_name_renames_in_the_open_session(session, monkeypatch):
    monkeypatch.setattr(config, "_git_user_name", lambda: None)
    assert cli.main(["config", "display_name", "Rafael Two"]) == 0
    assert session.renamed == ["rafael-two"]
    assert SessionProfile.current().name == "rafael-two"


def test_an_agent_with_a_name_of_its_own_is_not_renamed_by_the_default(
        session, monkeypatch, capsys):
    """The default is for agents without one; publishing it over an agent's
    own name would rename the wrong agent."""
    monkeypatch.setenv("COLLAB_NAME", "alice")
    assert cli.main(["config", "display_name", "rafael-two"]) == 0
    assert session.renamed == []
    assert "answers to alice" in capsys.readouterr().out


def test_an_agent_with_a_colour_of_its_own_keeps_it(session, tmp_path, capsys):
    from collab import identity

    identity.save(tmp_path / "state", color="#123456", name="rafael")
    assert cli.main(["config", "color", "#00cccc"]) == 0
    assert session.reports == []
    assert "colour of its own" in capsys.readouterr().out


def test_with_no_session_open_the_colour_waits_for_the_join(tmp_path, monkeypatch,
                                                            capsys):
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "empty"))
    monkeypatch.setattr(cli, "_client", lambda profile: pytest.fail("no hub to reach"))
    assert cli.main(["config", "color", "#00cccc"]) == 0
    assert "published when you join" in capsys.readouterr().out


def test_turning_sharing_off_says_the_old_figure_stays(session, capsys):
    """The same line `collab stats --share off` prints: the daemon stops
    reporting at once, and the hub keeps the last figure until told otherwise."""
    assert cli.main(["config", "share_stats", "off"]) == 0
    assert "keep seeing whatever you last shared" in capsys.readouterr().out


# --- and the stamp that decides when the file is read again ------------------------
#
# Every setting above reaches a running session because `load_config` re-reads
# the file when its stamp moves. The stamp is (mtime, size), and «nearly
# always» was doing more work in that sentence than it could carry: two values
# of the same length written inside one mtime tick are one stamp. A colour is
# always seven characters. Two themes can be named alike. `new_when` goes
# between `idle` and `task` without moving a byte.
#
# On a filesystem that stamps whole seconds — the network and FUSE mounts this
# project has met — the second write of such a pair was invisible, and the
# setting stayed at its old value for the life of the process.

def _whole_second_filesystem(path, when):
    """A file stamped the way a coarse filesystem stamps it."""
    os.utime(path, (when, when))


def test_a_same_size_change_inside_one_tick_is_still_seen(isolated):
    """The case the size cannot tell apart, on the clock that cannot either."""
    path = isolated / "config.json"
    tick = int(time.time())

    path.write_text(json.dumps({"new_when": "idle"}))
    _whole_second_filesystem(path, tick)
    assert config.new_when() == "idle"

    path.write_text(json.dumps({"new_when": "task"}))
    _whole_second_filesystem(path, tick)

    assert config.new_when() == "task"


def test_a_colour_is_always_seven_characters_and_still_changes(isolated):
    """The everyday version of the same thing: two `collab color` calls in one
    second differ in no byte the stamp was looking at."""
    path = isolated / "config.json"
    tick = int(time.time())

    path.write_text(json.dumps({"color": "#00cccc"}))
    _whole_second_filesystem(path, tick)
    assert config.default_color() == "#00cccc"

    path.write_text(json.dumps({"color": "#ff8800"}))
    _whole_second_filesystem(path, tick)

    assert config.default_color() == "#ff8800"


def test_a_viewer_already_open_sees_it_too(isolated):
    """Through the running object rather than the accessor, because the frame
    is where this was going to be noticed or not. Two real zones of the same
    length and an hour and a half apart, so the clocks on the rows differ and
    the file does not."""
    path = isolated / "config.json"
    tick = int(time.time())
    path.write_text(json.dumps({"timezone": "Asia/Kolkata"}))
    _whole_second_filesystem(path, tick)
    viewer = _viewer()
    before = viewer._conversation(80)

    path.write_text(json.dumps({"timezone": "Asia/Jakarta"}))
    _whole_second_filesystem(path, tick)
    after = viewer._conversation(80)

    assert after is not before, "the rows were served from the cache"
    assert [r.text for r in after] != [r.text for r in before]


def test_a_file_nobody_has_touched_is_still_answered_from_the_cache(isolated):
    """The fix must not have bought correctness by throwing the cache away: the
    viewer asks this several times a frame, four times a second, and a re-read
    each time is what the cache exists to prevent."""
    path = isolated / "config.json"
    path.write_text(json.dumps({"theme": "classic"}))
    long_ago = time.time() - 3600
    _whole_second_filesystem(path, long_ago)
    config.load_config()

    reads = []
    real = Path.read_text

    def counted(self, *a, **kw):
        if self.name == "config.json":
            reads.append(1)
        return real(self, *a, **kw)

    with mock.patch.object(Path, "read_text", counted):
        for _ in range(200):
            config.load_config()

    assert reads == [], "an old file is read once and then remembered"


def test_a_file_just_written_is_re_read_until_its_stamp_settles(isolated):
    """The cost, stated: a read per call for as long as the stamp is too fresh
    to be believed — which is the second somebody is watching to see whether
    their change worked."""
    path = isolated / "config.json"
    path.write_text(json.dumps({"theme": "classic"}))
    config.load_config()

    reads = []
    real = Path.read_text

    def counted(self, *a, **kw):
        if self.name == "config.json":
            reads.append(1)
        return real(self, *a, **kw)

    with mock.patch.object(Path, "read_text", counted):
        for _ in range(5):
            config.load_config()

    assert len(reads) == 5
    assert config.STAMP_SETTLES > 1.0, "longer than a whole-second tick"


# --- and a read taken inside the window is not believed after it ------------------
#
# Re-reading inside the window closes the case where nobody looked until it had
# passed. It does not close the one where somebody looked DURING it, which is
# the ordinary case rather than a narrow one: a viewer polls four times a second
# and a daemon beats every three, so a poll inside a second-and-a-half window is
# what usually happens.
#
#   write A · a poll lands inside the window, reads A, caches it
#   write B, same second, same size — the stamp does not move
#   the next poll is past the window: matching stamp, settled stamp, and the
#   cached A trusted for good, until some unrelated write moved the stamp
#
# So an entry remembers whether the stamp was still moving when it was filled,
# and such an entry is read again on the first call after the stamp settles.

@pytest.fixture
def coarse_clock(monkeypatch):
    """A filesystem that stamps whole seconds, which is where this bites."""
    real = Path.stat

    def floored(self, **kw):
        st = real(self, **kw)

        class Coarse:
            st_mtime = float(int(st.st_mtime))
            st_size = st.st_size

        return Coarse()

    monkeypatch.setattr(Path, "stat", floored)


def test_a_value_read_during_the_window_is_not_trusted_after_it(isolated,
                                                                coarse_clock):
    """The sequence, in order. The middle read is the one that used to poison
    the cache, and it is the read a running session actually takes."""
    path = isolated / "config.json"

    path.write_text(json.dumps({"new_when": "idle"}))
    assert config.new_when() == "idle"          # a poll inside the window

    path.write_text(json.dumps({"new_when": "task"}))   # same second, same size
    _settle()

    assert config.new_when() == "task"


def test_the_same_holds_for_a_colour(isolated, coarse_clock):
    """Seven characters whatever it is, so the size never moves either."""
    path = isolated / "config.json"

    path.write_text(json.dumps({"color": "#00cccc"}))
    assert config.default_color() == "#00cccc"

    path.write_text(json.dumps({"color": "#ff8800"}))
    _settle()

    assert config.default_color() == "#ff8800"


def test_the_re_read_after_the_window_happens_once_and_not_again(isolated,
                                                                 coarse_clock):
    """One extra read per write, and then the cache is a cache again — which is
    the whole reason the entry is marked rather than the window widened."""
    path = isolated / "config.json"
    path.write_text(json.dumps({"theme": "classic"}))
    config.load_config()                        # filled inside the window
    _settle()

    reads = []
    real = Path.read_text

    def counted(self, *a, **kw):
        if self.name == "config.json":
            reads.append(1)
        return real(self, *a, **kw)

    with mock.patch.object(Path, "read_text", counted):
        for _ in range(50):
            config.load_config()

    assert len(reads) == 1, "the first call after it settles, and no other"


def _settle():
    """Wait for the stamp to be old enough to believe."""
    time.sleep(config.STAMP_SETTLES + 0.2)
