"""A state directory per agent, in one checkout.

Two agents in the same repo collide over collab's state — one profile, one
listener, one inbox, one lock — and nothing else. So that is the only thing
separated: `.collab-bob` beside `.collab`, same working tree, same files.

The hard part is not creating it. It is that a *later* command — `collab send`,
run as a fresh process minutes afterwards — has to find the same directory
again, with nothing carried over from the join.
"""

from __future__ import annotations

import os

import pytest

from collab import lockfile
from collab.config import (
    COLLAB_DIRNAME,
    agent_home,
    base_home,
    resolve_home,
    safe_slug,
    sibling_homes,
)


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    return base_home().parent


def _claim(home, name="alice", session_id="s_1", pid=None, owner_pids=None):
    """A claim on a directory. By default it is ours — same process chain."""
    home.mkdir(parents=True, exist_ok=True)
    lockfile.acquire(lockfile.Lock(
        name=name, session_id=session_id, hub_pid=pid or os.getpid(),
        owner_pids=lockfile.ancestry() if owner_pids is None else owner_pids,
    ), home)


# --- naming ------------------------------------------------------------------

def test_the_directory_is_named_after_the_agent(repo):
    assert agent_home("bob") == repo / ".collab-bob"


def test_a_name_that_would_escape_the_repo_cannot(repo):
    home = agent_home("../../etc")
    assert home.parent == repo
    assert ".." not in home.name


@pytest.mark.parametrize("name,slug", [
    ("bob", "bob"), ("Bob Smith", "Bob-Smith"), ("agent/2", "agent-2"),
    ("", "agent"), ("...", "agent"),
])
def test_slugs(name, slug):
    assert safe_slug(name) == slug


# --- choosing one ------------------------------------------------------------

def test_an_empty_repo_uses_the_default(repo):
    assert resolve_home("bob") == repo / COLLAB_DIRNAME


def test_a_repo_held_by_someone_else_sends_us_beside_it(repo):
    _claim(base_home(), name="alice", owner_pids=[999998])
    assert resolve_home("bob") == repo / ".collab-bob"


def test_our_own_claim_is_not_someone_else(repo):
    """Re-running join for a session we are already in is not a collision."""
    _claim(base_home(), name="bob")
    assert resolve_home("bob") == base_home()


def test_a_stale_claim_does_not_displace_us(repo, monkeypatch):
    _claim(base_home(), name="alice", pid=999999)
    monkeypatch.setattr(os, "kill", _gone)
    assert resolve_home("bob") == base_home()


def test_an_explicit_home_still_wins(repo, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", "/somewhere/else")
    from collab.config import collab_home

    _claim(base_home(), name="alice")
    assert str(collab_home("bob")) == "/somewhere/else"


# --- finding it again, with no name to go on --------------------------------

def test_a_later_command_finds_the_home_its_own_agent_claimed(repo):
    """`collab send` knows nothing about the join that came before it, so the
    claim carries the process chain that made it."""
    _claim(repo / ".collab-bob", name="bob", session_id="s_1")
    assert resolve_home() == repo / ".collab-bob"


def test_the_default_holder_is_not_redirected_to_a_sibling(repo):
    """The regression this rule replaced.

    With alice on .collab and bob on .collab-bob, every bare command alice ran
    resolved to *bob's* directory: her messages went out under his name, her
    viewer showed his, and her `kill` stopped his listener.
    """
    _claim(base_home(), name="alice")                      # ours: same process
    _claim(repo / ".collab-bob", name="bob", owner_pids=[999999])

    assert resolve_home() == base_home()


def test_a_stranger_is_sent_to_the_default_not_to_someone_elses(repo):
    """An agent whose lineage matches nothing gets the repo's own directory —
    never another agent's, which is the mistake worth not repeating."""
    _claim(base_home(), name="alice", owner_pids=[999998])
    _claim(repo / ".collab-bob", name="bob", owner_pids=[999999])

    assert resolve_home() == base_home()


def test_a_folder_the_user_named_is_still_recognised_as_theirs(repo):
    """--home my-state is as much someone's as .collab-bob is."""
    _claim(repo / "my-state", name="carol")
    assert resolve_home() == repo / "my-state"


def test_a_dead_sibling_is_not_mistaken_for_ours(repo, monkeypatch):
    live, dead = os.getpid(), 999999

    def selective(pid, sig):
        if pid == dead:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", selective)
    _claim(base_home(), name="alice", pid=live)
    _claim(repo / ".collab-gone", name="carol", pid=dead)

    assert resolve_home() == base_home(), "no live sibling; fall back"


def test_with_two_live_siblings_a_name_is_required(repo):
    """Three agents is genuinely ambiguous, so it does not guess."""
    _claim(base_home(), name="alice", owner_pids=[999998])
    _claim(repo / ".collab-bob", name="bob", owner_pids=[999997])
    _claim(repo / ".collab-carol", name="carol", owner_pids=[999996])

    assert resolve_home() == base_home(), "ambiguous — do not pick one at random"
    assert resolve_home("carol") == repo / ".collab-carol", "with a name it is clear"


def test_siblings_are_listed(repo):
    _claim(base_home())
    _claim(repo / ".collab-bob")
    _claim(repo / ".collab-carol")

    names = {p.name for p in sibling_homes()}
    assert names == {".collab-bob", ".collab-carol"}
    assert COLLAB_DIRNAME not in names, "the default is not its own sibling"


# --- it is git-invisible -----------------------------------------------------

def test_a_new_directory_ignores_itself(repo):
    from collab.config import ensure_home

    home = ensure_home(name="bob")
    assert (home / ".gitignore").read_text().strip().endswith("*")


def _gone(pid, sig):
    raise ProcessLookupError
