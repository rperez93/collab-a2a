"""`collab agent create|update|delete|list`, and joining as a chosen agent.

Grouped under `agent` rather than as loose commands: `collab update` already
means "install a newer collab", and a second `update` meaning something else
is the kind of collision that makes somebody run the wrong one once and never
trust either again.
"""
from __future__ import annotations

import argparse

import pytest

from collab import cli, config, identity, peers


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".collab").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    monkeypatch.setattr(peers, "current_user", lambda: "alice")
    monkeypatch.setattr(peers, "machine_name", lambda: "workstation")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    identity._CACHE.clear()
    config._CACHE.clear()
    config._HOME_CACHE.clear()
    yield tmp_path
    identity._CACHE.clear()
    config._CACHE.clear()
    config._HOME_CACHE.clear()


def agent(action, name="", **kw):
    kw.setdefault("color", "")
    kw.setdefault("rename", "")
    kw.setdefault("force", False)
    return cli.cmd_agent(argparse.Namespace(action=action, name=name, **kw))


def no_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)


def a_terminal(monkeypatch, answer):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: answer)


# --- create -------------------------------------------------------------------

def test_create_makes_a_directory_with_an_identity(repo):
    assert agent("create", "alice", color="#00cccc") == 0
    home = repo / ".collab-alice"
    assert home.is_dir()
    got = identity.load(home)
    assert got["name"] == "alice"
    assert got["color"] == "#00cccc"


def test_create_ignores_the_state_directory_it_makes(repo):
    """Its own state must not end up committed to the repo it lives in."""
    agent("create", "alice")
    assert (repo / ".collab-alice" / ".gitignore").exists()


def test_create_normalises_the_name(repo):
    agent("create", "Alice Two")
    assert (repo / f".collab-{config._slug('Alice Two')}").is_dir()


def test_create_refuses_a_name_that_is_taken(repo):
    """Never silently adopt an existing directory.

    It may hold a live session, and a `create` that quietly means `use` is how
    somebody ends up editing an agent they thought they were making.
    """
    agent("create", "alice", color="#008080")
    assert agent("create", "alice", color="#ff7f50") == 2
    assert identity.load(repo / ".collab-alice")["color"] == "#008080"


def test_create_refuses_a_colour_it_cannot_read(repo):
    assert agent("create", "alice", color="sort-of-blue") == 2
    assert not (repo / ".collab-alice").exists(), "it made one anyway"


def test_create_needs_a_name(repo):
    assert agent("create", "") == 2


# --- update -------------------------------------------------------------------

def test_update_changes_the_colour(repo):
    agent("create", "alice", color="#008080")
    assert agent("update", "alice", color="#ff7f50") == 0
    assert identity.load(repo / ".collab-alice")["color"] == "#ff7f50"


def test_update_renames_without_moving_the_directory(repo, capsys):
    """The directory may be holding a live session.

    Moving it out from under a running daemon to keep a string tidy is a bad
    trade, so the label changes and the id stays — and it says so.
    """
    agent("create", "alice")
    assert agent("update", "alice", rename="bob") == 0
    assert (repo / ".collab-alice").is_dir()
    assert identity.load(repo / ".collab-alice")["name"] == "bob"
    assert "alice" in capsys.readouterr().out


def test_update_needs_something_to_change(repo):
    agent("create", "alice")
    assert agent("update", "alice") == 2


def test_update_refuses_an_agent_that_is_not_there(repo):
    assert agent("update", "ghost", color="#008080") == 2


# --- delete -------------------------------------------------------------------

def test_delete_asks_first(repo, monkeypatch):
    agent("create", "alice")
    a_terminal(monkeypatch, "n")
    assert agent("delete", "alice") == 2
    assert (repo / ".collab-alice").is_dir(), "it deleted after being told no"


