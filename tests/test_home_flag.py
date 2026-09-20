"""`--home` on host and join: a folder name, and the last word on where state goes.

The order it settles: `.collab` by default, `.collab-<name>` when another
agent's lock holds `.collab`, and whatever `--home` says over both.
"""

from __future__ import annotations

import os

import pytest

from collab import lockfile
from collab.cli import _home_from, build_parser
from collab.config import base_home


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- reading the value -------------------------------------------------------

def test_a_bare_name_is_a_folder_in_the_repo(repo):
    assert _home_from("my-state") == repo / "my-state"


def test_a_dotted_name_is_still_a_folder_in_the_repo(repo):
    assert _home_from(".collab-review") == repo / ".collab-review"


def test_a_path_is_taken_at_its_word(repo, tmp_path):
    elsewhere = tmp_path / "far" / "away"
    assert _home_from(str(elsewhere)) == elsewhere.resolve()


def test_a_relative_path_with_a_separator_is_a_path(repo):
    assert _home_from("sub/dir").is_absolute()


# --- which commands have it --------------------------------------------------

def test_only_host_and_join_take_it():
    """They are the commands that decide where a session lives; the rest find
    it. A flag on everything would be a flag to get wrong everywhere."""
    parser = build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None)
               and "host" in a.choices)

    def has_home(command):
        return any("--home" in a.option_strings for a in sub.choices[command]._actions)

    assert has_home("host") and has_home("join")
    for other in ("send", "kill", "who", "task", "watch", "lock"):
        assert not has_home(other), f"{other} should not take --home"


# --- the order it settles ----------------------------------------------------

def _claim(home, name="alice"):
    home.mkdir(parents=True, exist_ok=True)
    lockfile.acquire(lockfile.Lock(name=name, session_id="s_1",
                                   hub_pid=os.getpid()), home)


def test_default_is_the_repos_own_folder(repo):
    from collab.config import resolve_home

    assert resolve_home("bob") == base_home()


def test_a_lock_moves_us_beside_it(repo):
    from collab.config import resolve_home

    _claim(base_home())
    assert resolve_home("bob") == base_home().with_name(".collab-bob")


def test_an_explicit_folder_wins_over_both(repo, monkeypatch):
    """Including over the lock: saying which folder you want is the point."""
    from collab.config import collab_home

    _claim(base_home())
    monkeypatch.setenv("COLLAB_HOME", str(_home_from("chosen")))
    assert collab_home("bob") == repo / "chosen"
