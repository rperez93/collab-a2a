"""Compacting an agent's context from outside its own turn.

An agent that can see its context window filling up can do nothing about it: the
command that compacts a session is a slash command typed at the tool's own
prompt, and a model inside a turn cannot type at its own prompt. So the agent
asks, and something outside the turn types it.

That something is the wake's tmux pane, which is why these tests are mostly
about REFUSING. The line goes into a terminal somebody is working in, and every
way of getting it wrong is a line of text submitted as a turn in somebody's
session, or a shell command run in a pane the agent left an hour ago:

* a wake armed against anything but a pane — a Codex thread, a headless recipe —
  has no prompt to type at, and each gets a refusal that says which it is;
* a pane that has been recycled, has had its agent exit, or is in copy mode is
  refused by the wake's own checks and not by a second copy of them;
* a program collab does not know is refused by name, because `/compact` means
  nothing to Gemini and `/clear` means something else entirely to Codex.

And the daemon's automatic half is about not doing it twice. Compaction is not
undoable, so the threshold is off unless asked for, and a session over the line
is compacted once rather than on every heartbeat for as long as it stays there.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json

import pytest

from collab import cli, compaction, config as cfg, wake
from collab.client import daemon as d


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


# --- the command --------------------------------------------------------------

def _run(profile, monkeypatch, action, **kwargs):
    """`collab context <action>`, with both streams captured."""
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    args = argparse.Namespace(**{"action": action, "session": None,
                                 "agent": None, **kwargs})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_context(args)
    return code, out.getvalue()


def test_the_command_types_it_and_says_what_it_typed_and_where(profile, monkeypatch):
    sent = []
    _armed(d.DaemonPaths(profile.dir).root, program="claude", target="%5")
    monkeypatch.setattr(wake, "_tmux", _fake_tmux("claude", sent))
    code, out = _run(profile, monkeypatch, "compact")
    assert code == 0, out
    assert "/compact" in out and "%5" in out
    assert _typed(sent) == "/compact"


def test_the_command_explains_a_wake_that_cannot_carry_this(profile, monkeypatch):
    """The refusal is the common case — most agents arm no wake at all — so it
    has to end with the command that would fix it rather than with «no»."""
    code, out = _run(profile, monkeypatch, "compact")
    assert code != 0
    assert "collab wake set --agent tmux" in out


def _fake_tmux(current_command, sent, *, pid="900", in_mode="0"):
    """`wake._tmux` itself, so a CLI test needs no subprocess of any kind."""
    def fake(args, runner=None):
        sent.append(args)
        if "display-message" in args:
            return 0, f"{pid} {in_mode} {current_command}"
        return 0, ""
    return fake


# --- the setting --------------------------------------------------------------

def test_the_threshold_is_off_until_somebody_asks(tmp_path):
    """Compacting is not undoable. A threshold nobody chose, firing mid-task,
    hands somebody a summary of the reasoning they were relying on."""
    assert cfg.context_compact_at() == 0
    assert cfg.setting("context_compact_at").default == 0


@pytest.mark.parametrize("typed", ["20", "99", "-5"])
def test_a_threshold_outside_the_range_is_refused_at_the_command(typed):
    """Below the floor is a session that spends its life being compacted; above
    the ceiling there is no room left to write the summary in."""
    with pytest.raises(ValueError) as bad:
        cfg.setting("context_compact_at").parse(typed)
    assert "50" in str(bad.value) and "95" in str(bad.value)


def test_zero_is_the_one_value_below_the_floor_that_means_something():
    assert cfg.setting("context_compact_at").parse("0") == 0


@pytest.mark.parametrize("raw,expected", [
    ({"context_compact_at": 9}, 50),      # floored, not obeyed
    ({"context_compact_at": 300}, 95),    # capped
    ({"context_compact_at": "soon"}, 0),
    ({"context_compact_at": True}, 0),    # a bool is not a percentage
    ({"context_compact_at": None}, 0),
])
def test_nothing_in_a_hand_edited_file_can_start_compacting_at_nine_percent(
        raw, expected, tmp_path, monkeypatch):
    """Read on the heartbeat, so it is clamped rather than refused here — the
    split `remind_every` makes, for the same reason."""
    path = tmp_path / "hand.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("COLLAB_CONFIG", str(path))
    cfg._CACHE.clear()
    assert cfg.context_compact_at() == expected


# --- the daemon's automatic half ----------------------------------------------

class _Daemon:
    """Just enough of a daemon to drive `_maybe_compact` and watch the clock."""

    _maybe_compact = d.Daemon._maybe_compact

    def __init__(self, profile, share):
        self.profile = profile
        self.paths = d.DaemonPaths(profile.dir)
        self._share = share
        self._context_compacted_at = 0.0
        self._context_tried_at = 0.0
        self._context_under_since = 0.0
        self.applied: list[str] = []


@pytest.fixture
def compacting(profile, tmp_path, monkeypatch):
    """A daemon whose agent reports a share, with the pane always willing."""
    _armed(d.DaemonPaths(profile.dir).root)
    share = {"pct": 10.0}
    monkeypatch.setattr(d, "read_stats", lambda p: {"context_pct": share["pct"]})
    daemon = _Daemon(profile, share)

    def apply(root, action, **_kw):
        daemon.applied.append(action)
        return 0, f"typed /compact into %3"

    monkeypatch.setattr(compaction, "apply", apply)
    return daemon, share


def _beat(daemon, now):
    """One heartbeat, at a time of the test's choosing."""
    import collab.client.daemon as mod
    real = mod.time.time
    mod.time.time = lambda: now                       # noqa: B010
    try:
        asyncio.run(daemon._maybe_compact())
    finally:
        mod.time.time = real


