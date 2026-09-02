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


#: Every segment the row can carry, in the order they are given up. The batch
#: is no longer on the row by default — the roster row and the host's status
#: line carry it — so the tests about the RANKING ask for the full list, which
#: is the ranking they are about.
FULL = config.WATCH_STATUS_SEGMENTS


# --- what is on the row, and in what order ----------------------------------

def test_everything_that_has_something_to_say_is_on_it():
    parts = sb.compose(notice="⏸ 4 new below", keys="q: quit",
                       batch={"done": 6, "total": 10, "fetched_at": time.time()},
                       stats={"cost_usd": 3.1}, command="main", segments=FULL)
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
                      stats={"cost_usd": 3.1}, command="main", segments=FULL)


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
                       batch={"done": 6, "total": 10, "fetched_at": time.time()},
                       segments=FULL)
    assert "the whole legend" in _fit(parts, 80)
    narrowed = _fit(parts, 40)
    assert "q: quit" in narrowed and "6/10" in narrowed


def test_a_shorter_form_that_still_does_not_fit_is_given_up_anyway():
    parts = sb.compose(keys=("the whole legend, at some length", "q: quit"),
                       batch={"done": 6, "total": 10, "fetched_at": time.time()},
                       segments=FULL)
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
    assert settings["segments"] == config.DEFAULT_WATCH_STATUS_SEGMENTS
    assert settings["command"] == ""
    assert settings["interval"] == config.DEFAULT_WATCH_STATUS_INTERVAL


def test_an_unknown_name_in_the_file_costs_that_segment_only(cfg):
    cfg.write_text(json.dumps({"watch_status_segments": ["batch", "nope", "keys"]}))
    assert config.watch_status_settings()["segments"] == ("batch", "keys")


def test_a_segments_value_that_is_not_a_list_falls_back(cfg):
    """Read on the draw path of a curses program: a TypeError out of here is
    not an error message, it is a terminal left in a broken state."""
    cfg.write_text(json.dumps({"watch_status_segments": "batch"}))
    assert config.watch_status_settings()["segments"] == config.DEFAULT_WATCH_STATUS_SEGMENTS


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


def test_the_bottom_row_carries_the_batch_when_asked_and_your_spend_regardless(
        tmp_path, cfg):
    """The batch left the default — the roster row and the host's status line
    already carry it — but a reader who wants all three copies may have them."""
    config.save_watch_status(segments=["batch", "stats", "keys"])
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

    BOTH SWITCHES, because the roster pane's row is no longer the reader's.
    `watch_status` governs the row carrying the reader's own figures and
    `watch_status_roster` the one carrying the session's; the roster-only pane
    has a single row and either switch can claim it. Turning off only the
    personal one used to empty that row and now correctly does not, so the
    line is reclaimed when NEITHER is asked for. This assertion read the same
    before and after that change while meaning something different, which is
    the more dangerous half: it went on passing and stopped guarding.
    """
    viewer = _viewer(tmp_path, cfg, view=view)
    pane = viewer.chat if view == "chat" else viewer.roster

    win = _Pane()
    _draw(viewer, win)
    with_row = pane.rows
    assert 29 in win.rows, "the row is drawn in this view"

    config.save_watch_status(enabled=False)
    config.save_watch_roster(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    assert 29 not in win.rows, "the row is gone"
    # The roster's row brings a rule above it, so that pane gets two lines
    # back; the conversation's row has no rule and gives back one.
    grew = 2 if view == "roster" else 1
    assert pane.rows == with_row + grew, "and the pane grew into it"


def test_the_roster_pane_keeps_its_row_when_only_the_personal_one_is_off(
        tmp_path, cfg):
    """The half the parametrised test above can no longer see.

    Somebody who turns off the row about themselves has said nothing about the
    row about everybody — and the roster-only pane is the view with no title
    bar to carry those figures instead, which is the reason the second key
    exists at all.
    """
    viewer = _viewer(tmp_path, cfg, view="roster")
    viewer.model.status = _roster_row()

    config.save_watch_status(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    assert 29 in win.rows, "the session's row went with the reader's"
    # NAMED BY WHAT ONLY IT HAS. `batch` appears on the reader's row too, so
    # asserting it proves a row was drawn and not WHICH row — this test passed
    # against a build where the roster branch was deleted entirely and the pane
    # fell through to the personal bar. The message count is on the session's
    # row alone, and the spend is on the reader's alone, so the pair of them
    # tells the two apart.
    assert "128 messages" in win.rows[29], win.rows[29]
    assert "$3.10" not in win.rows[29], win.rows[29]


# --- the roster panel's own row: figures that are true for everybody ---------
#
# The trap this whole section exists for is that MOST of what the daemon writes
# into `status.json` is written from the VIEWER's point of view.
# `others_connected` and `others_total` exclude the reader by participant id so
# a daemon does not count itself; `unread` and `unread_messages` are properties
# of one inbox; `watchers` and `ws_clients` are that daemon's own subscribers.
# Four participants would read four different numbers off any of them — and
# they would read them beside a hub-counted batch bar that genuinely is shared,
# which lends the false ones credit they have not earned. So this row is built
# only from figures the hub counted once and handed out whole, and the config
# refuses the rest by name rather than by convention.

ROSTER = config.WATCH_ROSTER_SEGMENTS


def _roster_row(**over):
    """`status.json` as a viewer holds it, with the hub's own two figures."""
    now = time.time()
    payload = {"batch": {"done": 6, "total": 10, "fetched_at": now},
               "messages": {"total": 128, "fetched_at": now}}
    payload.update(over)
    return payload


