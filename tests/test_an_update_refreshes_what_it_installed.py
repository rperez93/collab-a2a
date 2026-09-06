"""An upgrade replaces the package and nothing it wrote into other people's files.

The skills are copies in each agent's own directory and the status line is a
snippet in somebody's settings. `pip install --upgrade` touches neither, so a
release that changes what a skill says — or what the status line invokes — ships
the change to the package and leaves every machine still running the old text.

Three rules, and every one of them was got wrong first:

* refresh only what is INSTALLED, which is not the same question as what is
  DETECTED — `auto` means every host present on the machine;
* refresh the skills with `--force`, because the installs that can go stale are
  exactly the ones the default refuses to touch;
* refresh by spawning the new executable, since this process is still holding
  the version it started with.
"""

from __future__ import annotations

import os

import pytest

from collab import skills, update  # noqa: F401


class _Ran:
    """A runner that records instead of running. Nothing here touches the machine."""

    def __init__(self, code: int = 0):
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv):
        self.calls.append(list(argv))
        return type("Done", (), {"returncode": self.code})()


@pytest.fixture
def targets(monkeypatch):
    """Say what is installed, one target at a time."""
    state = {"skills": {}, "claude-code": False, "tmux": False}

    monkeypatch.setattr("collab.skills.status",
                        lambda **kw: {"agents": state["skills"]})
    monkeypatch.setattr("collab.statusline.install.status_claude_code",
                        lambda *a, **k: {"installed": state["claude-code"]})
    monkeypatch.setattr("collab.statusline.install.status_tmux",
                        lambda *a, **k: {"installed": state["tmux"]})
    monkeypatch.setattr(update, "collab_executable", lambda: "/opt/bin/collab")
    return state


def test_it_installs_nothing_that_was_not_there(targets):
    """An agent with no skills installed is somebody who chose not to.

    An update is not the moment to overrule them, and this is the assertion
    that keeps a future «while we are here» from doing so.
    """
    ran = _Ran()
    assert update.refresh_installed(runner=ran) == []
    assert ran.calls == []


def test_a_detected_host_is_not_an_installed_host(targets):
    """The defect this test exists for: `auto` widens to what is merely present.

    Status line in Claude Code, tmux installed on the machine but never wired
    up — running `statusline install` with no arguments would write a collab
    block into `~/.tmux.conf` that nobody asked for.
    """
    targets["claude-code"] = True
    targets["tmux"] = False
    ran = _Ran()

    update.refresh_installed(runner=ran)

    assert ran.calls == [["/opt/bin/collab", "statusline", "install",
                          "--agent", "claude-code"]]
    assert not any("tmux" in " ".join(call) for call in ran.calls)


def test_each_agent_with_skills_is_named_rather_than_swept_up(targets):
    targets["skills"] = {"claude-code": {"installed": True},
                         "codex": {"installed": False},
                         "goose": {"installed": True}}
    ran = _Ran()

    notes = update.refresh_installed(runner=ran)

    named = [call[call.index("--agent") + 1] for call in ran.calls]
    assert named == ["claude-code", "goose"]
    assert "codex" not in " ".join(notes)


def test_the_skills_are_refreshed_with_force(targets):
    """Without it this fixes nothing for the installs that need fixing.

    `skills install` refuses to replace a directory it did not symlink itself,
    and a COPIED install is exactly such a directory — while a symlink was
    never out of date in the first place. The default therefore skips the only
    case the refresh exists for.
    """
    targets["skills"] = {"claude-code": {"installed": True}}
    ran = _Ran()

    update.refresh_installed(runner=ran)

    assert "--force" in ran.calls[0]


def test_this_sessions_collab_home_is_not_baked_into_a_global_hook(targets,
                                                                   monkeypatch):
    """`statusline install` stamps a COLLAB_HOME it finds into the hook.

    During `collab host` that variable names one session and the hook is
    machine-wide, so inheriting it would write a session-specific home into a
    global file as a side effect of an update.
    """
    targets["claude-code"] = True
    monkeypatch.setenv("COLLAB_HOME", "/repo/.collab-bob")
    seen = {}

    def runner(argv, **kw):
        return type("Done", (), {"returncode": 0})()

    # The real runner is the one that builds the environment, so exercise it.
    def fake_run(argv, **kw):
        seen.update(kw.get("env") or {})
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update.refresh_installed()

    assert "COLLAB_HOME" not in seen
    assert "PATH" in seen, "the rest of the environment is still passed through"


