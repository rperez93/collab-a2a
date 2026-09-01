"""Usage figures belong to the agent that produced them.

Two agents share a repo and get a state directory each. Usage is published
under a name, so a file of figures is a claim about a person — and everything
that writes one has to work out which directory is whose. The status line has
the worst of it: Claude Code starts it, hands it a cost and a quota, and it
knows the agent's working directory and nothing else. `SessionProfile.current`
answers with the repo's default directory when it cannot tell the agents apart,
which is the right answer for a command that has to show something and the
wrong one for a write.

So every prompt, one agent's spend, model and remaining quota were written into
the other agent's directory, and that agent's daemon published them as its own.
Not a display glitch: the figures a collaborator reads to decide who takes the
next task were the wrong agent's.
"""

from __future__ import annotations

import json

import pytest

from collab import config, stats
from collab.config import SessionProfile
from collab.statusline import render as r


def _profile(home, session_id="s", pid="p_me"):
    home.mkdir(parents=True, exist_ok=True)
    p = SessionProfile(session_id=session_id, url="u", name=home.name,
                       host_name="host", token="t", home=str(home),
                       participant_id=pid)
    p.save()
    return p


# --- the stamp --------------------------------------------------------------

def test_figures_are_written_with_an_owner(tmp_path):
    profile = _profile(tmp_path / ".collab")
    stats.write_stats(profile, {"model": "Opus 5", "cost_usd": 1.5})

    raw = json.loads((profile.dir / stats.STATS_FILE).read_text())
    assert raw[stats.OWNER_KEY] == "p_me"


def test_a_profile_reads_back_its_own_figures(tmp_path):
    profile = _profile(tmp_path / ".collab")
    stats.write_stats(profile, {"model": "Opus 5"})

    assert stats.read_stats(profile) == {"model": "Opus 5"}, "and no stamp in them"


def test_somebody_elses_figures_are_not_published_as_yours(tmp_path):
    """The bug, in one assertion."""
    mine = _profile(tmp_path / ".collab", pid="p_me")
    theirs = _profile(tmp_path / ".collab-bob", session_id="s", pid="p_bob")
    stats.write_stats(theirs, {"model": "gpt-5", "cost_usd": 9.0})

    # Their figures, dropped into my directory by whatever means.
    (mine.dir / stats.STATS_FILE).write_text(
        (theirs.dir / stats.STATS_FILE).read_text())

    assert stats.read_stats(mine) == {}


def test_an_unstamped_file_is_not_claimed(tmp_path):
    """Every writer stamps now, so what is left unstamped is not evidence."""
    profile = _profile(tmp_path / ".collab")
    (profile.dir / stats.STATS_FILE).write_text(json.dumps({"model": "Opus 5"}))

    assert stats.read_stats(profile) == {}


def test_a_profile_with_no_participant_id_still_owns_its_figures(tmp_path):
    """Older profiles predate the id; the directory identifies them instead."""
    profile = _profile(tmp_path / ".collab", pid="")
    stats.write_stats(profile, {"model": "Opus 5"})

    assert stats.read_stats(profile) == {"model": "Opus 5"}


# --- the status line, which is where the wrong directory was chosen ---------

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A repo with two agents in it and neither provably ours."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    for name in (".collab", ".collab-bob"):
        home = tmp_path / name
        _profile(home)
        (home / "current").write_text("s\n")
    return tmp_path


PAYLOAD = json.dumps({"model": {"display_name": "Opus 5"}, "cost": {"total_cost_usd": 3.2}})


def test_the_status_line_writes_nothing_it_cannot_attribute(repo, monkeypatch):
    """Unable to tell the agents apart, it used to write to the default one."""
    monkeypatch.setattr(r, "claimed_home", lambda cwd=None: None)

    r.stash_agent_stats(PAYLOAD, repo)

    for name in (".collab", ".collab-bob"):
        assert not (repo / name / "sessions" / "s" / stats.STATS_FILE).exists()


def test_it_writes_to_the_directory_it_can_prove_is_its_own(repo, monkeypatch):
    monkeypatch.setattr(r, "claimed_home", lambda cwd=None: repo / ".collab-bob")

    r.stash_agent_stats(PAYLOAD, repo)

    assert (repo / ".collab-bob" / "sessions" / "s" / stats.STATS_FILE).exists()
    assert not (repo / ".collab" / "sessions" / "s" / stats.STATS_FILE).exists()


def test_an_explicit_home_is_proof_enough(repo, monkeypatch):
    """COLLAB_HOME is somebody saying which agent this is, in so many words."""
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-bob"))
    monkeypatch.setattr(r, "claimed_home", lambda cwd=None: None)

    r.stash_agent_stats(PAYLOAD, repo)

    assert (repo / ".collab-bob" / "sessions" / "s" / stats.STATS_FILE).exists()


# --- and the proof itself ---------------------------------------------------

def test_claimed_home_is_none_when_nothing_proves_ownership(repo):
    """Neither directory is locked by anything in this process's ancestry."""
    assert config.claimed_home(repo) is None


def test_a_command_still_gets_an_answer(repo):
    """resolve_home must keep falling back: a command has to act on something."""
    assert config.resolve_home(cwd=repo) == repo / ".collab"