def test_the_roster_row_carries_the_batch_and_the_message_count(cfg):
    status = _roster_row()
    parts = sb.compose(batch=status["batch"], messages=status["messages"],
                       segments=ROSTER)
    assert any("6/10" in p for p in parts), parts
    assert "128 messages" in parts, parts


def test_a_figure_that_is_only_the_readers_cannot_be_put_on_that_row(cfg):
    """`stats` and `command` are refused BY NAME, not left to convention.

    They are real segments one row lower, so nothing about the spelling says
    they do not belong here. Allowed through, they would show each participant
    their own quota on a row that speaks for all of them.
    """
    with pytest.raises(ValueError) as bad:
        config.setting("watch_status_roster_segments").write(["batch", "stats"])
    assert "stats" in str(bad.value)

    config.setting("watch_status_roster_segments").write(["batch", "messages"])
    assert config.watch_roster_settings()["segments"] == ("batch", "messages")


def test_a_hand_edited_name_costs_that_segment_and_not_the_row(cfg):
    """Refused at the command, ignored in the file — the same split the
    reader's own row already makes, and for the same reason."""
    cfg.write_text(json.dumps(
        {"watch_status_roster_segments": ["batch", "stats", "messages"]}))
    assert config.watch_roster_settings()["segments"] == ("batch", "messages")


def test_a_roster_segments_value_that_is_not_a_list_falls_back(cfg):
    """Read on the draw path of a curses program, like every other reader
    here: a TypeError out of this is a terminal left broken."""
    cfg.write_text(json.dumps({"watch_status_roster_segments": "batch"}))
    assert config.watch_roster_settings()["segments"] == ROSTER


@pytest.mark.parametrize("raw", [
    '{"watch_status_roster": Infinity}',
    '{"watch_status_roster": NaN}',
    '{"watch_status_roster_segments": 1e400}',
    '{"watch_status_roster_segments": {"batch": true}}',
])
def test_nothing_in_the_file_can_stop_the_roster_row_being_read(raw, cfg):
    cfg.write_text(raw)
    settings = config.watch_roster_settings()
    assert isinstance(settings["enabled"], bool)
    assert all(name in ROSTER for name in settings["segments"])


def test_both_rows_are_on_by_default(cfg):
    assert config.watch_roster_settings() == {"enabled": True,
                                              "segments": ROSTER}


