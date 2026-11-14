"""R19: install a peer's shared skill only after review and the human's consent."""
import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest

from collab import cli, skills


def record(content="---\nname: review\ndescription: Review a diff\n---\nDo it.\n", owner="Bob B"):
    return {"id": "sk_1", "name": "review", "description": "Review a diff", "owner": owner,
            "owner_id": "p_b", "content": content, "size_bytes": len(content.encode()),
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


@pytest.fixture
def target(tmp_path):
    return skills.Target("claude-code", "Claude Code", "skills", tmp_path / "skills", tmp_path)


def test_install_is_namespaced_records_provenance_and_never_overwrites(target):
    dest = skills.install_shared(record(), target=target, session_id="s_1")
    assert dest.name == "shared-bob-b-review"
    assert (dest / "SKILL.md").read_text().endswith("Do it.\n")
    meta = json.loads((dest / skills.PROVENANCE).read_text())
    assert meta["publisher"] == "Bob B" and meta["publication_id"] == "sk_1" and meta["session_id"] == "s_1"
    with pytest.raises(ValueError, match="--force"):
        skills.install_shared(record(), target=target, session_id="s_1")
    skills.install_shared(record(content="---\nname: review\ndescription: v2\n---\nv2\n"),
                          target=target, session_id="s_1", force=True)
    assert (dest / "SKILL.md").read_text().endswith("v2\n")
    assert [i["dir"] for i in skills.shared_installs(target)] == ["shared-bob-b-review"]


def test_uninstall_removes_only_what_collab_installed(target):
    own = target.path / "shared-mine"
    own.mkdir(parents=True)
    with pytest.raises(ValueError):
        skills.uninstall_shared("shared-mine", target=target)
    with pytest.raises(ValueError):
        skills.uninstall_shared("../etc", target=target)
    dest = skills.install_shared(record(), target=target, session_id="s_1")
    skills.uninstall_shared(dest.name, target=target)
    assert not dest.exists() and own.exists()


class Hub:
    def __init__(self, rec): self.rec = rec
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def shared_skill(self, skill_id): return {"skill": self.rec}


def run(monkeypatch, target, rec, approved):
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(session_id="s_1"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub(rec))
    monkeypatch.setattr(skills, "skills_target", lambda agent=None: target)
    return cli.cmd_skills(argparse.Namespace(action="install-shared", target="sk_1", agent=None,
                                             force=False, human_approved=approved))


def test_the_cli_shows_the_text_and_needs_the_humans_yes(monkeypatch, target, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert run(monkeypatch, target, record(content="---\nname: review\ndescription: d\n---\n\x1b[2Jevil\n"),
               approved=False) == 1
    out = capsys.readouterr()
    assert "\x1b" not in out.out and "peer-authored text" in out.out
    assert "--human-approved" in out.err + out.out
    assert not target.path.exists(), "nothing written without consent"
    assert run(monkeypatch, target, record(), approved=True) == 0
    assert (target.path / "shared-bob-b-review" / "SKILL.md").exists()


def test_a_text_that_does_not_match_its_hash_is_refused(monkeypatch, target):
    bad = {**record(), "content": "tampered"}
    assert run(monkeypatch, target, bad, approved=True) == 1
    assert not target.path.exists()
