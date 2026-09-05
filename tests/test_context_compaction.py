"""Two things to do about a context window filling up, from outside the turn.

An agent that can see its own window filling up can do nothing about it: both
answers are slash commands typed at the tool's own prompt, and a model inside a
turn cannot type at its own prompt. So the agent asks, and something outside the
turn types it.

They are two acts and not one with an argument. `compact` summarises the session
and keeps working in it — lossy, but the work goes on. `new` keeps nothing at
all, and the agent comes back not knowing what it was doing. So each has a
switch of its own, each ships off, each has a percent of its own for the daemon
to act on, and a fresh session has a guard that a compaction does not need.

The something outside the turn is the wake's tmux pane, which is why the first
half of these tests is mostly about REFUSING. The line goes into a terminal
somebody is working in, and every way of getting it wrong is a line of text
submitted as a turn in somebody's session, or a shell command run in a pane the
agent left an hour ago:

* a wake armed against anything but a pane — a Codex thread, a headless recipe —
  has no prompt to type at, and each gets a refusal that says which it is;
* a pane that has been recycled, has had its agent exit, or is in copy mode is
  refused by the wake's own checks and not by a second copy of them;
* a program collab does not know is refused by name, because `/compact` means
  nothing to Gemini and `/clear` means something else entirely to Codex.

The second half is about not doing either twice, and not doing the wrong one.
Neither act is undoable, so both ship off; a session over a line is acted on
once rather than on every heartbeat for as long as it stays there; the clocks
are kept per act; and with both percents set the lower fires first while the
far one wins at the far end, unless the guard on a fresh session says otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json

import pytest

from collab import activity, cli, compaction, config as cfg, wake
from collab.client import daemon as d
from collab.protocol import KIND_TASK, Envelope, now_iso


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    """A throwaway global config, never the machine's own — as test_wake does."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "global-config.json"))
    cfg._CACHE.clear()
    yield
    cfg._CACHE.clear()


class _Answer:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


def _tmux_answering(current_command, sent=None, *, pane_exists=True, pid="900",
                    in_mode="0"):
    """A fake tmux: says what is in the pane, records what was typed.

    The same shape as `test_wake`'s, deliberately — this feature types through
    the same function, and a fake that behaved differently would be testing a
    tmux that does not exist.
    """
    def runner(argv, **_kwargs):
        if sent is not None:
            sent.append(argv)
        if "display-message" in argv:
            if not pane_exists:
                return _Answer(0, "")
            return _Answer(0, f"{pid} {in_mode} {current_command}".strip())
        return _Answer(0)
    return runner


def _armed(root, program="claude", target="%3", pid="900"):
    """A wake armed with the tmux recipe, as `collab wake set --agent tmux` writes it."""
    recipe = wake.recipe("tmux")
    wake.write_config(root, wake.WakeConfig(
        command=recipe.command(target=target, pid=pid, running=program,
                               collab="/usr/bin/collab")))
    return root


def _switched(act, on):
    """Turn one of the two acts off, or back on, the way a user does.

    Both ship ON, because on demand is the regular mode. What ships off is the
    unprompted half, and it is off because nobody has set a percent — so these
    tests reach for this only to prove the switch still shuts the act down.
    """
    cfg.setting(act).write(on)


def _at_a_boundary(daemon):
    """The agent has just said it is about to start something."""
    activity.write_local(daemon.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))
    daemon._watch_for_a_boundary()


def _typed(sent):
    """The line that reached the pane, or None."""
    for argv in sent:
        if "send-keys" in argv:
            return argv[argv.index("--") + 1]
    return None


# --- which wake can carry this at all ----------------------------------------

def test_the_tmux_recipe_is_recognised_however_collab_was_spelled(tmp_path):
    """The armed command holds an absolute path to whatever `collab` was, so
    the recipe is recognised by its shape and not by a remembered string."""
    _armed(tmp_path, target="%7", pid="41")
    pane, why = compaction.armed_pane(wake.read_config(tmp_path).command)
    assert why == ""
    assert (pane.target, pane.pid, pane.command) == ("%7", "41", "claude")


def test_a_codex_thread_is_refused_and_says_it_is_a_thread(tmp_path):
    """`codex queue` puts a message in a thread. A `/compact` queued that way
    arrives as something the user said, not as a command."""
    recipe = wake.recipe("codex")
    wake.write_config(tmp_path, wake.WakeConfig(
        command=recipe.command(target="th_1", collab="/usr/bin/collab")))
    pane, why = compaction.armed_pane(wake.read_config(tmp_path).command)
    assert pane is None and "thread" in why


