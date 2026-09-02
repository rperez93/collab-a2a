"""The roster viewer's bottom row.

The row was a key legend, and a legend is the one thing on a screen that can be
cut anywhere without anybody noticing. Everything that has since been put on it
can not: a batch figure two agents steer by, this agent's remaining quota, a
notice saying the view is not live, and whatever a user's own command printed.

So these tests are about what the row refuses to do — draw a remembered count
as a current one, drop the notice to make room, measure wide characters in the
wrong unit, or run a subprocess on the redraw path.
"""

from __future__ import annotations

import contextlib
import curses
import io
import json
import os
import threading
import time

import pytest

from collab import config
from collab.client import statusbar as sb
from collab.client import tui
from collab.config import SessionProfile


def _fit(parts, width):
    """Through the viewer's own measure and clip, which is the point of them."""
    return sb.fit(parts, width, tui._w, tui._clip)


# --- what is on the row, and in what order ----------------------------------

def test_everything_that_has_something_to_say_is_on_it():
    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit",
                       batch={"done": 6, "total": 10, "fetched_at": time.time()},
                       stats={"cost_usd": 3.1}, command="main")
    assert parts[0] == "⏸ 4 new below", "the notice comes first"
    assert [p.split()[0] for p in parts[1:]] == ["batch", "$3.10", "main", "q:"]


def test_an_unknown_segment_name_costs_that_segment_and_not_the_row():
    """The list is edited by hand; a typo in it must not empty the row."""
    parts = sb.compose(keys="q: quit", segments=("batch", "nonsense", "keys"))
    assert parts == ["q: quit"]


def test_a_reordered_list_is_honoured():
    parts = sb.compose(keys="q: quit", stats={"cost_usd": 1.0},
                       segments=("keys", "stats"))
    assert parts == ["q: quit", "$1.00"]


# --- what survives a narrow pane --------------------------------------------

def _full():
    return sb.compose(notice="⏸ 4 new below", keys="q: quit · G: newest",
                      batch={"done": 6, "total": 10, "fetched_at": time.time()},
                      stats={"cost_usd": 3.1}, command="main")


def test_the_keys_are_the_first_thing_given_up():
    line = _fit(_full(), 60)
    assert "batch" in line and "$3.10" in line and "main" in line
    assert "q: quit" not in line


def test_a_segment_is_asked_for_a_shorter_form_before_it_is_dropped():
    """Dropping alone got the priorities right and the outcome wrong.

    The key legend is ninety columns and goes first, so on a pane under about a
    hundred and thirty — most of them, with a batch and a quota beside it — the
    keys vanished entirely, and the legend is how anybody learns the viewer.
    """
    parts = sb.compose(keys=("the whole legend, at some length", "q: quit"),
                       batch={"done": 6, "total": 10, "fetched_at": time.time()})
    assert "the whole legend" in _fit(parts, 80)
    narrowed = _fit(parts, 40)
    assert "q: quit" in narrowed and "6/10" in narrowed


def test_a_shorter_form_that_still_does_not_fit_is_given_up_anyway():
    parts = sb.compose(keys=("the whole legend, at some length", "q: quit"),
                       batch={"done": 6, "total": 10, "fetched_at": time.time()})
    line = _fit(parts, 26)
    assert "q: quit" not in line and "6/10" in line


def test_the_batch_figure_is_the_last_thing_given_up():
    """It is the only number on the row that BOTH agents are steering by.

    A viewer that hid it at eighty columns is one where the two of them quietly
    stop sharing a figure, which is the whole point of the batch.
    """
    line = _fit(_full(), 38)
    assert "$3.10" not in line and "main" not in line
    assert "6/10" in line


def test_the_notice_is_never_given_up():
    """It is the reader's only sign that what they are looking at is not live.

    Dropped for width, the row goes on showing a batch bar and a quota over a
    conversation that stopped updating twenty messages ago.
    """
    for width in (80, 40, 20, 15):
        assert "new below" in _fit(_full(), width), f"lost at {width}"
    # And it is the FIRST thing on the row at every one of those widths, not
    # merely somewhere on it: giving up from the left would keep the notice
    # last and longest, and the eye lands on the left.
    # 8 is narrower than the notice itself: it is clipped there rather than
    # given up, which is the difference between «the row ran out of space» and
    # «the row said nothing».
    for width in (80, 40, 20, 15, 8):
        assert _fit(_full(), width).startswith(" ⏸"), f"not first at {width}"


