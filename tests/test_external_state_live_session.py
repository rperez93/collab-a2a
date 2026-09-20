"""Host and concurrent joins resolve private homes through real CLI processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys


def test_three_agents_share_a_checkout_without_sharing_session_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {**os.environ, "COLLAB_HOME": "", "COLLAB_NO_UPDATE_CHECK": "1",
           "COLLAB_NO_TUNNEL": "1", "NO_COLOR": "1"}

    def run(actor, *args, check=True):
        result = subprocess.run(
            [sys.executable, "-m", "collab.cli", *args], cwd=workspace,
            env={**env, "COLLAB_AGENT_ID": actor}, capture_output=True,
            text=True, timeout=65)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def profile(actor):
        result = subprocess.check_output(
            [sys.executable, "-c", "from collab.config import collab_home; print(collab_home())"],
            cwd=workspace, env={**env, "COLLAB_AGENT_ID": actor}, text=True, timeout=10)
        home = Path(result.strip())
        sid = (home / "current").read_text().strip()
        return home, json.loads((home / "sessions" / sid / "profile.json").read_text())

    try:
        run("host", "host", "--name", "host", "--no-tunnel", "--no-daemon", "--keep")
        host_home, host = profile("host")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda actor: run(actor, "join", "--local", host["session_id"],
                                             "--name", actor, "--keep"), ("alice", "bob")))
        alice_home, alice = profile("alice")
        bob_home, bob = profile("bob")
        assert len({host_home, alice_home, bob_home}) == 3
        assert len({host["token"], alice["token"], bob["token"]}) == 3
        assert alice["session_id"] == bob["session_id"] == host["session_id"]
        assert list(workspace.iterdir()) == []
        for home in (host_home, alice_home, bob_home):
            assert json.loads((home / "workspace.json").read_text())["path"] == str(workspace)
        run("alice", "send", "--to", "bob", "isolated inbox delivery")
        # recv may first return already-unread joins; repeat boundedly until
        # the daemon has stored this message from the live feed.
        received = ""
        for _ in range(5):
            received += run("bob", "recv", "--wait", "2").stdout
            if "isolated inbox delivery" in received:
                break
        assert "isolated inbox delivery" in received
        assert "isolated inbox delivery" not in run("alice", "recv", "--peek").stdout
    finally:
        for actor in ("alice", "bob", "host"):
            run(actor, "kill", "--disarm", check=False)