@pytest.mark.parametrize("agent", ["claude", "gemini", "codex-exec", "aider"])
def test_a_headless_recipe_is_refused_because_a_fresh_run_holds_nothing(agent, tmp_path):
    """These start a new process per turn. There is no context to compact, and
    the refusal has to say so rather than merely failing to find a pane."""
    wake.write_config(tmp_path, wake.WakeConfig(
        command=wake.recipe(agent).command(cwd=str(tmp_path))))
    pane, why = compaction.armed_pane(wake.read_config(tmp_path).command)
    assert pane is None and "fresh run" in why


def test_no_wake_at_all_says_how_to_arm_one(tmp_path):
    pane, why = compaction.armed_pane([])
    assert pane is None and "collab wake set --agent tmux" in why


# --- what gets typed ----------------------------------------------------------

@pytest.mark.parametrize("program,action,expected", [
    ("claude", "compact", "/compact"),
    ("claude", "clear", "/clear"),
    ("codex", "compact", "/compact"),
    # Codex's `/clear` empties the TERMINAL and keeps the conversation; `/new`
    # is the one that starts again. The obvious spelling is the wrong one.
    ("codex", "clear", "/new"),
    ("gemini", "compact", "/compress"),
    ("gemini", "clear", "/clear"),
])
def test_each_agent_gets_its_own_spelling(program, action, expected, tmp_path):
    sent = []
    _armed(tmp_path, program=program)
    code, detail = compaction.apply(tmp_path, action,
                                    runner=_tmux_answering(program, sent))
    assert code == 0, detail
    assert _typed(sent) == expected
    assert expected in detail and "%3" in detail, \
        "it has to say what it typed and where; «done» is not checkable"


def test_a_program_collab_does_not_know_is_refused_by_name(tmp_path):
    """A guess here does not fail: it submits a line of prose as a turn."""
    sent = []
    _armed(tmp_path, program="vim")
    code, why = compaction.apply(tmp_path, "compact",
                                 runner=_tmux_answering("vim", sent))
    assert code != 0
    assert "vim" in why and "claude" in why, "say what it knows, not just no"
    assert _typed(sent) is None


def test_an_action_nobody_offers_is_refused_before_the_pane_is_touched(tmp_path):
    _armed(tmp_path)
    code, why = compaction.apply(tmp_path, "restart")
    assert code != 0 and "restart" in why


# --- the wake's own refusals, reached through this door ------------------------

def test_a_recycled_pane_is_refused_by_the_wakes_own_check(tmp_path):
    """Not a second copy of the check: the copy is what falls behind the day a
    new way of losing a pane is found."""
    sent = []
    _armed(tmp_path, pid="900")
    code, why = compaction.apply(tmp_path, "compact",
                                 runner=_tmux_answering("claude", sent, pid="4242"))
    assert code != 0 and "different terminal" in why
    assert _typed(sent) is None


def test_a_pane_in_copy_mode_is_not_typed_into(tmp_path):
    """tmux's copy mode eats the keys as copy-mode commands and says nothing."""
    sent = []
    _armed(tmp_path)
    code, why = compaction.apply(tmp_path, "compact",
                                 runner=_tmux_answering("claude", sent, in_mode="1"))
    assert code != 0 and "copy mode" in why
    assert _typed(sent) is None


def test_a_pane_whose_agent_has_exited_is_not_typed_into(tmp_path):
    """`/compact` typed at a shell is a command that does not exist — at best."""
    sent = []
    _armed(tmp_path, program="claude")
    code, why = compaction.apply(tmp_path, "compact",
                                 runner=_tmux_answering("bash", sent))
    assert code != 0 and "not the claude" in why
    assert _typed(sent) is None


# --- the two commands -----------------------------------------------------------

def _run(profile, monkeypatch, command, **kwargs):
    """`collab compact` or `collab new`, with both streams captured."""
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    fields = {"session": None, "agent": None, "all": False, "agree": None,
              "decline": None, "reason": None, "status": False, "json": False}
    args = argparse.Namespace(**{**fields, **kwargs})
    run = cli.cmd_compact if command == "compact" else cli.cmd_new
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = run(args)
    return code, out.getvalue()


def _fake_tmux(current_command, sent, *, pid="900", in_mode="0"):
    """`wake._tmux` itself, so a CLI test needs no subprocess of any kind."""
    def fake(args, runner=None):
        sent.append(args)
        if "display-message" in args:
            return 0, f"{pid} {in_mode} {current_command}"
        return 0, ""
    return fake