def test_the_row_is_measured_in_columns_and_not_in_characters():
    """A CJK column is one character and two columns wide.

    The row used to be cut with `line[:width - 1]`, which counts characters. A
    user command printing a branch name in Japanese — or any of the block bar,
    the `⏸` or the `→` already on the row — then over-ran the pane by however
    many wide characters it held, and a write past the last cell is what ends
    the viewer rather than the frame.
    """
    parts = sb.compose(keys="", command="機能追加ブランチ作業中です", segments=("command",))
    for width in (30, 20, 12, 6):
        assert tui._w(_fit(parts, width)) <= width, f"over-ran at {width}"


def test_nothing_is_drawn_into_a_pane_with_no_room():
    assert _fit(_full(), 1) == ""
    assert _fit([], 40) == ""


# --- the batch segment refuses the same four things the host line does ------

def test_a_remembered_count_is_never_drawn_as_a_bar():
    """The hub counts the batch, so a client that cannot reach it holds the
    previous answer. Drawn plainly it is indistinguishable from a current one."""
    stale = {"done": 6, "total": 10, "fetched_at": time.time() - 600}
    text = sb.batch_segment(stale)
    assert text.startswith("batch ?") and "10m old" in text
    assert "6/10" not in text and "█" not in text


def test_an_empty_batch_has_no_percentage_to_show():
    assert sb.batch_segment({"done": 0, "total": 0, "fetched_at": time.time()}) == ""
    assert sb.batch_segment(None) == ""


def test_a_closed_batch_is_off_the_row():
    """It is over, and somebody said so. The row is for work under way."""
    closed = {"done": 6, "total": 10, "fetched_at": time.time(), "state": "closed"}
    assert sb.batch_segment(closed) == ""


def test_a_finished_batch_says_so_rather_than_vanishing():
    done = {"done": 10, "total": 10, "fetched_at": time.time()}
    assert sb.batch_segment(done).endswith("100% 10/10 done")


def test_a_scope_change_is_explained_while_it_is_still_news():
    now = time.time()
    moved = {"done": 7, "total": 12, "fetched_at": now,
             "total_delta": 2, "delta_at": now}
    assert "+2" in sb.batch_segment(moved, now=now)


def test_figures_that_are_not_figures_do_not_take_the_row_with_them():
    """They came off the hub and through a file: a remote party chose them."""
    assert sb.batch_segment({"done": "x", "total": [1], "fetched_at": time.time()}) == ""
    assert sb.batch_segment("not a dict") == ""


# --- your own usage ----------------------------------------------------------

def test_your_own_quota_and_spend_are_on_the_row():
    figures = {"quotas": {"five_hour": {"used_pct": 88}}, "cost_usd": 3.1}
    assert sb.stats_segment(figures) == "quota 5h 88% · $3.10"


def test_an_agent_that_reports_nothing_gets_no_empty_shell():
    assert sb.stats_segment({}) == ""
    assert sb.stats_segment(None) == ""


def test_the_row_does_not_repeat_the_roster_row_above_it():
    """The rows already carry the model, the repo and the context share for
    everybody. Quota and spend are what you would otherwise scroll to find."""
    figures = {"model": "Opus 5", "context_pct": 40, "tokens_in": 90_000,
               "cost_usd": 3.1}
    assert sb.stats_segment(figures) == "$3.10"


# --- the user's own command --------------------------------------------------

def test_the_command_never_runs_on_the_draw_path():
    """The viewer redraws four times a second.

    Run inside the draw, a command taking half a second freezes the pane for
    half of every second, and one that hangs freezes it until the timeout.
    """
    segment = sb.CommandSegment()
    started = time.monotonic()
    assert segment.poll("sleep 1; echo late", 30)
    assert time.monotonic() - started < 0.5, "the draw waited for it"
    assert segment.text() == "", "and it had nothing yet"

    for _ in range(60):
        if segment.text():
            break
        time.sleep(0.1)
    assert segment.text() == "late", "it landed afterwards"


def test_a_control_character_never_reaches_the_terminal():
    """This is text on its way to a terminal, and an ESC in it is not text.

    It is the user's own command, but `git log -1 --format=%s` puts somebody
    else's commit subject on the row.
    """
    segment = sb.CommandSegment()
    segment._run("printf 'main\\033[2Jwiped'")
    assert segment.text() == "main[2Jwiped"


def test_only_the_first_line_is_used():
    segment = sb.CommandSegment()
    segment._run("printf 'first\\nsecond\\n'")
    assert segment.text() == "first"


def test_a_command_that_fails_renders_nothing():
    """Not the error. The row would print it four times a second."""
    segment = sb.CommandSegment()
    segment._run("echo half-done; exit 3")
    assert segment.text() == ""


