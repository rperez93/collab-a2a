"""Concurrent owners of one workspace never share credentials or cursors."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from collab import config, owner
from collab.client.exclusive import Stamp


def test_fresh_commands_keep_their_owners_home_while_parallel_agents_do_not(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    script = '''
import json
from collab.config import ensure_home, SessionProfile, collab_home
h = ensure_home()
p = SessionProfile(session_id="same-room", url="http://hub", name="same-name",
                   host_name="host", token=__import__("os").environ["COLLAB_AGENT_ID"], home=str(h))
p.save()
print(json.dumps({"home": str(h), "profile": str(p.dir / "profile.json")}))
'''
    def run(agent):
        env = {**os.environ, "COLLAB_AGENT_ID": agent}
        env.pop("COLLAB_HOME", None)
        return json.loads(subprocess.check_output([sys.executable, "-c", script], env=env, text=True))
    with ThreadPoolExecutor(max_workers=2) as pool:
        alice, bob = list(pool.map(run, ["alice", "bob"]))
    assert alice["home"] != bob["home"]
    assert run("alice")["home"] == alice["home"]
    from pathlib import Path
    assert json.loads(Path(alice["profile"]).read_text())["token"] == "alice"
    assert json.loads(Path(bob["profile"]).read_text())["token"] == "bob"
    assert list(workspace.iterdir()) == []
    assert config.repo_for_home(alice["home"]) == workspace


def test_canonical_workspace_hash_and_opaque_session_ids_cannot_traverse(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_AGENT_ID", "../../other/" + "x" * 1000)
    real = tmp_path / "workspace"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert config.base_home(real) == config.base_home(alias)
    assert config.workspace_home(real).name == hashlib.sha256(os.fsencode(real)).hexdigest()
    assert len(config.agent_key()) == 64
    assert config.base_home(real).is_relative_to(config.state_root())


def test_codex_threads_with_the_same_display_name_have_distinct_homes(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_AGENT_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    first = config.collab_home(tmp_path, name="same")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-b")
    assert config.collab_home(tmp_path, name="same") != first


def test_known_agent_process_stamp_is_stable_across_cli_calls(tmp_path, monkeypatch):
    for key in ("COLLAB_AGENT_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(owner, "current", lambda: Stamp(pid=111, started="one", boot="boot"))
    first = config.base_home(tmp_path)
    assert config.base_home(tmp_path) == first
    monkeypatch.setattr(owner, "current", lambda: Stamp(pid=222, started="two", boot="boot"))
    assert config.base_home(tmp_path) != first


def test_legacy_state_is_never_adopted_without_explicit_selection(tmp_path, monkeypatch):
    legacy = tmp_path / ".collab"
    legacy.mkdir()
    (legacy / "current").write_text("old-owner")
    monkeypatch.chdir(tmp_path)
    assert config.collab_home() != legacy
    assert legacy not in config.candidate_homes()
    monkeypatch.setenv("COLLAB_HOME", str(legacy))
    assert config.collab_home() == legacy


def test_workspace_folders_without_git_are_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    assert config.base_home(a) != config.base_home(b)


def test_identity_cache_cannot_follow_a_different_agent_or_workspace(tmp_path, monkeypatch):
    from collab import identity
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monkeypatch.chdir(a)
    first = config.ensure_home()
    identity.save(first, name="first")
    assert config.agent_identity()["name"] == "first"
    monkeypatch.setenv("COLLAB_AGENT_ID", "another-agent")
    assert config.agent_identity() == {}
    second = config.ensure_home()
    identity.save(second, name="second")
    assert config.agent_identity()["name"] == "second"
    monkeypatch.chdir(b)
    assert config.agent_identity() == {}


def test_a_hook_missing_thread_environment_requires_a_unique_stamped_owner(tmp_path, monkeypatch):
    from collab import lockfile
    who = Stamp(pid=1234, started="stamp", boot="boot")
    monkeypatch.setattr(owner, "current", lambda: who)
    monkeypatch.setenv("COLLAB_AGENT_ID", "thread-one")
    first = config.base_home(tmp_path)
    lockfile.acquire(lockfile.Lock(name="same", session_id="room", hub_pid=os.getpid(), owner=who.encode()), first)
    assert config.process_owned_home(tmp_path) is None
    for key in ("COLLAB_AGENT_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    assert config.process_owned_home(tmp_path) == first
    monkeypatch.setattr(owner, "current", lambda: Stamp(pid=2345, started="other", boot="boot"))
    assert config.process_owned_home(tmp_path) is None
    monkeypatch.setattr(owner, "current", lambda: who)
    second = config.base_home(tmp_path)
    lockfile.acquire(lockfile.Lock(name="same", session_id="room", hub_pid=os.getpid(), owner=who.encode()), second)
    assert config.process_owned_home(tmp_path) is None
    monkeypatch.setattr(owner, "current", lambda: Stamp(pid=1234))
    assert config.process_owned_home(tmp_path) is None


def test_background_profile_save_does_not_rebind_a_legacy_workspace(tmp_path, monkeypatch):
    a, b = tmp_path / "repo-a", tmp_path / "repo-b"
    a.mkdir(); b.mkdir()
    home = a / ".collab"
    monkeypatch.chdir(b)
    profile = config.SessionProfile(session_id="room", url="http://hub", name="agent",
                                    host_name="host", token="token", home=str(home))
    assert config.repo_for_home(home) == a
    profile.save(make_current=False)
    assert config.repo_for_home(home) == a
    assert not (b / ".collab").exists()