@pytest.mark.parametrize("command,expected", [("compact", "/compact"),
                                              ("new", "/clear")])
def test_each_command_types_its_own_line_and_says_where(command, expected,
                                                        profile, monkeypatch):
    """«done» is not checkable by a person; «/compact into %5» is."""
    sent = []
    _armed(d.DaemonPaths(profile.dir).root, program="claude", target="%5")
    monkeypatch.setattr(wake, "_tmux", _fake_tmux("claude", sent))

    code, out = _run(profile, monkeypatch, command)

    assert code == 0
    assert _typed(sent) == expected
    assert "%5" in out


def test_the_command_explains_a_wake_that_cannot_carry_this(profile, monkeypatch):
    code, out = _run(profile, monkeypatch, "compact")
    assert code != 0
    assert "wake set --agent tmux" in out


# --- the switches -----------------------------------------------------------------
#
# ON, both, with no threshold. On demand is the regular mode: these are things
# an agent or a person asks for at a moment they chose, and refusing those out
# of the box would only teach everybody to turn a switch on before every use.
# What ships off is the unprompted half, and it is off because no percent has
# been set rather than because a switch forbids it.
#
# The switches are still the one place to shut either act down completely, and
# they govern the command and the daemon alike.

@pytest.mark.parametrize("key", ["compact", "new"])
def test_each_act_is_available_out_of_the_box(key):
    assert cfg.setting(key).read() is True
    assert cfg.setting(key).default is True


@pytest.mark.parametrize("key", ["compact_at", "new_at"])
def test_but_nothing_fires_unprompted_out_of_the_box(key):
    """The caution lives in the percent, which is where somebody looking for
    «what does collab do to my session unasked» will find it."""
    assert cfg.setting(key).read() == 0


def test_the_registry_lists_each_switch_before_its_own_percent_and_moment():
    """The order somebody meets them in, and the order they are read in."""
    names = [s.name for s in cfg.settings()]
    assert names.index("compact") < names.index("compact_at") \
        < names.index("compact_when") < names.index("new") \
        < names.index("new_at") < names.index("new_when") \
        < names.index("new_consensus") < names.index("new_consensus_minutes")


@pytest.mark.parametrize("command,what", [("compact", "compaction"),
                                          ("new", "a fresh session")])
def test_each_command_refuses_once_its_switch_is_off(command, what, profile,
                                                     monkeypatch):
    """One line, and nothing typed at anybody's prompt."""
    _switched(command, False)
    sent = []
    _armed(d.DaemonPaths(profile.dir).root, program="claude")
    monkeypatch.setattr(wake, "_tmux", _fake_tmux("claude", sent))

    code, out = _run(profile, monkeypatch, command)

    assert code == 1
    assert f"{what} is off" in out
    assert f"config {command} on" in out, "an agent told only «no» improvises"
    assert sent == []


def test_turning_one_off_leaves_the_other_alone(profile, monkeypatch):
    """The whole reason there are two switches."""
    _switched("new", False)
    sent = []
    _armed(d.DaemonPaths(profile.dir).root, program="claude")
    monkeypatch.setattr(wake, "_tmux", _fake_tmux("claude", sent))

    assert _run(profile, monkeypatch, "compact")[0] == 0
    code, out = _run(profile, monkeypatch, "new")

    assert code == 1 and "a fresh session is off" in out


def test_the_refusal_comes_before_the_session_is_looked_for(tmp_path, monkeypatch):
    """A refusal that first reported no active session would be answering a
    question nobody asked."""
    _switched("compact", False)
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: None))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_compact(argparse.Namespace(session=None, agent=None))

    assert code == 1
    assert "compaction is off" in out.getvalue()
    assert "no active session" not in out.getvalue()


# --- the percents -----------------------------------------------------------------

@pytest.mark.parametrize("key", ["compact_at", "new_at"])
@pytest.mark.parametrize("typed", ["20", "99", "-5"])
def test_a_threshold_outside_the_range_is_refused_at_the_command(key, typed):
    """Below the floor is a session that spends its life being acted on; above
    the ceiling there may be no room left to write a summary in."""
    with pytest.raises(ValueError) as raised:
        cfg.setting(key).parse(typed)
    assert "50" in str(raised.value) and "95" in str(raised.value)


@pytest.mark.parametrize("key", ["compact_at", "new_at"])
def test_zero_is_the_one_value_below_the_floor_that_means_something(key):
    assert cfg.setting(key).parse("0") == 0