def test_a_command_that_hangs_is_abandoned_rather_than_waited_on():
    segment = sb.CommandSegment(timeout=0.2)
    segment._run("sleep 5")
    assert segment.text() == ""


def test_a_command_missing_from_the_machine_is_not_a_crash():
    segment = sb.CommandSegment()
    segment._run("collab-no-such-command-anywhere")
    assert segment.text() == ""


def test_it_is_not_re_run_before_its_interval():
    segment = sb.CommandSegment()
    now = time.time()
    assert segment.poll("true", 30, now=now)
    segment._running = False
    assert not segment.poll("true", 30, now=now + 5)
    assert segment.poll("true", 30, now=now + 31)


def test_a_slow_command_does_not_pile_up_behind_itself():
    """One five-second command on a thirty-second timer became an unbounded
    pile of shells the moment it was slower than its own interval."""
    segment = sb.CommandSegment()
    now = time.time()
    assert segment.poll("sleep 2", 1, now=now)
    assert not segment.poll("sleep 2", 1, now=now + 10)
    assert not segment.poll("sleep 2", 1, now=now + 20)


def test_taking_the_command_away_takes_its_text_off_the_row():
    segment = sb.CommandSegment()
    segment._run("echo main")
    assert segment.text() == "main"
    segment.poll("", 30)
    assert segment.text() == ""


# --- the settings ------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "global_config_path", lambda: path)
    config._CACHE.clear()
    yield path
    config._CACHE.clear()


def test_everything_is_on_by_default(cfg):
    settings = config.watch_status_settings()
    assert settings["enabled"] is True
    assert settings["segments"] == config.WATCH_STATUS_SEGMENTS
    assert settings["command"] == ""
    assert settings["interval"] == config.DEFAULT_WATCH_STATUS_INTERVAL


def test_an_unknown_name_in_the_file_costs_that_segment_only(cfg):
    cfg.write_text(json.dumps({"watch_status_segments": ["batch", "nope", "keys"]}))
    assert config.watch_status_settings()["segments"] == ("batch", "keys")


def test_a_segments_value_that_is_not_a_list_falls_back(cfg):
    """Read on the draw path of a curses program: a TypeError out of here is
    not an error message, it is a terminal left in a broken state."""
    cfg.write_text(json.dumps({"watch_status_segments": "batch"}))
    assert config.watch_status_settings()["segments"] == config.WATCH_STATUS_SEGMENTS


def test_a_broken_interval_falls_back_and_a_zero_one_is_floored(cfg):
    cfg.write_text(json.dumps({"watch_status_interval": "soon"}))
    assert config.watch_status_settings()["interval"] == config.DEFAULT_WATCH_STATUS_INTERVAL
    cfg.write_text(json.dumps({"watch_status_interval": 0}))
    assert config.watch_status_settings()["interval"] == config.DEFAULT_WATCH_STATUS_INTERVAL
    cfg.write_text(json.dumps({"watch_status_interval": 1}))
    assert config.watch_status_settings()["interval"] == config.MIN_WATCH_STATUS_INTERVAL


def test_a_change_made_in_another_terminal_reaches_an_open_pane(cfg):
    """The same live reload the theme has: `load_config` re-reads on the mtime,
    and the row asks it once per frame."""
    assert config.watch_status_settings()["enabled"] is True
    config.save_watch_status(enabled=False)
    assert config.watch_status_settings()["enabled"] is False


# --- the row on a real draw --------------------------------------------------

class _Pane:
    """Just enough curses window to capture what was written where."""

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


def _viewer(tmp_path, cfg_path, view="both"):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="u", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save()
    model = tui.Model(profile=profile)
    model.snapshot = {"participants": [{"name": "bob", "connected": True}],
                      "fetched_at": time.time()}
    model.status = {"batch": {"done": 6, "total": 10, "fetched_at": time.time()}}
    model.own_stats = {"cost_usd": 3.1}
    model._state = "live"
    return tui.Tui(model, view=view)


def _draw(viewer, win):
    try:
        viewer._draw(win)
    except curses.error:
        pass                       # no real terminal; the text is what matters


def test_the_bottom_row_carries_the_batch_and_your_spend(tmp_path, cfg):
    viewer, win = _viewer(tmp_path, cfg), _Pane()
    _draw(viewer, win)
    assert "batch" in win.rows[29] and "6/10" in win.rows[29]
    assert "$3.10" in win.rows[29]


