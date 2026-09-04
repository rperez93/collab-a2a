"""Changing a name or a colour asks which agent it belongs to.

`resolve_home` picks a directory when nothing says which one: if the shared
`.collab` is held by somebody else and exactly one sibling is live, it returns
that sibling. Harmless while it decided where to *read* session state; not
harmless now that it decides where to *write* an identity, because the same
guess means ana runs `collab color` and repaints bob.

A guess is the wrong shape here. There is a person at the keyboard, the options
are three lines long, and getting it wrong silently edits somebody else's
settings.
"""
from __future__ import annotations

import argparse
import json

import pytest

from collab import cli, config, identity, peers


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    for d in (".collab", ".collab-alice", ".collab-bob"):
        (tmp_path / d).mkdir()
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


def run(fn, **kw):
    kw.setdefault("agent", "")
    return fn(argparse.Namespace(**kw))


def no_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)


def a_terminal(monkeypatch, answer):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: answer)


# --- with nobody to ask, it refuses ------------------------------------------

def test_a_script_is_told_to_say_which_one(repo, monkeypatch, capsys):
    """Refusing is the point.

    A script has nobody to ask, and picking for it is exactly the failure being
    fixed. It says what to pass and stops.
    """
    no_terminal(monkeypatch)
    assert run(cli.cmd_color, value="#008080") == 2
    out = capsys.readouterr().out
    assert "--agent" in out
    assert not (repo / ".collab-alice" / identity.IDENTITY_FILE).exists()
    assert not (repo / ".collab-bob" / identity.IDENTITY_FILE).exists()


def test_refusing_lists_the_options(repo, monkeypatch, capsys):
    """Saying no without saying what to do instead just moves the problem."""
    no_terminal(monkeypatch)
    run(cli.cmd_color, value="#008080")
    out = capsys.readouterr().out
    assert ".collab-alice" in out and ".collab-bob" in out


def test_the_name_command_refuses_the_same_way(repo, monkeypatch):
    no_terminal(monkeypatch)
    assert run(cli.cmd_name, value="bob", session=None) == 2
    assert not (repo / ".collab-alice" / identity.IDENTITY_FILE).exists()


# --- explicit answers are taken as given -------------------------------------

def test_agent_flag_writes_where_it_says(repo, monkeypatch):
    no_terminal(monkeypatch)
    assert run(cli.cmd_color, value="#ff7f50", agent="bob") == 0
    assert identity.load(repo / ".collab-bob")["color"] == "#ff7f50"
    assert identity.load(repo / ".collab-alice") == {}


def test_collab_home_needs_no_asking(repo, monkeypatch):
    """An explicit environment variable is already an answer."""
    no_terminal(monkeypatch)
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-alice"))
    config._HOME_CACHE.clear()
    assert run(cli.cmd_color, value="#008080") == 0
    assert identity.load(repo / ".collab-alice")["color"] == "#008080"


def test_an_unknown_agent_name_is_refused(repo, monkeypatch, capsys):
    no_terminal(monkeypatch)
    assert run(cli.cmd_color, value="#008080", agent="nobody") == 2
    # fail() writes to stderr; the listing that follows goes to stdout.
    got = capsys.readouterr()
    assert "nobody" in got.err
    assert ".collab-bob" in got.out


def test_one_directory_is_not_a_choice(tmp_path, monkeypatch):
    """Asking when there is a single option is noise, not care."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".collab-solo").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    config._HOME_CACHE.clear()
    no_terminal(monkeypatch)
    assert run(cli.cmd_color, value="#008080") == 0
    assert identity.load(tmp_path / ".collab-solo")["color"] == "#008080"


# --- with a person there, it asks --------------------------------------------

def test_a_person_picks_from_the_list(repo, monkeypatch):
    a_terminal(monkeypatch, "3")           # .collab, -alice, -bob
    assert run(cli.cmd_color, value="#ff7f50") == 0
    assert identity.load(repo / ".collab-bob")["color"] == "#ff7f50"
    assert identity.load(repo / ".collab-alice") == {}


def test_anything_else_cancels_and_changes_nothing(repo, monkeypatch):
    a_terminal(monkeypatch, "")
    assert run(cli.cmd_color, value="#ff7f50") == 2
    assert identity.load(repo / ".collab-bob") == {}


def test_a_number_out_of_range_cancels(repo, monkeypatch):
    a_terminal(monkeypatch, "9")
    assert run(cli.cmd_color, value="#ff7f50") == 2
    assert identity.load(repo / ".collab-bob") == {}


# --- clearing is a change like any other -------------------------------------

def test_clearing_the_colour_asks_too(repo, monkeypatch):
    """It used to write to the machine's config while setting wrote to the
    agent's file, so `collab color none` said ok and the colour stayed."""
    identity.save(repo / ".collab-bob", name="bob", color="#ff7f50")
    a_terminal(monkeypatch, "3")
    assert run(cli.cmd_color, value="none") == 0
    assert "color" not in identity.load(repo / ".collab-bob")


# --- writing into another agent's file must not rename them ------------------

def test_setting_another_agents_colour_keeps_their_name(repo, monkeypatch):
    """`resolve_name()` answers for whoever holds the terminal.

    Using it here meant setting bob's colour also renamed bob to alice.
    """
    monkeypatch.setattr(cli, "resolve_name", lambda *a, **k: "alice")
    no_terminal(monkeypatch)
    run(cli.cmd_color, value="#ff7f50", agent="bob")
    assert identity.load(repo / ".collab-bob")["name"] == "bob"


def test_an_existing_name_is_not_overwritten(repo, monkeypatch):
    identity.save(repo / ".collab-bob", name="B.O.B")
    monkeypatch.setattr(cli, "resolve_name", lambda *a, **k: "alice")
    no_terminal(monkeypatch)
    run(cli.cmd_color, value="#ff7f50", agent="bob")
    assert identity.load(repo / ".collab-bob")["name"] == "B.O.B"