def test_delete_removes_it_when_told_yes(repo, monkeypatch):
    agent("create", "alice")
    a_terminal(monkeypatch, "y")
    assert agent("delete", "alice") == 0
    assert not (repo / ".collab-alice").exists()


def test_delete_refuses_without_a_person(repo, monkeypatch):
    """A script deleting somebody's session state because nobody was there to
    say no is not a thing to allow by default."""
    agent("create", "alice")
    no_terminal(monkeypatch)
    assert agent("delete", "alice") == 2
    assert (repo / ".collab-alice").is_dir()


def test_force_deletes_without_asking(repo, monkeypatch):
    agent("create", "alice")
    no_terminal(monkeypatch)
    assert agent("delete", "alice", force=True) == 0
    assert not (repo / ".collab-alice").exists()


def test_delete_will_not_touch_a_directory_in_use(repo, monkeypatch):
    """A daemon writing into a directory that is no longer there is worse than
    an agent that outlived its usefulness."""
    agent("create", "alice")
    monkeypatch.setattr(cli, "_is_busy",
                        lambda h: type("L", (), {"describe": lambda s: "held"})())
    assert agent("delete", "alice", force=True) == 1
    assert (repo / ".collab-alice").is_dir()


def test_delete_leaves_the_working_tree_alone(repo, monkeypatch):
    """Only collab's state is separated, and only collab's state goes."""
    (repo / "work.txt").write_text("mine", encoding="utf-8")
    agent("create", "alice")
    no_terminal(monkeypatch)
    agent("delete", "alice", force=True)
    assert (repo / "work.txt").read_text() == "mine"


# --- list ---------------------------------------------------------------------

def test_list_shows_them_all(repo, capsys):
    agent("create", "alice", color="#00cccc")
    agent("create", "bob", color="#ff7f50")
    assert agent("list") == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" in out and "#00cccc" in out


def test_list_with_nothing_says_how_to_start(repo, capsys):
    assert agent("list") == 0
    assert "collab agent create" in capsys.readouterr().out


# --- joining as one of them ---------------------------------------------------

def join_args(**kw):
    kw.setdefault("home", "")
    kw.setdefault("agent", "")
    kw.setdefault("name", "")
    return argparse.Namespace(**kw)


def test_join_asks_when_there_is_more_than_one(repo, monkeypatch):
    """Which agent joins decides the name, the colour and the id that everyone
    else in the session sees — a worse thing to guess than a local setting."""
    agent("create", "alice")
    agent("create", "bob")
    a_terminal(monkeypatch, "2")
    got = cli._which_agent_to_join(join_args())
    assert got == repo / ".collab-bob"


def test_join_refuses_without_a_person(repo, monkeypatch, capsys):
    agent("create", "alice")
    agent("create", "bob")
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args()) is False
    assert "--agent" in capsys.readouterr().out


def test_join_with_one_agent_does_not_ask(repo, monkeypatch):
    agent("create", "alice")
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args()) == repo / ".collab-alice"


def test_join_with_no_agents_carries_on(repo, monkeypatch):
    """Nothing created here means nothing to choose between."""
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args()) is None


def test_the_agent_flag_is_an_answer(repo, monkeypatch):
    agent("create", "alice")
    agent("create", "bob")
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args(agent="bob")) == \
        repo / ".collab-bob"


def test_an_explicit_name_is_an_answer_too(repo, monkeypatch):
    agent("create", "alice")
    agent("create", "bob")
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args(name="someone")) is None


def test_join_refuses_an_agent_that_does_not_exist(repo, monkeypatch, capsys):
    agent("create", "alice")
    no_terminal(monkeypatch)
    assert cli._which_agent_to_join(join_args(agent="ghost")) is False
    assert "collab agent create" in capsys.readouterr().out


def test_cancelling_the_choice_stops_the_join(repo, monkeypatch):
    agent("create", "alice")
    agent("create", "bob")
    a_terminal(monkeypatch, "")
    assert cli._which_agent_to_join(join_args()) is False