def test_a_wide_character_on_the_row_never_over_runs_the_pane(tmp_path, cfg):
    """The whole path, not just the composition: the viewer's own measure and
    clip have to be the ones the row is fitted with.

    A write past the last cell of a pane is what ends the viewer rather than
    the frame — `addnwstr() returned ERR`, straight out through curses.wrapper.
    """
    # The command alone, so the wide text is what has to be CUT rather than
    # something the fit can drop its way out of. 24 columns is the narrowest
    # pane the viewer draws at all.
    #
    # The configured command is the SAME command that is seeded, deliberately.
    # The draw polls on a timer and runs it in a thread, so a DIFFERENT command
    # racing to finish mid-test swaps the row's contents underneath the
    # assertion. It was `echo` here, whose empty output blanked the row: the
    # test passed only while the thread lost the race, and it stopped passing
    # the moment anything shifted the timing. Seeding and configuring the same
    # command makes the row's contents the same whoever wins.
    wide = "printf '機能追加ブランチ作業中です\\n'"
    config.save_watch_status(command=wide, segments=["command"])
    viewer = _viewer(tmp_path, cfg)
    viewer._command._run(wide)
    for width in (110, 40, 30, 24):
        win = _Pane(width=width)
        _draw(viewer, win)
        assert win.rows.get(29), f"nothing drawn at {width}"
        assert tui._w(win.rows[29]) <= width - 1, f"over-ran at {width}"


def test_the_keys_survive_an_ordinary_terminal(tmp_path, cfg):
    """110 columns with a batch running is the everyday case, and it is the one
    that lost the legend entirely when the only answer to width was to drop."""
    viewer, win = _viewer(tmp_path, cfg), _Pane(width=110)
    _draw(viewer, win)
    assert "q: quit" in win.rows[29]


