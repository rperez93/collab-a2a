"""The rules of the room are handed out at the door, not sent on request.

Agents left to work out for themselves how to collaborate do it badly: they
argue in rounds, paste files into messages, chase side-findings, and leave the
board stale. A written set of rules fixes most of that — but only if every
agent reads it, and a host that has to remember to send a file is a host that
forgets. So collab prints the rules itself, at `host` and at `join`, the same
moment it tells the agent to arm its monitor: both are things an agent decides
about once, on arrival.

Two parts. The shipped rules, which the user may switch off; and a pointer to
the repository's own `COLLAB.md`, which has no switch, because a repository's
rules are the repository's to make and every agent in it is bound by them.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.resources
import io
import json
import os
import re
import shlex
from pathlib import Path

import pytest

from collab import cli, config, rules
from collab.cli import build_parser, main

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "src" / "collab" / "rules" / "COLLAB.md"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A scratch config, peers directory and working directory."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    config._CACHE.clear()
    yield tmp_path
    config._CACHE.clear()


def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


# --- the file ships ------------------------------------------------------------

def test_the_rules_ship_inside_the_package():
    """Readable the way an installed wheel would read them, not by a path
    relative to a checkout."""
    text = importlib.resources.files("collab").joinpath("rules/COLLAB.md").read_text()
    assert text == rules.default_rules()
    assert text.lstrip().startswith("# COLLAB.md")
    assert "## Checklist" in text


def test_the_package_data_glob_carries_them():
    """The skills learned this the hard way: a file that is in the checkout and
    not in the wheel is one every installed copy lacks."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    package_data = pyproject.split("[tool.setuptools.package-data]", 1)[1]
    package_data = package_data.split("\n[", 1)[0]
    assert '"rules/*.md"' in package_data


def _fenced_commands(text: str) -> list[str]:
    """Every `collab …` line inside a code fence, comments stripped."""
    found = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced and line.strip().startswith("collab "):
            found.append(line.split("  #", 1)[0].strip())
    return found


def test_every_command_the_rules_cite_parses():
    """The rules are followed literally by an agent that will not open the
    source to check, so a command that is not one is a promise that fails in
    front of the user."""
    parser = build_parser()
    cited = _fenced_commands(rules.default_rules())
    assert len(cited) >= 10, "the fences went missing, or the scan did"
    for command in cited:
        argv = shlex.split(command)[1:]
        try:
            parser.parse_args(argv)
        except SystemExit:
            pytest.fail(f"the rules cite a command collab does not accept: {command}")


# --- what is printed ----------------------------------------------------------

def test_the_briefing_carries_the_shipped_rules_and_the_local_pointer():
    text = cli._rules_briefing()
    assert cli.RULES_HEADING in text
    assert rules.default_rules().strip() in text
    assert "COLLAB.md" in text
    assert "directory you are working in" in text


def test_turning_the_setting_off_drops_the_shipped_rules_and_nothing_else():
    assert _run("config", "rules", "off")[0] == 0
    text = cli._rules_briefing()
    assert cli.RULES_HEADING not in text
    assert "## Checklist" not in text
    assert "COLLAB.md" in text, "the local pointer has no switch"
    assert "directory you are working in" in text


def test_the_local_pointer_says_when_the_file_is_there_and_names_it(isolated):
    without = cli._rules_briefing()
    assert str(isolated / "COLLAB.md") not in without
    assert "COLLAB.md" in without, "asked to read it even when it does not exist yet"

    (isolated / "COLLAB.md").write_text("# ours\n\nno force-pushes\n")
    with_it = cli._rules_briefing()
    assert str(isolated / "COLLAB.md") in with_it


def test_the_local_pointer_asks_for_it_to_be_appended_to():
    text = cli._rules_briefing()
    assert re.search(r"append", text, re.I)
    assert re.search(r"binding", text, re.I)


# --- the command ----------------------------------------------------------------

def test_rules_prints_what_host_and_join_print():
    code, out = _run("rules")
    assert code == 0
    assert out.strip() == cli._rules_briefing().strip()


