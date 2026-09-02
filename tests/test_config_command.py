"""`collab config` — one place to see and set what collab remembers about you.

The settings arrived one at a time, each with the command that motivated it,
and there were nine ways to change something and no way to see what there was.
Somebody who had set a `stats_command` months earlier had nothing that would
remind them.

The command is an index, not a second owner of the settings: everything it
writes goes through the setter that already existed, because that is where the
validation lives and one of them clears a cache. These tests are mostly about
that — that the two routes to a setting cannot disagree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from collab import cli, config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    config._CACHE.clear()
    yield tmp_path / "config.json"
    config._CACHE.clear()


def _run(*argv):
    return cli.main(["config", *argv])


# --- seeing them -------------------------------------------------------------

def test_it_lists_every_setting_with_its_value_and_its_default(capsys):
    assert _run() == 0
    out = capsys.readouterr().out
    for item in config.settings():
        assert item.name in out, f"{item.name} is not in the listing"
        assert item.about in out, f"{item.name} has no line saying what it is for"


def test_a_setting_that_has_been_changed_says_what_it_would_have_been(capsys):
    config.set_share_stats(False)
    _run()
    out = capsys.readouterr().out
    assert re.search(r"share_stats\s+off\s+\(default on\)", out)


def test_one_setting_prints_bare_so_it_can_be_read_by_a_script(capsys):
    assert _run("theme") == 0
    assert capsys.readouterr().out.splitlines()[0] == config.theme()


def test_the_json_form_carries_the_default_and_the_description(capsys):
    assert _run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {item.name for item in config.settings()}
    assert data["watch_status"] == {"value": True, "default": True,
                                    "about": data["watch_status"]["about"]}


# --- changing them -----------------------------------------------------------

def test_it_writes_the_key_the_older_command_reads(isolated):
    """Two routes to one setting that wrote two keys would be worse than one."""
    assert _run("share_stats", "off") == 0
    assert config.share_stats_enabled() is False
    assert json.loads(isolated.read_text())["share_stats"] is False


def test_a_value_the_setter_refuses_is_not_written(isolated, capsys):
    """`set_theme` answers None for a theme nobody has installed. Writing it
    anyway and falling back at read time leaves somebody looking at the wrong
    theme convinced theirs is on."""
    assert _run("theme", "no-such-theme") == 2
    assert "no theme by that name" in capsys.readouterr().err
    assert not isolated.exists() or "theme" not in json.loads(isolated.read_text())


@pytest.mark.parametrize("key,value", [
    ("share_stats", "maybe"),
    ("stats_interval", "soon"),
    ("watch_layout", "sideways"),
    ("watch_roster_position", "diagonal"),
    ("watch_status", "sometimes"),
    ("watch_status_segments", "batch,nonsense"),
    ("color", "burgundy"),
])
def test_a_value_that_is_not_one_is_refused_rather_than_stored(key, value, isolated):
    assert _run(key, value) == 2
    assert not isolated.exists() or key not in json.loads(isolated.read_text())


def test_an_unknown_key_is_refused_and_the_real_ones_are_named(capsys):
    assert _run("watch_stauts", "off") == 2
    said = capsys.readouterr()
    assert "no setting called" in said.err
    assert "watch_status" in said.out, "and it says what the real ones are"


def test_unsetting_puts_a_setting_back_to_its_default(isolated):
    _run("watch_status", "off")
    assert config.watch_status_settings()["enabled"] is False
    assert _run("watch_status", "--unset") == 0
    assert config.watch_status_settings()["enabled"] is True
    assert "watch_status" not in json.loads(isolated.read_text())


def test_unsetting_something_never_set_is_not_an_error():
    assert _run("theme", "--unset") == 0


def test_a_value_and_an_unset_together_are_refused(capsys):
    """Two instructions in one command, and no way to tell which was meant."""
    assert _run("share_stats", "off", "--unset") == 2
    assert "takes no value" in capsys.readouterr().err


def test_on_and_off_are_what_a_person_types(isolated):
    for text, stored in (("on", True), ("yes", True), ("1", True),
                         ("off", False), ("no", False), ("0", False)):
        assert _run("share_stats", text) == 0
        assert json.loads(isolated.read_text())["share_stats"] is stored


def test_segments_are_taken_with_commas_or_with_spaces(isolated):
    assert _run("watch_status_segments", "batch,keys") == 0
    assert config.watch_status_settings()["segments"] == ("batch", "keys")
    assert _run("watch_status_segments", "keys batch") == 0
    assert config.watch_status_settings()["segments"] == ("keys", "batch")


# --- the registry and the file cannot drift apart ----------------------------

def test_every_setting_can_be_read_with_nothing_configured():
    """Each reader is a lambda in a table, and a table is where a typo hides:
    the listing is the only thing that calls most of them."""
    for item in config.settings():
        item.read()


def test_every_key_collab_writes_is_declared_here():
    """The point of the command is that it is complete.

    A setting added later with its own writer and no entry in the registry is
    invisible again — which is the state this command exists to end — and
    nothing else would notice.
    """
    source = Path(config.__file__).read_text()
    written = set(re.findall(r"""cfg\[["'](\w+)["']\]\s*=""", source))
    declared = {item.name for item in config.settings()}
    assert written <= declared, f"written but not in `collab config`: {written - declared}"


def test_the_defaults_shown_are_the_defaults_that_apply():
    """A listing whose «default» column is decoration is worse than no column.

    Every default is checked against what the reader answers with an empty
    config, which is the only thing the word can mean.
    """
    config._CACHE.clear()
    for item in config.settings():
        assert item.read() == item.default, item.name
