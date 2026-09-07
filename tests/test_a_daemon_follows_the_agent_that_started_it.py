"""What happens to a detached listener when the agent that started it quits.

Nothing, until now. The daemon is spawned detached on purpose — an agent's turn
kills everything the turn started, so a listener that must outlive the turn
cannot be in its process group — and nothing ever asked whether the agent was
still there. So it reconnected for ever to a session nobody was in, held the
lock, kept the repository's claim fresh, and answered `collab status` with
«live», which it was.

The obvious fix does not work, and that is the first thing here worth pinning.
A collab command's ancestry ends at `init`, which never exits, so «is any
forebear alive» is permanently true; and it begins with three shells that are
gone before the daemon has finished starting, so «are they all alive» is
permanently false. Neither says anything about the agent. What does is the
agent's own process, found by name.

The second is the direction it fails in. An agent that cannot be named means
this daemon follows nobody and behaves exactly as it did before — every one of
these tests exists to keep a plainly-running daemon from shutting itself down
because it could not read something.
"""

from __future__ import annotations

import os

import pytest

from collab import lockfile, owner
from collab.client.exclusive import Stamp, stamp_for


class Clock:
    def __init__(self, at: float = 1_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def tick(self, by: float) -> None:
        self.at += by


@pytest.fixture
def me():
    return stamp_for()


def _dead() -> Stamp:
    """A stamp for a process that certainly is not running."""
    return Stamp(pid=999_999, started="1", boot="a-machine-long-since-off")


# --- naming the agent ----------------------------------------------------------

def test_this_process_is_not_taken_for_an_agent():
    """The test runner is python, and python is not on the list."""
    assert not owner._wearing_the_name(os.getpid(), owner.known_names())


def test_the_chain_is_searched_nearest_first(monkeypatch):
    """Two agents from one terminal share every forebear but their own process."""
    monkeypatch.setattr(owner, "_wearing_the_name",
                        lambda pid, names: pid in (30, 40))
    assert owner.owner_in([10, 20, 30, 40]).pid == 30


def test_a_chain_with_no_agent_in_it_has_no_owner(monkeypatch):
    monkeypatch.setattr(owner, "_wearing_the_name", lambda pid, names: False)
    assert owner.owner_in([10, 20, 30]) is None


def test_the_detected_tool_is_looked_for_first_and_not_only():
    """A tool that announces itself is preferred; the rest are still candidates."""
    names = owner.known_names("codex")
    assert names[0] == "codex"
    assert "claude" in names
    assert set(owner.known_names()) == set(names)


def test_an_interpreter_is_read_through_to_the_script(monkeypatch):
    """A CLI shipped as a script is named by its path, not by its runtime.

    Asked about a pid with no `/proc` entry, so the `comm` read fails and the
    argv route is the one under test.
    """
    from collab.client import exclusive

    monkeypatch.setattr(exclusive, "argv",
                        lambda pid: ["/usr/bin/node", "/opt/x/bin/claude", "--help"])
    assert owner._wearing_the_name(999_999, ("claude",))


def test_an_unrelated_command_is_not_an_agent(monkeypatch):
    from collab.client import exclusive

    monkeypatch.setattr(exclusive, "argv",
                        lambda pid: ["/usr/bin/grep", "claude", "notes.txt"])
    assert not owner._wearing_the_name(999_999, ("claude",))


def test_a_process_that_says_nothing_about_itself_is_not_an_agent(monkeypatch):
    from collab.client import exclusive

    monkeypatch.setattr(exclusive, "argv", lambda pid: [])
    assert not owner._wearing_the_name(999_999, ("claude",))


# --- carrying it to a process that cannot see its own ancestry -----------------

def test_the_owner_travels_in_the_environment(monkeypatch, me):
    monkeypatch.setattr(owner, "current", lambda: me)
    env = owner.spawn_env({"PATH": "/bin"})
    assert env["PATH"] == "/bin"
    assert owner.from_env(env) == me


def test_no_owner_sets_no_variable(monkeypatch):
    """«No variable» and «a variable saying nothing» must not both be shapes to read."""
    monkeypatch.setattr(owner, "current", lambda: None)
    assert owner.ENV_OWNER not in owner.spawn_env({})
    assert owner.from_env({}) is None
    assert owner.from_env({owner.ENV_OWNER: ""}) is None


def test_the_repo_lock_carries_the_owner(tmp_path, me):
    lockfile.acquire(lockfile.Lock(name="a", session_id="s1",
                                   owner=me.encode()), tmp_path)
    assert owner.recorded(tmp_path, "s1") == me


def test_another_sessions_lock_is_not_our_owner(tmp_path, me):
    """Two agents in one repository: adopting the neighbour's owner would mean
    this daemon outliving its own agent for as long as the other kept working."""
    lockfile.acquire(lockfile.Lock(name="b", session_id="theirs",
                                   owner=me.encode()), tmp_path)
    assert owner.recorded(tmp_path, "ours") is None


def test_a_lock_with_no_owner_recorded_says_nothing(tmp_path):
    lockfile.acquire(lockfile.Lock(name="a", session_id="s1"), tmp_path)
    assert owner.recorded(tmp_path, "s1") is None


# --- the waiting ---------------------------------------------------------------

def test_a_present_agent_is_simply_followed(me):
    assert owner.Follower(me).look() == "following"


def test_nothing_to_follow_is_not_a_stop():
    """The whole safety property: unnameable is not gone."""
    follower = owner.Follower(None)
    assert follower.look() == "unowned"
    assert not follower.following
    assert follower.waiting() == 0.0


def test_a_missing_agent_is_waited_for_and_then_given_up_on():
    clock = Clock()
    follower = owner.Follower(_dead(), grace=120.0, now=clock)
    assert follower.look() == "waiting"
    clock.tick(119)
    assert follower.look() == "waiting"
    clock.tick(2)
    assert follower.look() == "gone"


def test_the_clock_runs_from_the_last_sighting_not_from_the_first_miss(me):
    """A beat that is late must not buy an absent agent extra time."""
    clock = Clock()
    follower = owner.Follower(me, grace=60.0, now=clock)
    assert follower.look() == "following"
    follower.owner = _dead()
    clock.tick(61)                      # one very late beat
    assert follower.look() == "gone"


def test_an_agent_that_comes_back_resets_everything(me):
    clock = Clock()
    follower = owner.Follower(_dead(), grace=60.0, now=clock)
    assert follower.look() == "waiting"
    clock.tick(30)
    assert follower.look(me) == "following"
    assert follower.gone_since is None
    clock.tick(600)
    assert follower.look() == "following"


def test_a_restarted_agent_is_adopted_off_the_lock(me):
    """What makes quitting and coming back cost nothing."""
    follower = owner.Follower(_dead())
    assert follower.look() == "waiting"
    assert follower.look(me) == "following"
    assert follower.owner == me


def test_a_lock_that_says_nothing_leaves_the_starting_owner_in_place(me):
    """An agent releasing its claim on the way out is exactly this case."""
    follower = owner.Follower(me)
    assert follower.look(None) == "following"
    assert follower.owner == me


def test_the_time_left_is_reported_while_it_runs():
    clock = Clock()
    follower = owner.Follower(_dead(), grace=100.0, now=clock)
    follower.look()
    clock.tick(40)
    assert follower.waiting() == pytest.approx(60.0)
    clock.tick(200)
    assert follower.waiting() == 0.0


# --- a turn the daemon started is not the agent --------------------------------

def test_a_turn_the_daemon_started_does_not_become_the_owner(tmp_path, me,
                                                             monkeypatch):
    """The daemon must outlive the TURN and not the agent, and a wake makes
    those two easy to confuse.

    A wake runs an agent as a child of the daemon — `codex-exec` spawns one
    outright — so a command issued from inside that turn has the woken process
    nearest in its own ancestry. Recorded as the owner it would give the
    session an owner that exits when the turn does, and two minutes later the
    daemon would stop itself, having been told its agent had gone by the very
    turn it started.
    """
    from collab import cli
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s1", url="http://h/", name="a",
                             host_name="h", token="t", home=str(tmp_path))
    lockfile.acquire(lockfile.Lock(name="a", session_id="s1",
                                   owner=me.encode()), tmp_path)
    listener = 4242
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: [99, listener, 7])
    monkeypatch.setattr(owner, "current", lambda: Stamp(pid=1234, started="x",
                                                        boot="y"))
    assert cli._who_owns_this(profile, listener) == me.encode()