def test_rules_default_is_the_shipped_file_verbatim():
    """So `collab rules --default > COLLAB.md` seeds a repository with nothing
    else in it — no heading, no colour, no pointer."""
    code, out = _run("rules", "--default")
    assert code == 0
    assert out == rules.default_rules()
    assert out == SHIPPED.read_text()


def test_rules_default_ignores_the_setting():
    _run("config", "rules", "off")
    assert _run("rules", "--default")[1] == rules.default_rules()


# --- the setting -----------------------------------------------------------------

def test_the_setting_round_trips_through_config(isolated):
    assert config.rules_enabled() is True
    assert _run("config", "rules", "off")[0] == 0
    assert config.rules_enabled() is False
    assert json.loads((isolated / "config.json").read_text())["rules"] is False
    assert _run("config", "rules", "on")[0] == 0
    assert config.rules_enabled() is True
    assert _run("config", "rules", "--unset")[0] == 0
    assert config.rules_enabled() is True
    assert "rules" not in json.loads((isolated / "config.json").read_text())


def test_a_value_that_is_not_one_is_refused(isolated):
    assert _run("config", "rules", "maybe")[0] == 2
    assert not (isolated / "config.json").exists() \
        or "rules" not in json.loads((isolated / "config.json").read_text())


@pytest.mark.parametrize("raw", ['"maybe"', "Infinity", "null", "[]", "0", '""'])
def test_a_hostile_value_in_the_file_does_not_raise(isolated, raw):
    """Every reader validates on the way out and never raises: a hand-edited
    file must cost one setting, not every command."""
    (isolated / "config.json").write_text('{"rules": ' + raw + "}")
    config._CACHE.clear()
    assert config.rules_enabled() in (True, False)
    cli._rules_briefing()


# --- host and join --------------------------------------------------------------

def _join(**flags) -> str:
    fields = {"url": None, "local": False, "name": None, "focus": "",
              "agent": None, "home": None, "no_daemon": True,
              "no_update_check": True, "update": False, "session": None}
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_join(argparse.Namespace(**{**fields, **flags}))
    assert code == 0, out.getvalue()
    return out.getvalue()


def test_join_prints_the_rules_after_the_monitor_hint(live_server, isolated, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(isolated / "guest-home"))
    (isolated / "COLLAB.md").write_text("# house rules\n")

    out = _join(url=f"{live_server['base']}#{live_server['invite']}", name="bob")

    assert "Arm your monitor" in out
    assert cli.RULES_HEADING in out
    assert str(isolated / "COLLAB.md") in out
    assert out.index("Arm your monitor") < out.index(cli.RULES_HEADING), \
        "the watcher first — it is the thing that must be armed now"
    assert out.index(cli.RULES_HEADING) < out.index(str(isolated / "COLLAB.md")), \
        "the repository's rules sit on top of the shipped ones, so they come after"


def test_join_with_the_setting_off_still_points_at_the_local_file(live_server, isolated,
                                                                  monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(isolated / "guest-home"))
    _run("config", "rules", "off")

    out = _join(url=f"{live_server['base']}#{live_server['invite']}", name="bob")

    assert cli.RULES_HEADING not in out
    assert "COLLAB.md" in out


def test_host_prints_the_rules_after_the_monitor_hint(isolated, monkeypatch):
    """A real hub, started the way `collab host` starts it, and stopped by the
    pid it recorded — never by matching a command line."""
    from collab.server.session import HubConfig, hosted_sessions, stop_session

    home = isolated / "host-home"
    monkeypatch.setenv("COLLAB_HOME", str(home))
    (isolated / ".git").mkdir()
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: isolated)

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["host", "--fresh", "--no-tunnel", "--no-daemon",
                         "--no-update-check", "--name", "alice"])
    finally:
        for cfg in hosted_sessions(home):
            latest = HubConfig.load(cfg.session_id, cfg.home) or cfg
            stop_session(latest)
    text = out.getvalue()
    assert code == 0, text
    assert "Arm your monitor" in text
    assert cli.RULES_HEADING in text
    assert "COLLAB.md" in text
    assert text.index("Arm your monitor") < text.index(cli.RULES_HEADING)