@pytest.mark.parametrize("key", ["compact_at", "new_at"])
@pytest.mark.parametrize("raw,expected", [
    (9, 50), (200, 95), ("80", 80), ("nonsense", 0), (True, 0), (None, 0),
])
def test_nothing_in_a_hand_edited_file_can_start_acting_at_nine_percent(
        key, raw, expected, tmp_path):
    """Clamped on the read side and refused at the command, the split
    `remind_every` argues for. `True` is 1 in Python, which is why a bool is
    thrown out before `int` is tried."""
    path = tmp_path / "global-config.json"
    path.write_text(json.dumps({key: raw}))
    cfg._CACHE.clear()
    assert cfg.setting(key).read() == expected


# --- the retired key ---------------------------------------------------------------

def test_the_old_key_is_ignored_rather_than_migrated(tmp_path):
    """A program that edits a config it was only asked to display is a worse
    surprise than the one it would be fixing."""
    path = tmp_path / "global-config.json"
    path.write_text(json.dumps({cfg.RETIRED_COMPACT_AT: 80}))
    cfg._CACHE.clear()

    assert cfg.compact_at() == 0
    assert json.loads(path.read_text()) == {cfg.RETIRED_COMPACT_AT: 80}


def test_listing_the_settings_says_the_old_key_is_dead(tmp_path):
    """A renamed setting is the quiet kind of breakage: the file still parses,
    the listing still prints, and what somebody configured months ago has
    simply stopped happening."""
    (tmp_path / "global-config.json").write_text(
        json.dumps({cfg.RETIRED_COMPACT_AT: 80}))
    cfg._CACHE.clear()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_config(argparse.Namespace(key=None, value=None, json=False,
                                          unset=False))

    said = out.getvalue()
    assert "context_compact_at is now compact_at" in said
    assert "ignored" in said


def test_a_file_without_the_old_key_says_nothing_about_it(tmp_path):
    """Silence for everybody who never had it, which is nearly everybody."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_config(argparse.Namespace(key=None, value=None, json=False,
                                          unset=False))

    assert "context_compact_at" not in out.getvalue()


# --- the daemon's automatic half ----------------------------------------------

class _Daemon:
    """Just enough of a daemon to drive the heartbeat's half and watch the clock."""

    _maybe_type_at_the_agent = d.Daemon._maybe_type_at_the_agent
    _which_act_is_due = d.Daemon._which_act_is_due
    _moment_allows = d.Daemon._moment_allows
    _watch_for_a_boundary = d.Daemon._watch_for_a_boundary
    _note_task_boundary = d.Daemon._note_task_boundary
    _compact_before_the_turn = d.Daemon._compact_before_the_turn
    _wake_once = d.Daemon._wake_once

    def __init__(self, profile, share):
        self.profile = profile
        self.paths = d.DaemonPaths(profile.dir)
        self._share = share
        self._acted_at = {"compact": 0.0, "new": 0.0}
        self._tried_at = {"compact": 0.0, "new": 0.0}
        self._under_since = {"compact": 0.0, "new": 0.0}
        self._boundary_at = 0.0
        self._was_working = False
        #: The turn this daemon started and has not seen end, exactly as the
        #: real one holds it: `_maybe_wake` launches a background task and the
        #: heartbeat carries on.
        self._waking = None
        # A real one: `_wake_once` asks it for the armed command and for
        # somewhere to write the prompt, and a fake that answered differently
        # would be testing a wake that does not exist.
        self.waker = wake.Waker(self.paths.root, profile.session_id)
        self._wake_note = ""
        self.applied: list[str] = []

    def _log_wake(self, *a, **kw):
        """The diagnostic record. Not what these tests are about."""


@pytest.fixture
def acting(profile, tmp_path, monkeypatch):
    """A daemon whose agent reports a share, with the pane always willing.

    Both switches are on by default. What this turns off is the MOMENT: these
    tests are about the thresholds, and `always` is the value that says «the
    threshold and nothing else». The moments have their own tests below.
    """
    cfg.setting("compact_when").write("always")
    cfg.setting("new_when").write("always")
    _armed(d.DaemonPaths(profile.dir).root)
    share = {"pct": 10.0}
    monkeypatch.setattr(d, "read_stats", lambda p: {"context_pct": share["pct"]})
    daemon = _Daemon(profile, share)

    def apply(root, action, **_kw):
        daemon.applied.append(action)
        return 0, "typed into %3"

    monkeypatch.setattr(compaction, "apply", apply)
    return daemon, share


