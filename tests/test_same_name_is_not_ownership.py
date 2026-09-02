"""A matching display name is not proof that a lock is ours.

`collab join` used to read a held lock carrying our own name as our own claim
and carry on into that directory. The name is the one thing two agents on one
machine are guaranteed to share — both resolve the same global default — so a
second agent joining under it walked into the first agent's `.collab`,
overwrote its profile and its lock, and the first agent's status line then
described the second agent as itself. What does differ is where each is
running from, and the lock records that: ownership is read from the chain.
"""

from __future__ import annotations

import os

import pytest

from collab import cli, config, lockfile

NAME = "rafael"


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    return tmp_path


def _held(home, chain, name=NAME):
    home.mkdir(parents=True, exist_ok=True)
    lockfile.acquire(lockfile.Lock(name=name, session_id="s_1", role="host",
                                   hub_pid=os.getpid(), state_dir=str(home),
                                   owner_pids=chain), home)


def _join_args():
    return cli.build_parser().parse_args(["join", "--name", NAME])


#: Alive, and in nobody's ancestry — `ancestry()` stops above pid 1.
UNRELATED_LIVE = 1
DEAD = 999_999


# --- the lock's own view ------------------------------------------------------

def test_a_chain_that_reaches_the_living_owner_is_ours():
    lock = lockfile.Lock(name=NAME, session_id="s_1", owner_pids=lockfile.ancestry())
    assert lock.owned_by(lockfile.ancestry())


def test_a_chain_that_only_shares_infrastructure_is_not(monkeypatch):
    """The other agent's process is alive and not above us; whatever we share
    beyond it — a tmux server, a terminal — is not ownership."""
    mine = lockfile.ancestry()
    lock = lockfile.Lock(name=NAME, session_id="s_1",
                         owner_pids=[DEAD, UNRELATED_LIVE, *mine[1:]])
    assert not lock.owned_by(mine)


def test_a_claim_whose_agent_has_gone_can_be_reclaimed_from_where_it_ran():
    """Every process of theirs that is dead is skipped; the first living one
    is what has to be ours. An agent that restarted from the same shell
    re-claims its own directory this way."""
    mine = lockfile.ancestry()
    lock = lockfile.Lock(name=NAME, session_id="s_1",
                         owner_pids=[DEAD, DEAD, *mine[1:]])
    assert lock.owned_by(mine)


def test_a_lock_with_no_chain_belongs_to_nobody():
    assert not lockfile.Lock(name=NAME, session_id="s_1").owned_by(lockfile.ancestry())


# --- join: same name, different agent ---------------------------------------

def test_the_same_name_from_another_agent_gets_its_own_directory(repo, capsys):
    """The reported failure: two agents under one default name shared .collab."""
    _held(repo / ".collab", [DEAD, UNRELATED_LIVE])

    assert cli._own_state_dir(_join_args(), NAME) is None
    assert os.environ.get("COLLAB_HOME") == str(repo / f".collab-{NAME}")
    assert "yours is" in capsys.readouterr().out, "and it says so"


def test_our_own_claim_under_our_own_name_is_kept(repo):
    """Re-running join is idempotent for the agent that made the claim."""
    _held(repo / ".collab", lockfile.ancestry())

    assert cli._own_state_dir(_join_args(), NAME) is None
    assert "COLLAB_HOME" not in os.environ


def test_our_own_claim_under_another_name_is_still_ours(repo):
    """`join --name alicia` from the agent that holds .collab as alice is a
    rename, not a second agent: the chain says so and the name does not."""
    _held(repo / ".collab", lockfile.ancestry(), name="alice")

    assert cli._own_state_dir(_join_args(), NAME) is None
    assert "COLLAB_HOME" not in os.environ


def test_a_third_agent_under_the_name_is_numbered(repo):
    _held(repo / ".collab", [DEAD, UNRELATED_LIVE])
    _held(repo / f".collab-{NAME}", [DEAD, UNRELATED_LIVE])

    assert cli._own_state_dir(_join_args(), NAME) is None
    assert os.environ.get("COLLAB_HOME") == str(repo / f".collab-{NAME}-2")


def test_an_explicit_home_is_never_overridden(repo, monkeypatch):
    chosen = repo / ".collab-review"
    monkeypatch.setenv("COLLAB_HOME", str(chosen))
    _held(repo / ".collab", [DEAD, UNRELATED_LIVE], name="alice")

    assert cli._own_state_dir(_join_args(), NAME) is None
    assert os.environ["COLLAB_HOME"] == str(chosen)