# --- what the message count refuses to say ----------------------------------

def test_a_remembered_count_is_never_drawn_as_a_current_one():
    """`write_status` keeps writing every three seconds after the hub has gone
    quiet, so a count drawn plainly freezes while looking live.

    The batch figure beside it already says its own age. A count that did not
    would be the same staleness defect, on the same row, next to the one
    segment that gets it right."""
    old = {"total": 128, "fetched_at": time.time() - 600}
    assert sb.messages_segment(old) == "messages ? 10m old"


def test_a_count_with_no_stamp_behind_it_claims_nothing():
    """No successful fetch is a memory of unknown age, not a fresh read."""
    assert sb.messages_segment({"total": 128}) == "messages ?"
    assert sb.messages_segment({"total": 128, "fetched_at": None}) == "messages ?"


def test_a_stamp_in_the_future_is_not_a_fresh_one():
    """A backward clock step — NTP, a VM resuming — is not freshness."""
    assert "?" in sb.messages_segment({"total": 5, "fetched_at": time.time() + 3600})


def test_a_count_that_is_not_a_count_does_not_take_the_row_with_it():
    """For a guest this arrived over the network from somebody else's hub."""
    for junk in ("lots", None, [1], float("nan")):
        assert sb.messages_segment(
            {"total": junk, "fetched_at": time.time()}) == "", junk
    assert sb.messages_segment("not a dict") == ""
    assert sb.messages_segment(None) == ""


def test_one_message_is_not_pluralised():
    assert sb.messages_segment(
        {"total": 1, "fetched_at": time.time()}) == "1 message"


def test_the_batch_on_the_roster_row_is_the_one_renderer():
    """Not a second one. Two drawings of one figure that disagreed would be
    worse than either, and the reader has both rows on screen at once."""
    figures = {"done": 6, "total": 10, "fetched_at": time.time()}
    assert sb.batch_segment(figures) in sb.compose(batch=figures,
                                                   segments=ROSTER)


# --- and on a real draw ------------------------------------------------------

def test_the_roster_panel_has_a_row_of_its_own_at_the_foot_of_the_roster(
        tmp_path, cfg):
    """On the roster's last row, immediately above the conversation header.

    Asserted as «alone on that row», not merely «present on it»: the fake pane
    concatenates everything written to a line, so a participant painted over
    the top of this one would still leave the figures findable there and the
    test would pass on a genuinely overlapping draw.
    """
    viewer, win = _viewer(tmp_path, cfg), _Pane()
    viewer.model.status = _roster_row()
    _draw(viewer, win)

    assert "6/10" in win.rows[9] and "128 messages" in win.rows[9]
    assert not any(name in win.rows[9] for name in ("bob", "alice")), \
        f"a participant was painted onto this row: {win.rows[9]!r}"
    assert "CONVERSATION" in win.rows[10], "and the chat header is right below"


def test_the_conversation_row_keeps_the_readers_own_figures(tmp_path, cfg):
    """It is honestly theirs, so it stays. Only the roster's row is everyone's."""
    viewer, win = _viewer(tmp_path, cfg), _Pane()
    viewer.model.status = _roster_row()
    _draw(viewer, win)

    assert "$3.10" in win.rows[29], "the reader's spend is still on their row"
    assert "128 messages" not in win.rows[29], \
        "and the session's count did not migrate onto it"


