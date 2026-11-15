"""What you are told when there is nothing to show, or no tunnel to show it on.

`collab host` with ngrok installed but no authtoken said «ngrok was not found»,
sending people to install a binary they already had. `collab status` outside a
session printed a state directory and nothing else, so finding out what was
running on the machine took three more commands.
"""

from __future__ import annotations

import os

import contextlib
import io

import pytest

from collab import config
from collab.cli import main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    config._CACHE.clear()
    yield tmp_path
    config._CACHE.clear()


def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main(list(argv))
    return code, out.getvalue()


def test_status_outside_a_session_says_what_to_do_next():
    code, out = _run("status")
    assert code == 0, out
    assert "no active session" in out
    assert "nothing running here" in out
    assert "collab host" in out


def test_status_json_stays_one_object():
    import json

    _, out = _run("status", "--json")
    assert json.loads(out)["active"] is False


def test_an_installed_ngrok_that_fails_is_not_called_missing(isolated, monkeypatch):
    """A real hub, with an ngrok on PATH that exits the way an unauthenticated
    one does."""
    from collab.server.session import HubConfig, hosted_sessions, stop_session

    bin_dir = isolated / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ngrok"
    fake.write_text("#!/bin/sh\necho 'ERR_NGROK_4018 authentication failed' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    home = isolated / ".collab"
    (isolated / ".git").mkdir()
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: isolated)

    # The hub this test starts, held by its process rather than looked up in
    # hub.json: under a loaded suite `host` can stop waiting before the hub has
    # recorded its pid, and a cleanup that reads pid 0 left it running for
    # hours (T_cf1f2c12b363).
    import signal
    import subprocess
    started = []
    real_popen = subprocess.Popen

    def popen(cmd, *a, **kw):
        child = real_popen(cmd, *a, **kw)
        if "collab.hub_main" in cmd:
            started.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", popen)
    try:
        code, out = _run("host", "--fresh", "--no-daemon", "--no-update-check",
                         "--name", "alice")
    finally:
        for cfg in hosted_sessions(home):
            stop_session(HubConfig.load(cfg.session_id, cfg.home) or cfg)
        for child in started:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)  # its own session: the group is its own
                try:
                    child.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(10)
    assert started, "the test must see the hub it starts"
    assert all(child.poll() is not None for child in started), "no hub outlives the test"
    assert code == 0, out
    assert "ngrok was not found" not in out
    assert "ngrok config add-authtoken" in out
    assert "ngrok.log" in out
