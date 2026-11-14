"""One agent's state directory must never answer for another's.

The rule before this compared nothing: if exactly one per-agent directory was
in use, it was assumed to be ours. For the agent holding the *default*
directory that was exactly backwards — every bare command it ran resolved into
the other agent's state, so its messages went out under the other name, its
viewer showed the other name, and its `kill` stopped the other listener.

Names cannot decide it, because two agents on one machine resolve the same
default name — that is why they collide at all. Process lineage can.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from collab import lockfile
from collab.config import SessionProfile, base_home, resolve_home


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    return base_home().parent


def _claim(home: Path, name: str, chain: list[int]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lockfile.acquire(lockfile.Lock(name=name, session_id="s_1",
                                   hub_pid=os.getpid(), owner_pids=chain), home)


def _mine() -> list[int]:
    return lockfile.ancestry()


def _pid_max() -> int:
    try:
        with open("/proc/sys/kernel/pid_max", encoding="ascii") as fh:
            return int(fh.read())
    except (OSError, ValueError):
        return 4_194_304            # Linux's ceiling; macOS pids stay far below it


#: A pid no process can have: one past the kernel's ceiling. The fixed numbers
#: these tests used before — 111 111 and 999 999 — are ordinary pids on a host
#: whose ceiling is 4 194 304, and a heavy suite reaches them.
STRANGER = _pid_max() + 1


@pytest.fixture
def chain(monkeypatch):
    """Give `resolve_home` a process chain of our choosing, nearest first.

    THE REAL CHAIN IS NOT THE POINT OF THE TESTS THAT USE THIS, and it was what
    made one of them flaky. Its shape is whatever launched pytest: five deep
    from an interactive shell, but only `[python3, flock]` when the suite was
    started by a shell that has since exited, leaving `flock` reparented to
    init. A test that built «our process» as `chain[1]` and «the shared
    terminal» as `chain[-1]` then named ONE pid twice; alice's claim and bob's
    met the chain at the same distance, and the tie correctly decided nothing.
    Reproduced with `setsid sh -c 'flock … pytest … &'`: fails every time.

    The numbers sit above the pid ceiling, so none of them can be anything real.
    """
    def use(pids: list[int]) -> list[int]:
        monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: list(pids))
        return pids
    return use


def test_the_default_holder_keeps_the_default(repo):
    """The reported regression, in one line.

    The REAL chain on purpose, and safe at any depth: it always begins with
    this process, so alice meets it at distance 0 and the stranger not at all.
    """
    _claim(base_home(), "alice", _mine())
    _claim(repo / ".collab-bob", "bob", [STRANGER])

    assert resolve_home() == base_home()


def test_the_sibling_holder_keeps_the_sibling(repo):
    _claim(base_home(), "alice", [STRANGER])
    _claim(repo / ".collab-bob", "bob", _mine())

    assert resolve_home() == repo / ".collab-bob"


def test_agents_sharing_a_terminal_are_still_told_apart(repo, chain):
    """Two agents started from one shell share everything above it.

    So "shares an ancestor" answers yes for every claim in the repo. What
    separates them is where the sharing starts: each meets its own process
    first.
    """
    command, ours, shell, terminal = chain([STRANGER + 1, STRANGER + 2,
                                            STRANGER + 3, STRANGER + 4])

    _claim(base_home(), "alice", [STRANGER, terminal])        # theirs
    _claim(repo / ".collab-bob", "bob", [ours, terminal])     # ours

    assert resolve_home() == repo / ".collab-bob"


def test_the_two_agents_are_told_apart_however_shallow_the_chain(repo, chain):
    """The flaky case itself: our process right under the shared one.

    `[python3, flock]` with flock reparented to init is the shallowest chain a
    suite can have. The two claims must still differ by where they meet it.
    """
    ours, shared = chain([STRANGER + 1, STRANGER + 2])

    _claim(base_home(), "alice", [STRANGER, shared])
    _claim(repo / ".collab-bob", "bob", [ours, shared])

    assert resolve_home() == repo / ".collab-bob"


def test_an_equal_claim_from_both_decides_nothing(repo, chain):
    """If the only thing shared is shared equally, it is not evidence."""
    *_, shared = chain([STRANGER + 1, STRANGER + 2, STRANGER + 3])
    _claim(base_home(), "alice", [shared])
    _claim(repo / ".collab-bob", "bob", [shared])

    assert resolve_home() == base_home(), "fall back, do not pick a stranger's"


def test_a_lock_from_before_lineage_was_recorded(repo):
    """An older collab wrote no owner_pids; that must not resolve to anyone."""
    _claim(base_home(), "alice", [])
    _claim(repo / ".collab-bob", "bob", [])

    assert resolve_home() == base_home()


# --- the viewer, which is where it was noticed ------------------------------

def test_the_tmux_viewer_is_pinned_to_its_own_home(repo, monkeypatch, capsys):
    """A pane left to resolve for itself lands wherever the rule guesses, and
    then displays the other agent's name as yours."""
    from collab import cli

    home = repo / ".collab-bob"
    home.mkdir(parents=True)
    profile = SessionProfile(session_id="s_1", url="u", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.dir.mkdir(parents=True, exist_ok=True)
    profile.save()
    (home / "current").write_text("s_1")

    captured = {}

    def fake_pane(argv, env=None, **kw):
        captured["env"] = dict(env or {})
        return "opened"

    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr("collab.client.watch.open_tmux_pane", fake_pane)
    monkeypatch.setattr("collab.client.watch.tmux_available", lambda: True)
    monkeypatch.setattr("collab.client.watch.in_tmux", lambda: True)

    cli.main(["watch", "--tmux"])

    assert captured["env"].get("COLLAB_HOME") == str(home), \
        "the pane must be told which session it is showing"


def test_the_listener_command_carries_a_non_default_home(repo, capsys):
    """The agent is told to run this in a fresh shell, where nothing else says
    which session it belongs to."""
    from collab.cli import _monitor_hint

    home = repo / ".collab-bob"
    profile = SessionProfile(session_id="s_1", url="u", name="bob",
                             host_name="alice", token="t", home=str(home))
    _monitor_hint(profile, {})
    assert f"COLLAB_HOME={home}" in capsys.readouterr().out


def test_the_default_home_needs_no_prefix(repo, capsys):
    from collab.cli import _monitor_hint

    profile = SessionProfile(session_id="s_1", url="u", name="alice",
                             host_name="alice", token="t",
                             home=str(base_home()))
    _monitor_hint(profile, {})
    assert "COLLAB_HOME=" not in capsys.readouterr().out, "noise when unneeded"