def test_nothing_happens_at_all_with_the_threshold_off(compacting):
    daemon, share = compacting
    share["pct"] = 99.0
    _beat(daemon, 1000.0)
    assert daemon.applied == []


def test_an_agent_over_its_threshold_is_compacted_once(compacting):
    daemon, share = compacting
    cfg.setting("context_compact_at").write(80)
    share["pct"] = 84.0
    _beat(daemon, 1000.0)
    assert daemon.applied == ["compact"]
    # Still over the line — the figure the agent reports lags the compaction
    # by however long its status line takes — and the heartbeat is every three
    # seconds. Without the guard this is a compaction twenty times a minute.
    for tick in range(1, 40):
        _beat(daemon, 1000.0 + tick * 3)
    assert daemon.applied == ["compact"]


def test_it_waits_for_the_share_to_fall_before_compacting_again(compacting):
    """Both conditions, because either alone fires forever: a figure that stops
    being reported keeps its last value, and a compaction that frees very
    little leaves the share hovering on the line."""
    daemon, share = compacting
    cfg.setting("context_compact_at").write(80)
    share["pct"] = 84.0
    _beat(daemon, 1000.0)
    # An hour later, having never once dropped below the line: still nothing.
    # This is what a status line that has stopped reporting looks like.
    _beat(daemon, 1000.0 + 3600)
    assert daemon.applied == ["compact"], "the share never fell"

    # And the other way round: it drops and climbs straight back, which is what
    # a compaction that freed very little looks like. Inside the ten minutes.
    share["pct"] = 20.0
    _beat(daemon, 1000.0 + 3660)
    share["pct"] = 90.0
    _beat(daemon, 1000.0 + 3720)
    assert daemon.applied == ["compact", "compact"], \
        "an hour after the last one, having dropped, it is due"
    share["pct"] = 20.0
    _beat(daemon, 1000.0 + 3760)
    share["pct"] = 90.0
    _beat(daemon, 1000.0 + 3800)
    assert daemon.applied == ["compact", "compact"], \
        "eighty seconds after the last one, it is not"


def test_a_share_that_dropped_and_climbed_back_compacts_again(compacting):
    daemon, share = compacting
    cfg.setting("context_compact_at").write(80)
    share["pct"] = 84.0
    _beat(daemon, 1000.0)
    share["pct"] = 20.0
    _beat(daemon, 1100.0)
    share["pct"] = 88.0
    _beat(daemon, 1000.0 + cfg.CONTEXT_COMPACT_GAP + 1)
    assert daemon.applied == ["compact", "compact"]


def test_an_agent_that_reports_no_context_share_is_left_alone(compacting):
    daemon, _share = compacting
    cfg.setting("context_compact_at").write(80)
    import collab.client.daemon as mod
    mod.read_stats = lambda p: {"cost_usd": 3.1}
    try:
        _beat(daemon, 1000.0)
    finally:
        mod.read_stats = d.read_stats
    assert daemon.applied == []


def test_a_failed_attempt_does_not_probe_the_pane_every_three_seconds(
        profile, monkeypatch):
    """A pane in copy mode with a full context would otherwise mean a
    `tmux display-message` twenty times a minute for the rest of the session."""
    _armed(d.DaemonPaths(profile.dir).root)
    monkeypatch.setattr(d, "read_stats", lambda p: {"context_pct": 90.0})
    daemon = _Daemon(profile, 90.0)
    monkeypatch.setattr(compaction, "apply",
                        lambda root, action, **_kw: (daemon.applied.append(action),
                                                     (1, "pane %3 is in copy mode"))[1])
    cfg.setting("context_compact_at").write(80)
    for tick in range(20):
        _beat(daemon, 1000.0 + tick * 3)
    assert daemon.applied == ["compact"], "it tried again on every beat"
    _beat(daemon, 1000.0 + d.COMPACT_RETRY + 1)
    assert daemon.applied == ["compact", "compact"], "and it never tried again"
