"""An agent's default name says what kind of agent it is, and nothing about anybody.

Since agent settings (R21) a machine-wide `display_name` names no agent, and an
agent that had relied on it fell through to a name derived from the PERSON —
git's `user.name`, then `$USER`. A host printed «starting as rafael-perez»; in a
remote session that publishes the human's real name to every peer. These tests
hold the two halves of the fix: a legacy machine name is adopted once into the
agent's own layer, and the fallback is the agent kind and a stable suffix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from collab import config


@pytest.fixture
def person(tmp_path, monkeypatch):
    """A machine where everything a person-derived name could come from says
    «rafael-perez», and this agent has its own home."""
    monkeypatch.setenv("USER", "rafael-perez")
    monkeypatch.setenv("USERNAME", "rafael-perez")
    git = tmp_path / "gitconfig"
    git.write_text("[user]\n\tname = Rafael Perez\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git))
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.setenv("COLLAB_AGENT_KIND", "claude-code")
    home = tmp_path / "agent-home" / ".collab"
    home.mkdir(parents=True)
    monkeypatch.setenv("COLLAB_HOME", str(home))
    monkeypatch.setattr(config, "_agent_name", lambda: "")
    config._AGENT_CACHE.clear()
    config._AGENT_PATH.clear()
    config._CACHE.clear()
    return home


def _machine(values: dict) -> None:
    path = config.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))
    config._CACHE.clear()


def test_the_default_is_the_agent_kind_and_a_suffix_never_the_person(person):
    """With git's user.name and $USER both saying who the human is, the name
    an agent with no name of its own publishes says neither."""
    name = config.resolve_name()
    assert re.fullmatch(r"claude-[0-9a-f]{4}", name), name
    for leak in ("rafael", "perez"):
        assert leak not in name
    assert config._name_fallback() == name, "the settings listing shows the same default"


def test_the_default_is_stable_for_one_home_and_differs_for_another(person, tmp_path,
                                                                    monkeypatch):
    """An agent keeps its name across calls — and across processes, which is
    what a restart or a resume is — and two homes get two names."""
    first = config.resolve_name()
    assert config.resolve_name() == first
    probe = ("from collab import config\n"
             "config._agent_name = lambda: ''\n"
             "print(config.resolve_name())\n")
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(config.__file__))}
    again = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           env=env, timeout=60).stdout.strip()
    assert again == first, "a new process — a restart — answers to the same name"
    other = tmp_path / "second-agent" / ".collab"
    other.mkdir(parents=True)
    monkeypatch.setenv("COLLAB_HOME", str(other))
    config._AGENT_PATH.clear()
    config._AGENT_CACHE.clear()
    assert config.resolve_name() != first


def test_an_unknown_kind_is_called_an_agent_not_a_person(person, monkeypatch):
    monkeypatch.setenv("COLLAB_AGENT_KIND", "")
    monkeypatch.setattr("collab.hosttool.detect", lambda: "")
    name = config.resolve_name()
    assert re.fullmatch(r"agent-[0-9a-f]{4}", name), name


def test_a_legacy_machine_name_is_adopted_once_into_the_agents_own_layer(person, capsys):
    """An agent with no name of its own takes the machine file's old shared
    name — the person chose that name to be seen by — into ITS layer, so it
    stops falling through to the default, and the CLI says so exactly once."""
    _machine({"display_name": "friday"})
    assert config.setting_source("display_name") == "legacy"
    assert config.resolve_name() == "friday"
    assert config.setting_source("display_name") == "agent"
    assert json.loads(config.agent_config_path().read_text())["display_name"] == "friday"
    from collab import cli

    cli._name_source_note()
    said = capsys.readouterr().out
    assert "friday" in said and "as its own" in said
    cli._name_source_note()
    assert capsys.readouterr().out == "", "said once"
    assert config.resolve_name() == "friday", "and kept"


def test_an_agent_with_its_own_name_never_takes_the_legacy_one(person):
    _machine({"display_name": "friday"})
    config.setting("display_name").write("cortana")
    assert config.resolve_name() == "cortana"
    assert not config.agent_config_path().with_name(config.LEGACY_NAME_ADOPTED).exists()


def test_an_explicit_name_wins_and_adopts_nothing(person):
    _machine({"display_name": "friday"})
    assert config.resolve_name("jarvis") == "jarvis"
    assert config.setting_source("display_name") == "legacy", "nothing was adopted"