def test_turning_it_off_gives_the_row_back_to_the_roster(tmp_path, cfg):
    viewer = _viewer(tmp_path, cfg)
    viewer.model.status = _roster_row()
    win = _Pane()
    _draw(viewer, win)
    with_row = viewer.roster.rows
    assert "6/10" in win.rows[9]

    config.save_watch_roster(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    assert "6/10" not in win.rows.get(9, ""), "the row is gone"
    assert viewer.roster.rows == with_row + 2, \
        "and the roster grew into it, and into the rule above it"


def test_a_session_with_nothing_to_say_does_not_reserve_the_row(tmp_path, cfg):
    """Reserved unconditionally, an empty row is a line stolen from the one
    pane that cannot spare one. No figure is better than a blank."""
    viewer = _viewer(tmp_path, cfg)
    viewer.model.status = {}
    win = _Pane()
    _draw(viewer, win)
    empty = viewer.roster.rows
    assert 9 not in win.rows

    viewer.model.status = _roster_row()
    win = _Pane()
    _draw(viewer, win)
    assert viewer.roster.rows == empty - 2, \
        "the row, and the rule above it, are taken only when used"


def test_a_hub_gone_quiet_takes_the_row_with_it(tmp_path, cfg):
    """The end of the staleness rule, on the draw.

    A daemon that has stopped fetching goes on writing `status.json` every
    three seconds, so the figures in it are remembered rather than observed.
    The batch says its own age; the count says its own age; and nothing on the
    row is left asserting a current number.
    """
    viewer = _viewer(tmp_path, cfg)
    stopped = time.time() - 600
    viewer.model.status = {"batch": {"done": 6, "total": 10, "fetched_at": stopped},
                           "messages": {"total": 128, "fetched_at": stopped}}
    win = _Pane()
    _draw(viewer, win)

    assert "6/10" not in win.rows[9] and "128 messages" not in win.rows[9]
    assert "batch ? 10m old" in win.rows[9] and "messages ? 10m old" in win.rows[9]


def test_it_gives_up_its_row_before_squeezing_the_roster(tmp_path, cfg):
    """The roster is TWO rows per person and is already down to one row at the
    smallest heights before this row exists — so the rule cannot be «the roster
    always has N rows», it has to be «this row never makes that worse».

    It takes its line only where at least one whole participant still fits
    after it. Below that it draws nothing: half a participant is worse than no
    figures, and the roster is the one pane that cannot spare a line.
    """
    viewer = _viewer(tmp_path, cfg)
    viewer.model.status = _roster_row()

    # SEARCHED FOR WHEREVER IT LANDS, not read off a fixed row number. The
    # roster's foot moves with the pane, so a sweep that looked only at the row
    # this draws on at height 30 never visited a single height at which the
    # rule bites — and the guard survived being deleted with the suite green.
    #
    # «128 messages» rather than the batch: the batch legitimately appears on
    # the reader's own row at the bottom of the window, so `6/10` would find
    # that one at every height and prove nothing.
    drawn, refused = 0, 0
    for height in range(8, 40):
        win = _Pane(height=height)
        _draw(viewer, win)
        if any("128 messages" in row for row in win.rows.values()):
            drawn += 1
            assert viewer.roster.rows >= 2, \
                f"took the row at height {height}, leaving {viewer.roster.rows}"
        else:
            refused += 1
    assert drawn, "never drawn at all, so the rule is untested"
    assert refused, "never refused either, so the rule is untested"


def test_the_painter_measures_columns_and_not_characters(tmp_path, cfg):
    """Both rows go through one painter, so the arithmetic is proved once.

    A row holds a block bar, a `⏸`, a `→` and whatever a user's command
    printed. Cutting CHARACTERS to fit COLUMNS is only ever right for ASCII —
    one kanji is two columns and one slice position — and a write past the last
    cell of a pane is what ends the viewer rather than the frame.
    """
    viewer = _viewer(tmp_path, cfg)
    wide = "機能追加ブランチ作業中です"
    for width in (110, 40, 30, 24):
        win = _Pane(width=width)
        viewer._paint_bar(win, 9, width, [wide, wide])
        assert win.rows.get(9), f"nothing drawn at {width}"
        assert tui._w(win.rows[9]) <= width - 1, f"over-ran at {width}"


# --- the single-pane views spend no second row on it ------------------------

def test_the_roster_only_view_puts_the_session_figures_on_its_one_row(
        tmp_path, cfg):
    """That pane's bottom row IS the roster panel's bottom row.

    A second row stacked above it for the same figures would cost a
    participant to say what this one had room for.
    """
    viewer = _viewer(tmp_path, cfg, view="roster")
    viewer.model.status = _roster_row()
    win = _Pane()
    _draw(viewer, win)

    assert "6/10" in win.rows[29] and "128 messages" in win.rows[29]
    assert "$3.10" not in win.rows[29], \
        "the reader's own spend does not belong on a row that speaks for all"
    assert "128 messages" not in " ".join(
        row for y, row in win.rows.items() if y != 29), "and only on that row"


def test_the_roster_only_view_spends_no_extra_row_on_it(tmp_path, cfg):
    viewer = _viewer(tmp_path, cfg, view="roster")
    viewer.model.status = _roster_row()
    win = _Pane()
    _draw(viewer, win)
    with_figures = viewer.roster.rows

    config.save_watch_roster(enabled=False)
    win = _Pane()
    _draw(viewer, win)
    # The bottom row stays — it was never an extra one — and only the rule
    # above it, which is the roster's and not the reader's, is given back.
    assert viewer.roster.rows == with_figures + 1, "the row was never an extra one"
    assert "$3.10" in win.rows[29], "and it goes back to being the reader's"


def test_the_chat_only_view_is_untouched(tmp_path, cfg):
    """It has no roster, so it has no roster row.

    Tested on the message count and not on the batch: the batch legitimately
    appears in this view, on the reader's own row, so `6/10` proves nothing.
    """
    viewer = _viewer(tmp_path, cfg, view="chat")
    viewer.model.status = _roster_row()
    win = _Pane()
    _draw(viewer, win)

    assert "128 messages" not in " ".join(win.rows.values())
    assert "$3.10" in win.rows[29]


# --- the batch is on the roster row and the host's status line; not here too --

def test_the_conversation_row_does_not_carry_the_batch_by_default(cfg):
    """The roster row above it carries the batch, and so does the host agent's
    status line: on this row it was the third copy of one figure, on a screen
    that had two already. It stays a segment a reader can ask for."""
    assert "batch" not in config.DEFAULT_WATCH_STATUS_SEGMENTS
    assert "batch" not in config.watch_status_settings()["segments"]
    assert "batch" not in sb.DEFAULT_SEGMENTS
    assert "batch" in config.WATCH_STATUS_SEGMENTS, "still a segment one can add"


def test_the_batch_can_be_put_back_on_the_conversation_row(cfg):
    config.setting("watch_status_segments").write(["batch", "keys"])
    assert config.watch_status_settings()["segments"] == ("batch", "keys")


def test_the_readme_quotes_the_real_default(cfg):
    """The settings table is what `collab config` prints, in prose."""
    import re
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    row = re.search(r"^\| `watch_status_segments` \|.*\|\s*`([^`]+)`\s*\|$",
                    readme, re.M)
    assert row, "the README has lost its watch_status_segments row"
    assert row.group(1) == ",".join(config.DEFAULT_WATCH_STATUS_SEGMENTS)


def test_the_conversation_row_on_a_real_draw_carries_your_spend_and_not_the_batch(
        tmp_path, cfg):
    viewer, win = _viewer(tmp_path, cfg), _Pane()
    _draw(viewer, win)
    assert "$3.10" in win.rows[29]
    assert "batch" not in win.rows[29]


# --- zero is a count; nothing is not --------------------------------------------

def test_a_hub_that_counted_zero_says_so_and_a_hub_that_never_answered_says_nothing():
    """A fresh session has nothing said in it, and «0 messages» is that fact —
    the hub counted the log and found it empty. What the row must not do is
    print a zero it did not get: a figure that failed to parse, a snapshot with
    no count on it, a daemon that predates the field. Those are absences, and an
    absent segment is the honest drawing of an absence."""
    assert sb.messages_segment({"total": 0, "fetched_at": time.time()}) \
        == "0 messages"
    assert sb.messages_segment({"fetched_at": time.time()}) == ""
    assert sb.messages_segment({"total": None, "fetched_at": time.time()}) == ""
