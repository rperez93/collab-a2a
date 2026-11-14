"""Each agent in a repo has a name and a colour of its own.

Separate state directories came first — `.collab-bob` beside `.collab` —
because what two agents in one repo collide over is collab's state and not
their files. They still shared one name and one colour for the whole machine,
so the second agent was the same person in the same colour, and the only thing
telling them apart was whichever name the hub handed out.

The file holds only what somebody chose. Nothing derived from the machine, the
user or the path is stored: a derived value written down is a second copy of
one fact, and every defect worth having found in this code has that shape.
"""
from __future__ import annotations

import json

import pytest

from collab import config, identity


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with two agent directories in it."""
    (tmp_path / ".collab").mkdir()
    (tmp_path / ".collab-alice").mkdir()
    (tmp_path / ".collab-bob").mkdir()
    identity._CACHE.clear()
    config._HOME_CACHE.clear()
    config._CACHE.clear()
    yield tmp_path
    identity._CACHE.clear()
    config._HOME_CACHE.clear()
    config._CACHE.clear()


# --- the directory names the agent -------------------------------------------

def test_the_directory_says_who_lives_there(repo):
    """`.collab-alice` is itself a statement of whose state that is."""
    assert identity.agent_slug(repo / ".collab-alice") == "alice"
    assert identity.agent_slug(repo / ".collab-bob") == "bob"


def test_the_shared_directory_names_nobody(repo):
    """`.collab` belongs to whoever is not using their own."""
    assert identity.agent_slug(repo / ".collab") == ""


# --- the file ----------------------------------------------------------------

def test_only_what_somebody_chose_is_written(repo):
    """No id, no machine, no path — nothing derived.

    A derived value in a file is a second copy waiting to disagree with the
    first: copy the directory elsewhere and the file says one thing while the
    code says another.
    """
    identity.save(repo / ".collab-alice", name="alice", color="#008080")
    on_disk = json.loads(
        (repo / ".collab-alice" / identity.IDENTITY_FILE).read_text())
    assert on_disk == {"name": "alice", "color": "#008080"}


def test_saving_merges_rather_than_replaces(repo):
    """A command that knows one thing must not erase what another wrote.

    The same reason `/stats` merges: `collab color` knows the colour and
    nothing else, and running it should not cost you your name.
    """
    home = repo / ".collab-alice"
    identity.save(home, name="alice")
    identity.save(home, color="#00cccc")
    got = identity.load(home)
    assert got["name"] == "alice"
    assert got["color"] == "#00cccc"


def test_passing_none_clears_a_field(repo):
    home = repo / ".collab-alice"
    identity.save(home, name="alice", color="#008080")
    identity.save(home, color=None)
    assert "color" not in identity.load(home)
    assert identity.load(home)["name"] == "alice"


def test_a_broken_file_costs_a_setting_not_the_chat(repo):
    """This is read while drawing the conversation."""
    home = repo / ".collab-alice"
    (home / identity.IDENTITY_FILE).write_text("{not json", encoding="utf-8")
    identity._CACHE.clear()
    assert identity.load(home) == {}
    assert identity.describe(home)["name"] == "alice"   # from the directory


def test_a_missing_file_is_not_an_error(repo):
    assert identity.load(repo / ".collab-bob") == {}


def test_the_file_is_not_re_read_when_nothing_changed(repo, monkeypatch):
    """The colour is asked for on every frame."""
    home = repo / ".collab-alice"
    identity.save(home, color="#008080")
    identity.load(home)
    reads: list = []
    from pathlib import Path as _P
    real = _P.read_text
    monkeypatch.setattr(_P, "read_text",
                        lambda self, *a, **k: (reads.append(1),
                                               real(self, *a, **k))[1])
    for _ in range(20):
        identity.load(home)
    assert reads == []


def test_a_writer_sees_its_own_change(repo):
    """Two writes inside one second must not be hidden by the stamp."""
    home = repo / ".collab-alice"
    identity.save(home, color="#008080")
    identity.save(home, color="#ff7f50")
    assert identity.load(home)["color"] == "#ff7f50"


# --- what gets published ------------------------------------------------------

def test_describe_carries_the_name_and_the_colour(repo):
    identity.save(repo / ".collab-alice", name="alice", color="#00cccc")
    assert identity.describe(repo / ".collab-alice") == {
        "name": "alice", "color": "#00cccc"}


def test_describe_falls_back_to_the_directory_for_a_name(repo):
    assert identity.describe(repo / ".collab-bob")["name"] == "bob"


def test_an_agent_with_no_colour_says_so_rather_than_inventing_one(repo):
    """A dealt colour is the viewer's business, not something to publish."""
    assert identity.describe(repo / ".collab-alice", "alice")["color"] is None


# --- the colour resolves per agent -------------------------------------------

def test_this_agents_colour_wins_over_the_machines(repo, tmp_path, monkeypatch):
    """R21: colour is an agent's identity; a machine-file colour is legacy and
    never names another agent, so only this agent's own applies."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"color": "#ff7f50"}), encoding="utf-8")
    monkeypatch.setattr(config, "global_config_path", lambda: cfg)
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-alice"))
    config._CACHE.clear()
    config._HOME_CACHE.clear()

    assert config.default_color() != "#ff7f50"          # legacy machine colour: not inherited
    identity.save(repo / ".collab-alice", color="#00cccc")
    config._HOME_CACHE.clear()
    assert config.default_color() == "#00cccc"


def test_the_name_comes_from_the_directory_when_nothing_says_otherwise(
        repo, tmp_path, monkeypatch):
    """`.collab-alice` is itself a statement of who lives there.

    The global config is emptied on purpose: with a display_name in it this
    test passes whether or not the directory is consulted at all, which is how
    an earlier version of it passed with the function under test deleted.
    """
    cfg = tmp_path / "empty.json"
    monkeypatch.setattr(config, "global_config_path", lambda: cfg)
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-alice"))
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    config._CACHE.clear()
    config._HOME_CACHE.clear()
    assert config.resolve_name() == "alice"


def test_an_explicit_name_still_wins(repo, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-alice"))
    config._HOME_CACHE.clear()
    assert config.resolve_name("something-else") == "something-else"
