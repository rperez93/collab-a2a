"""What one agent found out, kept where the next one will read it.

A session is a conversation, and a conversation is the wrong shape for a fact.
«The staging bucket needs the eu-west key» is said once, at four in the
afternoon, to whoever happened to be reading — and then it is a hundred
messages back, invisible to the agent that joins tomorrow and to the agent that
compacted its context an hour later. Every session in a repository rediscovers
the same handful of things.

So it is said AND written down, and these tests are about where and how often.
Four things they hold:

* it goes to the SHARED state directory, not to the writing agent's own — two
  agents in one checkout are working on one repository, and a file each would
  give them two half-answers with no way to know it;
* every daemon files every learning it sees, its own sender's included, and the
  same one is never written twice however many daemons saw it;
* the body decides and the prefix does not, because anybody can type a message
  beginning «learning:»;
* a host tool that keeps project notes gets a copy in them, and its index gets
  one pointer rather than one per learning.

Every test here points HOME and the tool's config directory at a temporary
folder. The mirror writes into a home directory, and a test that wrote into the
real one would be putting its fixtures in somebody's notes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time

import pytest

from collab import cli, learnings
from collab.config import COLLAB_DIRNAME, SessionProfile
from collab.protocol import KIND_CHAT, KIND_TASK, Envelope


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout with one agent's state directory in it, and a private HOME.

    HOME is redirected first and unconditionally: `mirror_to_memory` writes
    into it, and a test that let that reach the real one would be writing its
    own fixtures into somebody's project notes.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()
    checkout = tmp_path / "checkout"
    (checkout / f"{COLLAB_DIRNAME}-alice").mkdir(parents=True)
    return checkout


def _home(repo, name="alice"):
    return repo / f"{COLLAB_DIRNAME}-{name}"


def _learning(text, *, seq=1, sender="alice", marked=True):
    return Envelope(kind=KIND_CHAT, text=f"{learnings.PREFIX} {text}",
                    sender=sender, seq=seq,
                    body={learnings.MARKER: True} if marked else {})


# --- where it lands -------------------------------------------------------------

def test_a_learning_lands_in_the_shared_directory_and_not_the_agents_own(repo):
    """Two agents in one checkout are working on one repository. A file each
    would give them two half-answers and no way to know it."""
    learnings.record(_home(repo), _learning("the staging bucket wants eu-west"),
                     session_id="s1")
    shared = repo / COLLAB_DIRNAME / learnings.FILENAME
    assert shared.exists()
    assert not (_home(repo) / learnings.FILENAME).exists()
    assert "the staging bucket wants eu-west" in shared.read_text()


def test_the_line_says_when_and_who(repo):
    line = learnings.line_for("alice", "always rebase", when=1757080980.0)
    assert line.startswith("- ")
    assert " · alice: always rebase" in line
    stamp = line[2:].split(" · ")[0]
    assert time.strptime(stamp, "%Y-%m-%d %H:%M")


def test_two_agents_in_one_checkout_write_it_once(repo):
    """Both daemons receive the same event and both want to file it."""
    env = _learning("one thing", seq=7)
    first = learnings.record(_home(repo, "alice"), env, session_id="s1")
    second = learnings.record(_home(repo, "bob"), env, session_id="s1")
    assert first and not second
    body = (repo / COLLAB_DIRNAME / learnings.FILENAME).read_text()
    assert body.count("one thing") == 1


def test_the_same_sentence_learnt_again_later_is_a_second_learning(repo):
    """De-duplicated on the sequence number, never on the text. The same thing
    learnt a month apart is two learnings, and the second is the confirmation."""
    learnings.record(_home(repo), _learning("always rebase", seq=1), session_id="s1")
    learnings.record(_home(repo), _learning("always rebase", seq=90), session_id="s1")
    assert len(learnings.read(_home(repo))) == 2


def test_the_same_sequence_in_a_different_session_is_not_the_same_event(repo):
    """`seq` is per session; two sessions both start at one."""
    learnings.record(_home(repo), _learning("a", seq=1), session_id="s1")
    learnings.record(_home(repo), _learning("b", seq=1), session_id="s2")
    assert len(learnings.read(_home(repo))) == 2


# --- what counts as one ---------------------------------------------------------

def test_the_body_decides_and_the_prefix_does_not(repo):
    """Anybody can type a message beginning «learning:», and a message that
    merely looks like one must not be filed as a fact about the repository."""
    assert not learnings.is_learning(_learning("looks like one", marked=False))
    assert learnings.record(_home(repo), _learning("looks like one", marked=False),
                            session_id="s1") == ""


def test_only_a_chat_can_be_one(repo):
    env = Envelope(kind=KIND_TASK, text="learning: x", seq=2,
                   body={learnings.MARKER: True})
    assert not learnings.is_learning(env)


def test_the_prefix_is_not_written_down_twice(repo):
    """It is on the wire so the sentence reads as what it is in somebody's
    transcript. In the file, the line already says what it is."""
    learnings.record(_home(repo), _learning("no prefix here"), session_id="s1")
    assert learnings.read(_home(repo))[0].endswith("alice: no prefix here")


def test_an_empty_learning_is_not_filed(repo):
    assert learnings.record(_home(repo), _learning("   "), session_id="s1") == ""


def test_control_characters_never_reach_the_file(repo):
    """The file is printed to a terminal by `collab learn --list`."""
    learnings.record(_home(repo), _learning("a\x1b[2Jb"), session_id="s1")
    assert "\x1b" not in learnings.read(_home(repo))[0]


def test_a_very_long_learning_is_cut(repo):
    """This is read back into an agent's context at the start of every session,
    so an unbounded one is a tax paid by everybody from then on."""
    learnings.record(_home(repo), _learning("x" * 5000), session_id="s1")
    assert len(learnings.read(_home(repo))[0]) < learnings.MAX_TEXT + 100


# --- the host tool's own project notes ------------------------------------------

def _project_dir(tmp_home, repo):
    slug = str(repo.resolve()).replace("/", "-")
    where = tmp_home / ".claude" / "projects" / slug
    where.mkdir(parents=True)
    return where


def test_nothing_is_written_where_the_tool_has_never_opened_this_repo(repo, tmp_path):
    """The config directory existing says the tool is installed. Only the
    project folder says it has been run HERE — without that second condition
    every daemon on the machine creates folders for repositories that tool has
    never seen."""
    (tmp_path / "home" / ".claude").mkdir()
    learnings.record(_home(repo), _learning("a"), session_id="s1")
    assert not list((tmp_path / "home" / ".claude").rglob("*.md"))


def test_nothing_is_written_where_the_tool_is_not_installed_at_all(repo, tmp_path):
    learnings.record(_home(repo), _learning("a"), session_id="s1")
    assert not (tmp_path / "home" / ".claude").exists()


def test_a_learning_reaches_the_project_notes_with_its_frontmatter(repo, tmp_path):
    project = _project_dir(tmp_path / "home", repo)
    learnings.record(_home(repo), _learning("the eu-west key"), session_id="s1")
    note = project / "memory" / learnings.MEMORY_FILE
    body = note.read_text()
    assert body.startswith("---\n")
    assert f"name: {learnings.MEMORY_NAME}" in body
    assert f"description: {learnings.MEMORY_DESCRIPTION}" in body
    assert "type: project" in body
    assert "the eu-west key" in body


def test_the_notes_index_gets_one_pointer_and_not_one_per_learning(repo, tmp_path):
    """The index is loaded whole at the start of every session, so filling it
    is the most expensive possible way to be helpful."""
    project = _project_dir(tmp_path / "home", repo)
    for n in range(5):
        learnings.record(_home(repo), _learning(f"thing {n}", seq=n), session_id="s1")
    index = (project / "memory" / learnings.MEMORY_INDEX).read_text()
    assert index.count(learnings.MEMORY_FILE) == 1
    assert index.startswith("- [")


def test_an_index_that_already_points_at_it_is_left_alone(repo, tmp_path):
    project = _project_dir(tmp_path / "home", repo)
    memory = project / "memory"
    memory.mkdir()
    (memory / learnings.MEMORY_INDEX).write_text(
        f"- [Something else](other.md) — hook\n"
        f"- [Ours]({learnings.MEMORY_FILE}) — written by hand\n")
    learnings.record(_home(repo), _learning("a"), session_id="s1")
    index = (memory / learnings.MEMORY_INDEX).read_text()
    assert index.count(learnings.MEMORY_FILE) == 1
    assert "written by hand" in index


def test_the_config_directory_can_be_pointed_elsewhere(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    slug = str(repo.resolve()).replace("/", "-")
    (elsewhere / "projects" / slug).mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    learnings.record(_home(repo), _learning("a"), session_id="s1")
    assert (elsewhere / "projects" / slug / "memory"
            / learnings.MEMORY_FILE).exists()


def test_a_home_that_cannot_be_written_does_not_lose_the_learning(
        repo, tmp_path, monkeypatch):
    """The repository's file is the first of the two places, and a permission
    somebody tightened on the second is not a reason to lose the first."""
    def refuse(home):
        raise OSError("read-only file system")

    monkeypatch.setattr(learnings, "_memory_dir", refuse)
    written = learnings.record(_home(repo), _learning("the eu-west key"),
                               session_id="s1")
    assert "the eu-west key" in written, "the record still reports it was filed"
    assert "the eu-west key" in learnings.read(_home(repo))[0]


# --- the command ----------------------------------------------------------------

@pytest.fixture
def profile(repo, monkeypatch):
    home = _home(repo)
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="alice",
                           host_name="alice", token="t", home=str(home),
                           is_host=True, participant_id="p_a")
    saved.save()
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(repo / "peers"))
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: saved))
    return saved


def _run(**flags):
    args = argparse.Namespace(**{"text": [], "list": False, "room": None,
                                 "json": False, "session": None, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_learn(args)
    return code, out.getvalue()


class _Sent:
    """A hub client that records the envelope instead of sending it."""

    envelopes: list[Envelope] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, env):
        _Sent.envelopes.append(env)
        return {"seq": 1}


def test_the_command_marks_the_body_and_prefixes_the_text(profile, monkeypatch):
    """Both, because either alone is a half-feature: a body nobody can see, or
    a prefix anybody can type by accident."""
    _Sent.envelopes = []
    monkeypatch.setattr(cli, "_client", lambda p: _Sent())
    code, out = _run(text=["the", "staging", "bucket", "wants", "eu-west"])
    assert code == 0
    sent = _Sent.envelopes[-1]
    assert sent.kind == KIND_CHAT
    assert sent.body == {learnings.MARKER: True}
    assert sent.text == "learning: the staging bucket wants eu-west"
    assert "learnt" in out


def test_the_command_with_nothing_to_say_explains_both_halves(profile):
    code, out = _run(text=[])
    assert code == 1
    assert "--list" in out


def test_the_list_prints_what_the_repo_knows(profile, repo):
    learnings.record(_home(repo), _learning("always rebase"), session_id="s1")
    code, out = _run(list=True)
    assert code == 0
    assert "always rebase" in out
    assert str(learnings.path_for(_home(repo))) in out


def test_the_list_says_so_when_there_is_nothing(profile):
    code, out = _run(list=True)
    assert code == 0 and "nothing learnt here yet" in out


def test_the_list_can_be_read_as_json(profile, repo):
    learnings.record(_home(repo), _learning("always rebase"), session_id="s1")
    code, out = _run(list=True, json=True)
    payload = json.loads(out)
    assert len(payload["learnings"]) == 1
    assert payload["file"].endswith(learnings.FILENAME)