def _beat(daemon, now):
    """One heartbeat, at a time of the test's choosing."""
    import collab.client.daemon as mod
    real = mod.time.time
    mod.time.time = lambda: now                       # noqa: B010
    try:
        asyncio.run(daemon._maybe_type_at_the_agent())
    finally:
        mod.time.time = real


def test_nothing_happens_at_all_with_both_thresholds_off(acting):
    daemon, share = acting
    share["pct"] = 99.0
    _beat(daemon, 1000.0)
    assert daemon.applied == []


@pytest.mark.parametrize("key,typed", [("compact_at", "compact"),
                                       ("new_at", "clear")])
def test_each_threshold_fires_its_own_act(key, typed, acting):
    daemon, share = acting
    cfg.setting(key).write(80)
    share["pct"] = 84.0

    _beat(daemon, 1000.0)

    assert daemon.applied == [typed]


@pytest.mark.parametrize("key,typed", [("compact_at", "compact"),
                                       ("new_at", "clear")])
def test_it_waits_for_the_share_to_fall_before_acting_again(key, typed, acting):
    """Either condition alone fires forever: a figure that stops being reported
    keeps its last value, and an act that freed very little leaves the share
    hovering on the line."""
    daemon, share = acting
    cfg.setting(key).write(80)
    share["pct"] = 90.0

    _beat(daemon, 1000.0)
    for tick in range(1, 400):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == [typed], "over the line is not a reason to act again"


@pytest.mark.parametrize("key,typed", [("compact_at", "compact"),
                                       ("new_at", "clear")])
def test_a_share_that_dropped_and_climbed_back_acts_again(key, typed, acting):
    daemon, share = acting
    cfg.setting(key).write(80)
    share["pct"] = 90.0
    _beat(daemon, 1000.0)

    share["pct"] = 40.0
    _beat(daemon, 1100.0)
    share["pct"] = 90.0
    _beat(daemon, 1000.0 + cfg.COMPACT_GAP + 10)

    assert daemon.applied == [typed, typed]


def test_an_agent_that_reports_no_context_share_is_left_alone(acting,
                                                              monkeypatch):
    """Every automatic form needs the tool to report the share; without it
    there is nothing to compare a threshold against."""
    daemon, _share = acting
    monkeypatch.setattr(d, "read_stats", lambda p: {})
    cfg.setting("compact_at").write(80)

    _beat(daemon, 1000.0)

    assert daemon.applied == []


@pytest.mark.parametrize("act,key", [("compact", "compact_at"),
                                     ("new", "new_at")])
def test_a_threshold_without_its_switch_does_nothing(act, key, acting):
    """A percent set by somebody who then shut the act off is a number and not
    a permission."""
    daemon, share = acting
    _switched(act, False)
    cfg.setting(key).write(80)
    share["pct"] = 99.0

    for tick in range(10):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == []


def test_the_two_clocks_are_kept_apart(acting):
    """One act having just run says nothing about whether the other should."""
    daemon, share = acting
    cfg.setting("compact_at").write(70)
    cfg.setting("new_at").write(90)

    share["pct"] = 75.0
    _beat(daemon, 1000.0)
    assert daemon.applied == ["compact"]

    # Straight past the higher line, well inside the ten minutes the compaction
    # would have to wait. The fresh session has a clock of its own and it has
    # not started.
    share["pct"] = 95.0
    _beat(daemon, 1030.0)

    assert daemon.applied == ["compact", "clear"]


# --- both thresholds at once ------------------------------------------------------

def test_the_lower_threshold_comes_first(acting):
    """No arithmetic needed: at 75 with compact at 70 and new at 90, one has
    been reached and the other has not."""
    daemon, share = acting
    cfg.setting("compact_at").write(70)
    cfg.setting("new_at").write(90)
    share["pct"] = 75.0

    _beat(daemon, 1000.0)

    assert daemon.applied == ["compact"]


def test_at_a_share_that_reached_both_the_fresh_session_wins(acting):
    """Somebody who set both meant compact for a window filling up and a fresh
    session for one that is nearly gone."""
    daemon, share = acting
    cfg.setting("compact_at").write(70)
    cfg.setting("new_at").write(90)
    share["pct"] = 95.0

    _beat(daemon, 1000.0)

    assert daemon.applied == ["clear"]


def test_but_a_moment_that_says_no_compacts_rather_than_doing_nothing(acting):
    """Refusing to act at all would leave an agent with both keys set, a full
    window and no help, on the strength of a guard whose whole purpose is to
    protect the work in hand — which compacting protects by definition."""
    daemon, share = acting
    cfg.setting("compact_at").write(70)
    cfg.setting("new_at").write(90)
    cfg.setting("new_when").write("idle")
    activity.write_local(daemon.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))
    share["pct"] = 95.0

    _beat(daemon, 1000.0)

    assert daemon.applied == ["compact"]