def test_a_refresh_that_fails_is_reported_and_not_raised(targets):
    """The upgrade has already succeeded by here; this must not undo that."""
    targets["claude-code"] = True

    def explode(argv):
        raise OSError("no such file")

    assert update.refresh_installed(runner=explode) == [
        "status line for claude-code not refreshed (OSError)"]


def test_a_non_zero_exit_is_reported_rather_than_called_success(targets):
    targets["tmux"] = True
    ran = _Ran(code=2)

    assert update.refresh_installed(runner=ran) == [
        "status line for tmux not refreshed (exit 2)"]


def test_it_spawns_the_executable_rather_than_calling_in_process(targets,
                                                                 monkeypatch):
    """The running interpreter still holds the version it started with.

    Calling the installers here would carefully write the OLD skills out again
    and report success — the exact failure the refresh exists to prevent.

    NO INJECTED RUNNER. An earlier version of this test passed `runner=` and so
    never exercised the default path at all: it asserted only that argv[0] was
    the executable, and would have passed unchanged against an implementation
    that called the installers in-process. `subprocess.run` is patched instead,
    which is the thing whose absence would be the bug.
    """
    targets["skills"] = {"claude-code": {"installed": True}}
    targets["tmux"] = True
    spawned = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kw: spawned.append(list(argv))
                        or type("Done", (), {"returncode": 0})())
    monkeypatch.setattr("collab.skills.install", _forbidden("skills.install"))
    monkeypatch.setattr("collab.statusline.install.install",
                        _forbidden("statusline install"))

    update.refresh_installed()

    assert [call[0] for call in spawned] == ["/opt/bin/collab"] * 2
    assert [call[1] for call in spawned] == ["skills", "statusline"]


def _forbidden(name):
    def _no(*a, **k):
        raise AssertionError(f"{name} must not be called in this process")
    return _no


def test_a_successful_update_actually_calls_the_refresh(monkeypatch):
    """Nothing asserted the wiring: both call sites could be deleted and the
    suite stayed green, which is a feature that exists only in the docstring."""
    calls = []
    monkeypatch.setattr(update, "refresh_installed",
                        lambda **kw: calls.append(True) or ["skills refreshed"])
    monkeypatch.setattr(update, "apply_update", lambda: (True, ""))
    monkeypatch.setattr(update, "installed_as",
                        lambda: update.Install("pip", None, ["pip", "install"]))

    info = update.UpdateInfo(current="1.0.0", latest="1.1.0", available=True)
    assert update.prompt_and_maybe_update(info) is True
    assert calls == [True], "a successful update must refresh what it installed"


def test_a_failed_update_refreshes_nothing(monkeypatch):
    """There is no new version to reinstall, so reinstalling would be a lie."""
    calls = []
    monkeypatch.setattr(update, "refresh_installed",
                        lambda **kw: calls.append(True) or [])
    monkeypatch.setattr(update, "apply_update", lambda: (False, "boom"))
    monkeypatch.setattr(update, "installed_as",
                        lambda: update.Install("pip", None, ["pip", "install"]))

    info = update.UpdateInfo(current="1.0.0", latest="1.1.0", available=True)
    assert update.prompt_and_maybe_update(info) is False
    assert calls == []


def test_a_broken_installer_module_does_not_take_the_update_with_it(monkeypatch):
    """Neither half may turn a good upgrade into a bad exit code."""
    monkeypatch.setattr("collab.skills.status",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("collab.statusline.install.status_tmux",
                        lambda *a, **k: {"installed": False})
    monkeypatch.setattr("collab.statusline.install.status_claude_code",
                        lambda *a, **k: {"installed": False})

    assert update.refresh_installed(runner=_Ran()) == []
