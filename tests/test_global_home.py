"""Everything global lives in one folder, and moves as one.

Settings, the machine peer registry and the update-check stamp are all
per-person rather than per-repo. If they ever derive their location
independently, pointing `COLLAB_CONFIG` at a second profile moves some of them
and not the others — and the failure is invisible: you get a session that
reads one profile's name and another profile's peers.
"""

from __future__ import annotations

from pathlib import Path

from collab import peers, update
from collab.config import global_config_path


def _all_global_paths() -> dict[str, Path]:
    return {
        "settings": global_config_path(),
        "peers": peers.peers_dir(),
        "update-check": update.cache_path(),
    }


def test_they_share_one_folder():
    parents = {name: p.parent for name, p in _all_global_paths().items()}
    assert len(set(parents.values())) == 1, f"scattered: {parents}"


def test_the_folder_is_under_the_user_home_by_default(monkeypatch):
    monkeypatch.delenv("COLLAB_CONFIG", raising=False)
    monkeypatch.delenv("COLLAB_PEERS_DIR", raising=False)
    home = global_config_path().parent
    assert home == Path.home() / ".config" / "collab"


def test_pointing_the_config_elsewhere_moves_all_of_them(tmp_path, monkeypatch):
    """A second profile has to be a whole profile, not a partial one."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "other" / "config.json"))
    monkeypatch.delenv("COLLAB_PEERS_DIR", raising=False)

    for name, path in _all_global_paths().items():
        assert path.parent == tmp_path / "other", f"{name} did not follow"


def test_peers_can_still_be_moved_on_their_own(tmp_path, monkeypatch):
    """The tests need it, and it is the only global path with its own override."""
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "just-peers"))
    assert peers.peers_dir() == tmp_path / "just-peers"
    assert global_config_path().parent != tmp_path / "just-peers"


def test_repo_state_is_not_the_global_folder(tmp_path, monkeypatch):
    """`.collab/` in a repo and the global folder are different things.

    Running collab from a directory that is not a repo puts session state in
    that directory — including `$HOME`, which then holds a `~/.collab` that
    looks like, but is not, the global folder.
    """
    from collab.config import collab_home, state_root

    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert collab_home().is_relative_to(state_root())
    assert collab_home() != tmp_path / ".collab"
    assert collab_home() != global_config_path().parent