# --- the moment, per act ----------------------------------------------------------
#
# A percent says how full; a moment says when it is worth acting on. A summary
# taken mid-turn throws away the reasoning the agent is holding right now to
# finish what it is doing; a summary taken at a task boundary loses nothing
# still needed, because the work that context was for is done.

def test_task_is_the_default_for_compaction():
    assert cfg.compact_when() == "task"
    assert cfg.setting("compact_when").default == "task"


def test_idle_is_the_default_for_a_fresh_session():
    """Stricter than compaction's, because a fresh session keeps nothing."""
    assert cfg.new_when() == "idle"
    assert cfg.setting("new_when").default == "idle"


def test_a_full_window_alone_is_not_a_moment(acting):
    """The default. Crossing the line mid-task is not the time."""
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    for tick in range(10):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == []


def test_the_same_agent_is_compacted_when_it_starts_something(acting):
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0
    _beat(daemon, 1000.0)
    assert daemon.applied == []

    _at_a_boundary(daemon)
    _beat(daemon, 1030.0)

    assert daemon.applied == ["compact"]


def test_a_boundary_is_one_moment_and_one_act(acting):
    """A boundary is an edge, not a state. Left standing it would let the next
    beat act again on a moment that has passed."""
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0
    _at_a_boundary(daemon)

    for tick in range(200):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == ["compact"]


def test_staying_at_work_is_not_a_second_boundary(acting):
    """The edge is the change. An agent that says `working` on every heartbeat
    has crossed one boundary, not a hundred."""
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0
    activity.write_local(daemon.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))

    for _ in range(5):
        daemon._watch_for_a_boundary()
    _beat(daemon, 1000.0)
    daemon._watch_for_a_boundary()
    _beat(daemon, 1030.0)

    assert daemon.applied == ["compact"]


def test_a_task_claimed_for_this_agent_is_a_boundary_too(acting):
    """The board is watched as well as the activity file: somebody else
    claiming a task FOR us is the case the file cannot see."""
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    daemon._note_task_boundary(Envelope(
        seq=1, ts=now_iso(), kind=KIND_TASK, sender="alice",
        body={"action": "claim", "id": "T_1", "title": "the parser",
              "state": "TASK_STATE_WORKING", "owner": daemon.profile.name}))
    _beat(daemon, 1000.0)

    assert daemon.applied == ["compact"]


def test_somebody_elses_task_is_not_our_boundary(acting):
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    daemon._note_task_boundary(Envelope(
        seq=1, ts=now_iso(), kind=KIND_TASK, sender="alice",
        body={"action": "claim", "id": "T_1", "title": "the parser",
              "state": "TASK_STATE_WORKING", "owner": "carol"}))
    _beat(daemon, 1000.0)

    assert daemon.applied == []


@pytest.mark.parametrize("state", ["TASK_STATE_OPEN", "done", "cancelled"])
def test_a_task_that_is_not_starting_is_not_a_boundary(state, acting):
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    daemon._note_task_boundary(Envelope(
        seq=1, ts=now_iso(), kind=KIND_TASK, sender="alice",
        body={"id": "T_1", "state": state, "owner": daemon.profile.name}))
    _beat(daemon, 1000.0)

    assert daemon.applied == []


def test_always_acts_whenever_the_line_is_crossed(acting):
    """What a percent on its own used to mean, kept for somebody who has
    decided a full window is the worse problem."""
    daemon, share = acting
    cfg.setting("compact_when").write("always")
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    _beat(daemon, 1000.0)

    assert daemon.applied == ["compact"]


def test_a_fresh_session_can_wait_for_a_boundary_too(acting):
    """The symmetrical shape: `task` on `new` is a fresh session right before a
    new task starts."""
    daemon, share = acting
    cfg.setting("new_when").write("task")
    cfg.setting("new_at").write(80)
    share["pct"] = 99.0
    _beat(daemon, 1000.0)
    assert daemon.applied == []

    _at_a_boundary(daemon)
    _beat(daemon, 1030.0)

    assert daemon.applied == ["clear"]


def test_a_working_agent_is_not_given_a_fresh_session_under_idle(acting):
    daemon, share = acting
    cfg.setting("new_when").write("idle")
    cfg.setting("new_at").write(80)
    activity.write_local(daemon.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))
    share["pct"] = 95.0

    for tick in range(10):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == []


