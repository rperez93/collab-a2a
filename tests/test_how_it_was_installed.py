"""Upgrading by the route collab actually arrived through.

There are two supported ways in — a clone with `install.sh`, and a wheel from
PyPI — and they upgrade by different commands. Running the wrong one does not
fail loudly, which is the whole problem: `git pull` outside a checkout reports
nothing to pull, and `pip install --upgrade` from inside a clone upgrades the
installed copy while the clone being edited stays where it was. Either way
somebody is left certain they are on a version they are not, which is the fault
this module exists to prevent rather than to commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from collab import update


@pytest.fixture()
def not_a_checkout(monkeypatch):
    """Take the clone away, so the wheel paths can be reached at all."""
    monkeypatch.setattr(update, "repo_dir", lambda: None)


def _prefix(monkeypatch, path: str) -> None:
    monkeypatch.setattr(sys, "prefix", path)


def test_a_clone_is_updated_by_pulling_it(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "repo_dir", lambda: tmp_path)
    how = update.installed_as()
    assert how.kind == "checkout"
    assert how.where == tmp_path
    assert "git" in how.command and "./install.sh" in how.command


def test_a_wheel_is_updated_by_pip_not_by_git(not_a_checkout, monkeypatch):
    """The case that made this necessary: an install from PyPI was being told
    to `cd` into a directory it does not have and pull a repository it never
    cloned."""
    _prefix(monkeypatch, "/home/someone/.venvs/work")
    monkeypatch.setattr(update, "_is_installed", lambda: True)
    how = update.installed_as()
    assert how.kind == "pip"
    assert how.command[:2] == [sys.executable, "-m"]
    assert "--upgrade" in how.command and update.PACKAGE in how.command
    assert "git" not in how.command


def test_pipx_is_upgraded_by_pipx(not_a_checkout, monkeypatch):
    """pip inside a pipx environment works once and is undone the next time
    pipx touches it, so the tool that owns the environment is the one to ask."""
    _prefix(monkeypatch, "/home/someone/.local/pipx/venvs/collab-a2a")
    how = update.installed_as()
    assert how.kind == "pipx"
    assert how.command == ["pipx", "upgrade", update.PACKAGE]


def test_a_uv_tool_is_upgraded_by_uv(not_a_checkout, monkeypatch):
    _prefix(monkeypatch, "/home/someone/.local/share/uv/tools/collab-a2a")
    how = update.installed_as()
    assert how.kind == "uv"
    assert how.command == ["uv", "tool", "upgrade", update.PACKAGE]


def test_a_venv_uv_merely_created_is_still_pip(not_a_checkout, monkeypatch):
    """`uv venv` produces an ordinary environment that pip upgrades perfectly
    well. Only a uv *tool* environment has its own verb, and the difference is
    the `tools` directory rather than the word `uv` appearing in a path."""
    _prefix(monkeypatch, "/home/someone/projects/uv/.venv")
    monkeypatch.setattr(update, "_is_installed", lambda: True)
    assert update.installed_as().kind == "pip"


def test_a_loose_tree_admits_it_cannot_update(not_a_checkout, monkeypatch):
    """Neither a clone nor an install — a PYTHONPATH, a vendored copy. There is
    no upgrade command that could be right, and inventing one would overwrite
    something somebody arranged deliberately."""
    _prefix(monkeypatch, "/home/someone/scratch")
    monkeypatch.setattr(update, "_is_installed", lambda: False)
    how = update.installed_as()
    assert how.kind == "unknown"
    assert not how.can_apply

    ok, message = update.apply_update()
    assert ok is False
    assert update.PACKAGE in message and update.REPO_URL in message


def test_a_package_upgrades_itself_without_asking(
        not_a_checkout, monkeypatch, capsys):
    """A wheel replaces only files it owns and re-running the command changes
    nothing, so stopping to ask buys the user the chance to decline something
    they will have to do anyway."""
    _prefix(monkeypatch, "/home/someone/.venvs/work")
    monkeypatch.setattr(update, "_is_installed", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ran = []
    monkeypatch.setattr(update, "apply_update",
                        lambda: (ran.append(True), (True, ""))[1])
    info = update.UpdateInfo(current="1.0.0", latest="1.1.0", available=True)

    assert update.prompt_and_maybe_update(info) is True
    assert ran, "it told the user to run a command instead of running it"
    assert "updated to 1.1.0" in capsys.readouterr().out


def test_a_clone_is_still_asked_rather_than_pulled(
        tmp_path, monkeypatch, capsys):
    """A working copy may hold edits, a branch, a rebase halfway through.
    Pulling into that is the user's decision, not a side effect of starting a
    session — so the clone keeps the behaviour it has always had."""
    monkeypatch.setattr(update, "repo_dir", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(update, "apply_update",
                        lambda: pytest.fail("it pulled somebody's checkout"))
    info = update.UpdateInfo(current="1.0.0", latest="1.1.0", available=True)

    assert update.prompt_and_maybe_update(info) is False
    out = capsys.readouterr().out
    assert "git pull" in out and str(tmp_path) in out


def test_a_failed_upgrade_does_not_stop_the_session(
        not_a_checkout, monkeypatch, capsys):
    """The session the user actually asked for runs perfectly well on the
    version already installed."""
    _prefix(monkeypatch, "/home/someone/.local/pipx/venvs/collab-a2a")
    monkeypatch.setattr(update, "apply_update",
                        lambda: (False, "network unreachable"))
    info = update.UpdateInfo(current="1.0.0", latest="1.1.0", available=True)

    assert update.prompt_and_maybe_update(info) is False
    assert "carrying on" in capsys.readouterr().out


def test_a_failing_upgrade_passes_its_own_message_through(
        not_a_checkout, monkeypatch):
    """PEP 668 refuses on a system Python and names the flag or the virtual
    environment that would fix it. Paraphrasing a policy we do not set would
    leave the reader without the one sentence that helps."""
    _prefix(monkeypatch, "/usr")
    monkeypatch.setattr(update, "_is_installed", lambda: True)

    class Refused:
        returncode = 1
        stdout = ""
        stderr = "error: externally-managed-environment\nUse a virtual environment"

    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: Refused())
    ok, message = update.apply_update()
    assert ok is False
    assert "externally-managed-environment" in message
