"""Viewer layout: a preference the user keeps, and tmux owning the split."""

from __future__ import annotations

import pytest

from collab import config
from collab.client import watch


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))


def test_defaults_when_nothing_is_saved():
    settings = config.watch_settings()
    assert settings["layout"] == "split"
    assert settings["roster_size"] == 30
    assert settings["roster_position"] == "top"


def test_a_layout_is_remembered():
    config.save_watch_settings(layout="tmux", roster_size=45,
                               roster_position="left")
    settings = config.watch_settings()
    assert settings == {"layout": "tmux", "roster_size": 45,
                        "roster_position": "left"}


def test_settings_are_saved_independently():
    config.save_watch_settings(layout="chat")
    config.save_watch_settings(roster_size=50)
    settings = config.watch_settings()
    assert settings["layout"] == "chat", "saving one must not reset the others"
    assert settings["roster_size"] == 50


def test_nonsense_saved_values_fall_back(tmp_path):
    config.save_config({"watch_layout": "sideways", "watch_roster_size": "huge",
                        "watch_roster_position": "diagonally"})
    settings = config.watch_settings()
    assert settings["layout"] == "split"
    assert settings["roster_size"] == 30
    assert settings["roster_position"] == "top"


@pytest.mark.parametrize("size,expected", [(1, 5), (200, 90), (40, 40)])
def test_roster_size_is_kept_usable(size, expected):
    """A 1% or 200% pane is not a layout anyone wants."""
    config.save_watch_settings(roster_size=size)
    assert config.watch_settings()["roster_size"] == expected


@pytest.mark.parametrize("position,direction,before", [
    ("top", "-v", True),
    ("bottom", "-v", False),
    ("left", "-h", True),
    ("right", "-h", False),
])
def test_positions_map_to_tmux_splits(position, direction, before):
    assert watch.POSITIONS[position] == (direction, before)


def test_opening_a_pane_outside_tmux_is_refused(monkeypatch):
    """Better a clear message than a split that silently never appears."""
    monkeypatch.setattr(watch, "tmux_available", lambda: True)
    monkeypatch.setattr(watch, "in_tmux", lambda: False)
    with pytest.raises(RuntimeError, match="could not identify this terminal's tmux pane"):
        watch.open_tmux_pane(["collab", "watch"])


def test_the_pane_command_carries_the_environment(monkeypatch):
    """A new tmux pane inherits the server's environment, not this shell's."""
    seen: dict[str, list[str]] = {}

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(watch, "tmux_available", lambda: True)
    monkeypatch.setattr(watch, "in_tmux", lambda: True)
    monkeypatch.setattr(watch, "_tmux_command", fake_run)

    watch.open_tmux_pane(["collab", "watch"], env={"COLLAB_HOME": "/repo/.collab"},
                         percent=40, position="left")

    command = seen["argv"][-1]
    assert "COLLAB_HOME=/repo/.collab" in command
    assert "-h" in seen["argv"] and "-b" in seen["argv"]
    assert "40%" in " ".join(seen["argv"])


def test_saving_a_layout_does_not_need_an_active_session(tmp_path, monkeypatch,
                                                         capsys):
    """The layout is a global preference about you.

    Needing to be in a session before you can record one is backwards, and it
    silently did nothing when the docs said it would work.
    """
    from collab import cli

    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))

    assert cli.main(["watch", "--layout", "tmux", "--roster-size", "45",
                     "--roster-position", "left", "--save"]) == 0
    assert "saved" in capsys.readouterr().out
    assert config.watch_settings() == {"layout": "tmux", "roster_size": 45,
                                       "roster_position": "left"}