def test_an_ordinary_command_does_record_the_agent_it_ran_from(tmp_path, me,
                                                               monkeypatch):
    from collab import cli
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s1", url="http://h/", name="a",
                             host_name="h", token="t", home=str(tmp_path))
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: [99, 7])
    monkeypatch.setattr(owner, "current", lambda: me)
    assert cli._who_owns_this(profile, 4242) == me.encode()


def test_naming_nobody_leaves_the_recorded_owner_where_it_is(tmp_path, me,
                                                             monkeypatch):
    """A command run from a plain shell can name nobody, and "" would erase a
    perfectly good owner recorded by the agent that really did start this."""
    from collab import cli
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s1", url="http://h/", name="a",
                             host_name="h", token="t", home=str(tmp_path))
    lockfile.acquire(lockfile.Lock(name="a", session_id="s1",
                                   owner=me.encode()), tmp_path)
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: [99, 7])
    monkeypatch.setattr(owner, "current", lambda: None)
    assert cli._who_owns_this(profile, 0) == me.encode()


def test_another_sessions_claim_is_not_inherited(tmp_path, me, monkeypatch):
    from collab import cli
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="ours", url="http://h/", name="a",
                             host_name="h", token="t", home=str(tmp_path))
    lockfile.acquire(lockfile.Lock(name="b", session_id="theirs",
                                   owner=me.encode()), tmp_path)
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: [99, 7])
    monkeypatch.setattr(owner, "current", lambda: None)
    assert cli._who_owns_this(profile, 0) == ""