def test_the_same_agent_gets_one_once_it_says_it_has_stopped(acting):
    daemon, share = acting
    cfg.setting("new_when").write("idle")
    cfg.setting("new_at").write(80)
    activity.write_local(daemon.profile,
                         activity.sanitise({"state": "working",
                                            "what": "the parser"}))
    share["pct"] = 95.0
    _beat(daemon, 1000.0)
    assert daemon.applied == []

    activity.write_local(daemon.profile, activity.sanitise({"state": "idle"}))
    _beat(daemon, 1030.0)

    assert daemon.applied == ["clear"]


def test_a_working_that_nobody_renewed_does_not_hold_it_off_for_ever(acting):
    """Read the way the roster reads it. An agent killed mid-task keeps the
    word `working`, and a guard that believed it would never fire again."""
    daemon, share = acting
    cfg.setting("new_when").write("idle")
    cfg.setting("new_at").write(80)
    stale = activity.sanitise({"state": "working", "what": "the parser"})
    # Stamped against the beat below rather than the wall clock, because that
    # is the clock the staleness is measured on.
    stale["updated_at"] = 1000.0 - activity.STALE_AFTER - 60
    activity.write_local(daemon.profile, stale)
    share["pct"] = 95.0

    _beat(daemon, 1000.0)

    assert daemon.applied == ["clear"]


@pytest.mark.parametrize("key,typed", [("compact_when", "sometimes"),
                                       ("new_when", "when-idle"),
                                       ("new_consensus", "most")])
def test_a_word_nobody_offers_is_refused_at_the_command(key, typed):
    """Somebody who typed `when-idle` and was answered «ok» would go on
    believing the guard was on."""
    with pytest.raises(ValueError):
        cfg.setting(key).parse(typed)


@pytest.mark.parametrize("key,fallback", [("compact_when", "task"),
                                          ("new_when", "idle"),
                                          ("new_consensus", "all")])
def test_a_word_nobody_offers_in_a_hand_edited_file_reads_as_the_default(
        key, fallback, tmp_path):
    """The read side defaults where the command refuses: a typo in a file
    should cost the setting its value, not the session."""
    (tmp_path / "global-config.json").write_text(json.dumps({key: "whenever"}))
    cfg._CACHE.clear()
    assert cfg.setting(key).read() == fallback


# --- compacting before a woken turn -----------------------------------------------
#
# A wake starts a turn in an agent that was not looking. If that agent's window
# is nearly full, the turn it is about to take is the one least able to afford
# it — so the summary goes first and the turn begins on it. Afterwards would be
# wrong twice: the turn would run in the full window, and the summary would then
# discard what it had just produced.

def _before_the_turn(daemon, now=1000.0):
    import collab.client.daemon as mod
    real = mod.time.time
    mod.time.time = lambda: now                       # noqa: B010
    try:
        asyncio.run(daemon._compact_before_the_turn())
    finally:
        mod.time.time = real


def test_a_full_window_is_compacted_before_the_turn_is_delivered(acting):
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 95.0

    _before_the_turn(daemon)

    assert daemon.applied == ["compact"]


def test_a_window_with_room_in_it_is_left_alone(acting):
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 40.0

    _before_the_turn(daemon)

    assert daemon.applied == []


def test_no_threshold_means_no_compaction_before_a_turn(acting):
    """The wake path is the automatic half by another door, so it answers to
    the same percent."""
    daemon, share = acting
    share["pct"] = 99.0

    _before_the_turn(daemon)

    assert daemon.applied == []


def test_the_switch_shuts_the_wake_path_down_too(acting):
    daemon, share = acting
    _switched("compact", False)
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0

    _before_the_turn(daemon)

    assert daemon.applied == []


def test_a_refused_compaction_never_blocks_the_turn(acting, monkeypatch):
    """A pane in copy mode is not a reason to withhold somebody's messages.
    The wake's own checks meet the same pane a moment later and say so."""
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 99.0
    monkeypatch.setattr(compaction, "apply",
                        lambda root, action, **_kw: (1, "pane %3 is in copy mode"))

    _before_the_turn(daemon)                    # must not raise

    assert daemon._acted_at["compact"] == 0.0, "and it did not pretend it had"


# --- the pre-wake compaction, through the gate that decides it --------------------
#
# Everything above drives `_compact_before_the_turn` directly. These go through
# `_wake_once`, which is the code that decides whether it is called at all —
# and which is where the gate was wrong.

