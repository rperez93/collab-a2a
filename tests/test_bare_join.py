"""`collab join` with no arguments is a full join, not a shortcut.

It is the form an agent reaches for when there is no link — the ordinary case,
two agents on one machine — so everything the URL form does before it connects
has to happen here too: the repo's lock is read, `--name` decides who is
arriving, and an agent that would collide with the one already here gets its
own state directory instead of walking into theirs.

That is true by construction — `_own_state_dir` runs before the URL is even
looked at — and it is worth a test precisely because it is by construction:
nothing about the arrangement announces itself, and the natural way to add a
"local" path later is a branch that skips it.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from collab import cli, config, lockfile, peers
from collab.cli import main


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    # Nothing to join: the join fails after the decisions this file is about,
    # which is exactly where we want to look.
    monkeypatch.setattr(peers, "find", lambda *a, **k: None)
    monkeypatch.setattr(peers, "candidates", lambda *a, **k: [])
    return tmp_path


def _somebody_here(repo, name="alice"):
    """A held lock on the repo's default directory, as another agent leaves."""
    base = repo / ".collab"
    base.mkdir(parents=True, exist_ok=True)
    (base / lockfile.LOCK_NAME).write_text(json.dumps({
        "name": name, "session_id": "s_theirs", "role": "host",
        "url": "http://127.0.0.1:9", "state_dir": str(base),
        "session_dir": str(base / "sessions" / "s_theirs"),
        "profile_path": str(base / "sessions" / "s_theirs" / "profile.json"),
        # A live process is what makes a claim real; the owner chain is
        # deliberately not ours, because the point is that it is somebody
        # else's claim.
        "participant_id": "p_alice", "hub_pid": os.getpid(),
        "owner_pids": [], "created_at": time.time(),
    }))
    return base


def test_the_bare_form_still_carries_every_flag_that_matters():
    """No arguments means no URL, not no options: who is arriving, where their
    state goes and what they are working on are all still yours to say."""
    args = cli.build_parser().parse_args(
        ["join", "--name", "bob", "--focus", "the client side",
         "--home", ".collab-review"])

    assert args.url == "" and not args.local, "nothing said about which session"
    assert args.name == "bob"
    assert args.focus == "the client side"
    assert args.home == ".collab-review"


def test_a_repo_somebody_else_holds_sends_you_to_your_own_directory(repo, capsys):
    _somebody_here(repo, name="alice")

    main(["join", "--name", "bob", "--no-update-check"])

    assert os.environ.get("COLLAB_HOME") == str(repo / ".collab-bob")
    out = capsys.readouterr().out
    assert "alice is using this repo's" in out
    assert ".collab-bob" in out


def test_the_name_decides_which_directory(repo):
    _somebody_here(repo, name="alice")

    main(["join", "--name", "carol", "--no-update-check"])

    assert os.environ.get("COLLAB_HOME") == str(repo / ".collab-carol")


def test_your_own_claim_is_not_something_to_step_around(repo):
    """The lock is yours: the default directory is where you belong."""
    _somebody_here(repo, name="bob")

    main(["join", "--name", "bob", "--no-update-check"])

    assert os.environ.get("COLLAB_HOME") in (None, str(repo / ".collab"))


def test_an_unheld_repo_stays_the_default(repo):
    main(["join", "--name", "bob", "--no-update-check"])

    assert os.environ.get("COLLAB_HOME") in (None, str(repo / ".collab"))


def test_home_is_honoured_too(repo, capsys):
    main(["join", "--home", ".collab-review", "--no-update-check"])

    assert os.environ.get("COLLAB_HOME") == str(repo / ".collab-review")


def test_the_decisions_happen_before_anything_is_looked_up(repo, monkeypatch):
    """Ordering is the whole point: a local path that resolved the session
    first and sorted the state directory afterwards would join as the wrong
    agent, into somebody else's inbox."""
    order = []
    real = cli._own_state_dir
    monkeypatch.setattr(cli, "_own_state_dir",
                        lambda a, n: (order.append("state-dir"), real(a, n))[1])
    monkeypatch.setattr(peers, "find",
                        lambda *a, **k: (order.append("find"), None)[1])

    main(["join", "--name", "bob", "--no-update-check"])

    assert order == ["state-dir", "find"]