def test_turning_the_row_off_gives_its_line_back_to_the_panes(tmp_path, cfg):
    """Reserved unconditionally, hiding the row bought a blank line rather
    than a line of conversation."""
    viewer, win = _viewer(tmp_path, cfg), _Pane()
    _draw(viewer, win)
    with_row = viewer.chat.rows
    assert 29 in win.rows

    config.save_watch_status(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    assert 29 not in win.rows, "the row is gone"
    assert viewer.chat.rows == with_row + 1, "and the pane grew into it"


# --- what the command path must survive -------------------------------------

def _settle(segment, seconds=5.0):
    """Wait for the background run to finish, rather than sleeping a fixed gap."""
    deadline = time.monotonic() + seconds
    while segment._running and time.monotonic() < deadline:
        time.sleep(0.02)


def test_output_that_is_not_utf8_is_replaced_rather_than_fatal(tmp_path):
    """`text=True` decodes STRICTLY, and plenty of ordinary commands do not.

    `ls` over a Latin-1 filename, `cat` of a Latin-1 file, `git log` under
    `i18n.logOutputEncoding=latin1`, `grep -a` over a binary. Every one of them
    raises UnicodeDecodeError, which is a ValueError — so it walked straight
    through the `(OSError, SubprocessError)` this thread was catching.
    """
    bad = tmp_path / "latin1.txt"
    bad.write_bytes("caf\xe9 au lait\n".encode("latin-1"))

    segment = sb.CommandSegment()
    segment._run(f"cat {bad}")

    assert segment.text().startswith("caf"), "the readable part still lands"


def test_a_command_that_cannot_be_decoded_does_not_disable_the_segment(tmp_path):
    """The thread died before clearing `_running`, so `poll` refused for ever.

    One undecodable byte, once, and the segment was gone for the rest of the
    session: no text, and no way to ever run again. That is exactly the silent
    death this class exists to avoid.
    """
    bad = tmp_path / "latin1.txt"
    bad.write_bytes(b"\xff\xfe not utf-8 at all\n")

    segment = sb.CommandSegment()
    segment.poll(f"cat {bad}", 30)
    _settle(segment)

    assert segment._running is False, "the run flag was left set"
    assert segment.poll("echo recovered", 0, now=time.time() + 10_000), \
        "the segment never ran again"
    _settle(segment)
    assert segment.text() == "recovered"


def test_the_thread_never_dies_and_so_never_paints_a_traceback(tmp_path):
    """An escaping exception reaches `threading.excepthook`, which writes a
    traceback to stderr — and under curses stderr IS the pane. Measured at 1636
    bytes of it, painted over the conversation, out of a segment whose whole
    promise is that it cannot disturb the draw.

    The hook is watched directly rather than by capturing stderr or by reading
    pytest's warning. Both of those miss it: pytest installs its own
    `threading.excepthook`, so `redirect_stderr` sees nothing, and the warning
    it raises instead arrives at teardown, after `recwarn` has been read. The
    hook is the thing that does the printing, so the hook is what to watch.
    """
    bad = tmp_path / "latin1.txt"
    bad.write_bytes(b"\xff\xfe\n")

    died: list = []
    previous = threading.excepthook
    threading.excepthook = died.append
    try:
        segment = sb.CommandSegment()
        segment.poll(f"cat {bad}", 30)
        _settle(segment)
    finally:
        threading.excepthook = previous

    assert not died, f"the thread died with {died[0].exc_type.__name__}"


def test_the_command_cannot_eat_the_viewers_keystrokes():
    """It inherited the viewer's stdin, so it read what the user was typing.

    Under a pty a `head -c 5` in the status row swallowed five characters aimed
    at the viewer. This row is a reader; a reader consumes no input.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"typed at the viewer\n")
    saved = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        sb.CommandSegment()._run("head -c 5")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(write_fd)

    left = os.read(read_fd, 200)
    os.close(read_fd)
    assert left == b"typed at the viewer\n", "the command ate the reader's input"


def test_any_failure_at_all_leaves_the_segment_usable_and_silent(monkeypatch):
    """The broad catch, tested on something the lenient decode cannot mask.

    `errors="replace"` removes the one failure we know about; the catch is for
    the ones we do not. It runs on a thread, so anything that escapes goes to
    `threading.excepthook` and is painted on the pane — there is no caller
    above this to handle it.
    """
    def explode(*a, **kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(sb.subprocess, "run", explode)

    died: list = []
    previous = threading.excepthook
    threading.excepthook = died.append
    try:
        segment = sb.CommandSegment()
        segment.poll("anything", 30)
        _settle(segment)
    finally:
        threading.excepthook = previous

    assert not died, "the thread died"
    assert segment.text() == ""
    assert segment._running is False
    assert segment.poll("echo back", 0, now=time.time() + 10_000)


def test_the_flag_is_cleared_even_by_what_the_catch_does_not_catch(monkeypatch):
    """Which is why it is a `finally` and not the last line of the `try`.

    `except Exception` does not catch a BaseException, and a thread that took
    one would leave `_running` set — and `poll` refuses for ever on a set flag,
    so the segment would be gone for the rest of the session with nothing said.
    """
    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(sb.subprocess, "run", interrupt)

    segment = sb.CommandSegment()
    segment._running = True
    with pytest.raises(KeyboardInterrupt):
        segment._run("anything")

    assert segment._running is False, "the segment was left unable to run again"


# --- a config value that is neither of the two exceptions we caught ----------

def test_an_infinite_interval_does_not_stop_the_viewer_starting(cfg):
    """`int(float("inf"))` raises OverflowError — not TypeError, not ValueError.

    `json.load` accepts a bare `Infinity` token, so this is reachable from a
    hand-edited config. It escaped `Tui.__init__`, which runs BEFORE any
    draw-path guard exists, so `collab watch` did not start at all.
    """
    cfg.write_text('{"watch_status_interval": Infinity}')
    assert (config.watch_status_settings()["interval"]
            == config.DEFAULT_WATCH_STATUS_INTERVAL)


@pytest.mark.parametrize("raw", [
    '{"watch_status_interval": Infinity}',
    '{"watch_status_interval": -Infinity}',
    '{"watch_status_interval": NaN}',
    '{"watch_status_interval": 1e400}',
])
def test_no_number_in_the_file_can_stop_the_row_being_read(raw, cfg):
    cfg.write_text(raw)
    assert (config.watch_status_settings()["interval"]
            >= config.MIN_WATCH_STATUS_INTERVAL)


def test_the_viewer_still_constructs_with_a_hostile_config(tmp_path, cfg):
    """`Tui.__init__` reads the settings, and nothing above it catches."""
    cfg.write_text('{"watch_status_interval": Infinity}')
    _viewer(tmp_path, cfg)


# --- and the single-pane views reserve the row on the same terms -------------

@pytest.mark.parametrize("view", ["chat", "roster"])
def test_turning_the_row_off_returns_its_line_in_a_single_pane_view(
        view, tmp_path, cfg):
    """The split view was guarded and these were not.

    `_draw_single` does the same arithmetic on its own line, so it could drift
    from `_draw` without a single test noticing — and it did not, but nothing
    was watching.
    """
    viewer = _viewer(tmp_path, cfg, view=view)
    pane = viewer.chat if view == "chat" else viewer.roster

    win = _Pane()
    _draw(viewer, win)
    with_row = pane.rows
    assert 29 in win.rows, "the row is drawn in this view"

    config.save_watch_status(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    assert 29 not in win.rows, "the row is gone"
    assert pane.rows == with_row + 1, "and the pane grew into it"