# --- reading `ps` where there is no /proc --------------------------------------

def test_the_ps_columns_name_an_agent_the_same_three_ways():
    """comm, the program, and — only for an interpreter — what it runs."""
    assert owner._named_in(["claude", "/usr/local/bin/claude", "--resume"],
                           ("claude",))
    assert owner._named_in(["node", "/usr/bin/node", "/opt/x/bin/claude"],
                           ("claude",))
    assert not owner._named_in([], ("claude",))
    assert not owner._named_in(["zsh", "-zsh"], ("claude",))


def test_a_command_that_merely_mentions_an_agent_is_not_one():
    """Scanning the arguments would make `grep claude notes.txt` an agent — and
    one this daemon would then follow into oblivion when the grep finished."""
    assert not owner._named_in(["grep", "/usr/bin/grep", "claude", "notes.txt"],
                               ("claude",))
    assert not owner._named_in(["vim", "/usr/bin/vim", "claude-notes.md"],
                               ("claude",))


# --- opting out ----------------------------------------------------------------

def test_keep_starts_a_daemon_that_follows_nobody(tmp_path, me, monkeypatch):
    """`--keep` is the escape hatch, so it has to actually withhold the name."""
    from collab.client import onboard
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s1", url="http://h/", name="a",
                             host_name="h", token="t", home=str(tmp_path))
    seen: dict = {}

    class Popen:
        pid = 4242

        def __init__(self, argv, **kw):
            seen.update(kw)

    monkeypatch.setattr(onboard.subprocess, "Popen", Popen)
    monkeypatch.setattr(onboard.exclusive, "locking_available", lambda: True)
    monkeypatch.setattr(owner, "current", lambda: me)

    onboard.spawn_daemon(profile, follow=False)
    assert owner.ENV_OWNER not in seen["env"]
    assert seen["env"]["COLLAB_HOME"] == str(tmp_path)
    assert seen["start_new_session"] is True, "it must still outlive the turn"

    seen.clear()
    onboard.spawn_daemon(profile)
    assert seen["env"][owner.ENV_OWNER] == me.encode()
    assert seen["start_new_session"] is True


def test_the_setting_is_read_every_beat_not_at_construction():
    """Turning it off must reach a daemon that is already running, which is the
    promise every setting the daemon reads makes."""
    import inspect

    from collab.client import daemon as d

    body = inspect.getsource(d.Daemon._follow_the_agent)
    assert "follow_agent_enabled()" in body
    assert "self._following.look" in body


# --- the one failure that matters most ------------------------------------------

def test_no_reachable_state_stops_a_daemon_whose_agent_is_alive(me):
    """The property to hold above all the others.

    A daemon that leaks is recoverable with `collab daemon stop`. A daemon that
    stops while its agent is working is somebody's session disappearing under
    them, and nothing they did caused it.
    """
    follower = owner.Follower(me)
    assert {follower.look() for _ in range(100)} == {"following"}

    unowned = owner.Follower(None)
    assert {unowned.look() for _ in range(100)} == {"unowned"}

    # A lock that says nothing — a junk value, a released claim, another
    # session's — leaves the good owner exactly where it was.
    kept = owner.Follower(me)
    assert kept.look(None) == "following" and kept.owner == me


def test_a_clock_that_runs_backwards_never_shortens_the_grace():
    """Wall-clock time is not monotonic; the daemon's beat reads `time.time`.
    A backwards step must not turn a pause into a stop."""
    class Backwards:
        def __init__(self):
            self.at = 1_000.0

        def __call__(self):
            self.at -= 5
            return self.at

    follower = owner.Follower(_dead(), grace=60.0, now=Backwards())
    assert {follower.look() for _ in range(20)} == {"waiting"}