class _InFlight:
    """A turn that has been started and has not finished."""

    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def _wake_the_agent(daemon, monkeypatch, now=1000.0):
    """`_wake_once` as far as the compaction, with the launch stubbed out.

    The subprocess half is somebody else's test. What is exercised here is the
    order: the summary goes in before the line does.
    """
    started = []

    async def never_launched(*a, **kw):
        started.append(True)
        raise OSError("not launching a real turn in a test")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never_launched)
    import collab.client.daemon as mod
    real = mod.time.time
    mod.time.time = lambda: now                       # noqa: B010
    try:
        asyncio.run(daemon._wake_once(None, "a reminder"))
    finally:
        mod.time.time = real
    return started


@pytest.mark.parametrize("when", ["task", "always"])
def test_a_woken_turn_is_given_a_summary_first_under_either_setting(
        when, acting, monkeypatch):
    """A woken turn about to be typed is one of the moments `task` names, and
    `always` is every moment including that one. Gated on `task`, the LESS
    restrictive setting lost the one guarantee the design is built around: the
    turn ran on the full window and the compaction caught up on a later
    heartbeat, after the turn it was for."""
    daemon, share = acting
    cfg.setting("compact_when").write(when)
    cfg.setting("compact_at").write(80)
    share["pct"] = 95.0

    _wake_the_agent(daemon, monkeypatch)

    assert daemon.applied == ["compact"]


def test_a_woken_turn_with_room_in_the_window_is_not_compacted(acting,
                                                               monkeypatch):
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 40.0

    _wake_the_agent(daemon, monkeypatch)

    assert daemon.applied == []


def test_the_turn_is_still_delivered_when_the_compaction_is_refused(
        acting, monkeypatch):
    """A pane in copy mode is not a reason to withhold somebody's messages."""
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 95.0
    monkeypatch.setattr(compaction, "apply",
                        lambda root, action, **_kw: (1, "pane %3 is in copy mode"))

    started = _wake_the_agent(daemon, monkeypatch)

    assert started, "the delivery was attempted all the same"


# --- and nothing is typed while a turn of ours is in flight -----------------------

@pytest.mark.parametrize("key,typed", [("compact_at", "compact"),
                                       ("new_at", "clear")])
def test_neither_act_is_typed_into_a_pane_taking_our_own_turn(key, typed,
                                                              acting):
    """`_maybe_wake` starts the turn as a background task and the heartbeat
    carries on. Without this the next beat types into the pane where that turn
    is still being taken, and the agent loses the work it was woken to do — to
    this program, rather than to anything it did."""
    daemon, share = acting
    cfg.setting(key).write(80)
    share["pct"] = 95.0
    daemon._waking = _InFlight(done=False)

    for tick in range(10):
        _beat(daemon, 1000.0 + tick * 3)

    assert daemon.applied == []


@pytest.mark.parametrize("key,typed", [("compact_at", "compact"),
                                       ("new_at", "clear")])
def test_the_act_happens_on_the_next_beat_after_the_turn_ends(key, typed,
                                                              acting):
    """Postponed, not cancelled: a threshold still crossed when the turn ends
    is still crossed on the beat after it."""
    daemon, share = acting
    cfg.setting(key).write(80)
    share["pct"] = 95.0
    daemon._waking = _InFlight(done=False)
    _beat(daemon, 1000.0)
    assert daemon.applied == []

    daemon._waking = _InFlight(done=True)
    _beat(daemon, 1030.0)

    assert daemon.applied == [typed]


def test_a_turn_in_flight_does_not_spend_the_boundary(acting):
    """A turn arriving between the boundary and the act must not cost the act
    its moment. The boundary is a moment the agent gave us, not a token this
    program may drop while it is busy."""
    daemon, share = acting
    cfg.setting("compact_when").write("task")
    cfg.setting("compact_at").write(80)
    share["pct"] = 95.0
    _at_a_boundary(daemon)
    daemon._waking = _InFlight(done=False)

    for tick in range(5):
        _beat(daemon, 1000.0 + tick * 3)
    assert daemon.applied == []
    assert daemon._boundary_at, "the moment is still ours to act on"

    daemon._waking = _InFlight(done=True)
    _beat(daemon, 1100.0)

    assert daemon.applied == ["compact"]


def test_a_finished_turn_is_no_obstacle(acting):
    daemon, share = acting
    cfg.setting("compact_at").write(80)
    share["pct"] = 95.0
    daemon._waking = _InFlight(done=True)

    _beat(daemon, 1000.0)

    assert daemon.applied == ["compact"]
