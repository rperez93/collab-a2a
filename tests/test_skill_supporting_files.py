"""R19b: a skill publication may carry selected supporting files, safely."""
import argparse
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from collab import cli, skills
from collab.skill_sharing import (MAX_FILES, MAX_FILES_BYTES, manifest_digest, read_selected,
                                  validate_files, validate_publication)


@pytest.fixture
def skill_dir(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: review\ndescription: Review a diff\n---\nUse checklist.md\n")
    (root / "checklist.md").write_text("- tests\n- docs\n")
    (root / "lib").mkdir()
    (root / "lib" / "notes.txt").write_text("notes\n")
    return root


def test_a1_selected_files_are_read_and_unsafe_ones_refused(skill_dir, tmp_path):
    pub = read_selected(skill_dir, files=["checklist.md", "lib/notes.txt"])
    assert [f["path"] for f in pub["files"]] == ["checklist.md", "lib/notes.txt"]
    assert pub["manifest_sha256"] == manifest_digest(pub["sha256"], pub["files"])
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (skill_dir / "link.txt").symlink_to(outside)
    (skill_dir / "run.sh").write_text("echo hi")
    os.chmod(skill_dir / "run.sh", 0o755)
    (skill_dir / "blob.bin").write_bytes(b"\xff\xfe\x00")
    for bad in ("../secret.txt", "/etc/passwd", "link.txt", "run.sh", "blob.bin", "lib", "SKILL.md"):
        with pytest.raises((ValueError, OSError)):
            read_selected(skill_dir, files=[bad])
    with pytest.raises(ValueError, match="duplicate"):
        validate_files([{"path": "a.md", "content": "x"}, {"path": "a.md", "content": "y"}])
    with pytest.raises(ValueError, match="at most"):
        validate_files([{"path": f"f{i}.md", "content": "x"} for i in range(MAX_FILES + 1)])
    with pytest.raises(ValueError, match="KiB"):
        validate_files([{"path": "big.md", "content": "x" * (MAX_FILES_BYTES + 1)}])


def test_a1_a_symlinked_parent_directory_is_refused(skill_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text("x")
    (skill_dir / "sub").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        read_selected(skill_dir, files=["sub/x.md"])


def test_a2_the_hub_stores_files_and_returns_the_manifest(client, host_headers, skill_dir):
    pub = read_selected(skill_dir, files=["checklist.md"])
    made = client.post("/ext/collab/v1/shared-skills", headers=host_headers, json=pub).json()["skill"]
    shown = client.get(f"/ext/collab/v1/shared-skills/{made['id']}", headers=host_headers).json()["skill"]
    assert shown["manifest_sha256"] == pub["manifest_sha256"]
    assert [f["path"] for f in shown["files"]] == ["checklist.md"]
    validate_publication(shown)  # the client re-checks every hash


def test_a2_an_old_hub_that_drops_files_is_reported(monkeypatch, skill_dir, capsys):
    class OldHub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def publish_skill(self, publication): return {"skill": {"id": "sk_" + "0" * 20}}
    monkeypatch.setattr(cli, "_require_own_profile", lambda a: SimpleNamespace())
    monkeypatch.setattr(cli, "_client", lambda p: OldHub())
    args = argparse.Namespace(action="publish", target=str(skill_dir), name=None, description=None,
                              file=["checklist.md"])
    assert cli.cmd_skills(args) == 1
    assert "not its supporting files" in capsys.readouterr().err


def record_with_files(files):
    content = "---\nname: review\ndescription: d\n---\nbody\n"
    pub = validate_publication({"name": "review", "description": "d", "content": content,
                                "sha256": hashlib.sha256(content.encode()).hexdigest(), "files": files})
    return {**pub, "id": "sk_1", "owner": "Bob", "owner_id": "p_b"}


def test_a3_a4_install_binds_the_manifest_and_writes_files_inside(monkeypatch, tmp_path):
    from collab import consent
    target = skills.Target("claude-code", "Claude Code", "skills", tmp_path / "skills", tmp_path)
    rec = record_with_files([{"path": "lib/notes.txt", "content": "notes\n"}])
    calls = []
    monkeypatch.setattr(consent, "request", lambda root, *, review, **f: calls.append(("req", f, review))
                        or consent.Receipt(token="t"))
    monkeypatch.setattr(consent, "consume", lambda root, receipt, **f: calls.append(("use", f, None)))
    monkeypatch.setattr(consent, "audit", lambda root, **kw: None)
    class Hub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def shared_skill(self, i): return {"skill": rec}
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(session_id="s", dir=tmp_path / "p"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    monkeypatch.setattr(skills, "skills_target", lambda agent=None: target)
    args = argparse.Namespace(action="install-shared", target="sk_1", agent=None, force=False)
    assert cli.cmd_skills(args) == 0
    assert calls[0][1]["digest"] == rec["manifest_sha256"] == calls[1][1]["digest"]
    assert calls[0][2]["supporting_files"][0]["text"] == "notes\n"
    dest = target.path / "shared-bob-review"
    assert (dest / "lib" / "notes.txt").read_text() == "notes\n"
    assert oct((dest / "lib" / "notes.txt").stat().st_mode & 0o777) == "0o644"
    meta = json.loads((dest / skills.PROVENANCE).read_text())
    assert meta["manifest_sha256"] == rec["manifest_sha256"] and meta["files"][0]["path"] == "lib/notes.txt"
    skills.uninstall_shared(dest.name, target=target)
    assert not dest.exists()


def test_a5_a_file_that_changed_after_the_manifest_is_refused(monkeypatch, tmp_path):
    from collab import consent
    a = record_with_files([{"path": "x.md", "content": "one"}])
    b = record_with_files([{"path": "x.md", "content": "two"}])
    assert a["manifest_sha256"] != b["manifest_sha256"]
    tampered = {**a, "files": b["files"]}            # republished file, stale manifest
    asked = []
    monkeypatch.setattr(consent, "request", lambda *a, **k: asked.append(1))
    class Hub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def shared_skill(self, i): return {"skill": tampered}
    target = skills.Target("claude-code", "Claude Code", "skills", tmp_path / "skills", tmp_path)
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(session_id="s", dir=tmp_path / "p"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    monkeypatch.setattr(skills, "skills_target", lambda agent=None: target)
    args = argparse.Namespace(action="install-shared", target="sk_1", agent=None, force=False)
    assert cli.cmd_skills(args) == 1
    assert asked == [] and not target.path.exists(), "refused before the human is asked"


def test_a3_a_review_too_large_to_show_in_full_is_refused_before_asking(monkeypatch, tmp_path):
    from collab import consent
    # escaping-heavy text: every char becomes \\uXXXX under ensure_ascii
    heavy = "é" * 14000
    rec = record_with_files([{"path": f"f{i}.md", "content": heavy[:3000]} for i in range(3)] +
                            [{"path": "big.md", "content": heavy}])
    asked = []
    monkeypatch.setattr(consent, "request", lambda *a, **k: asked.append(1))
    class Hub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def shared_skill(self, i): return {"skill": rec}
    target = skills.Target("claude-code", "Claude Code", "skills", tmp_path / "skills", tmp_path)
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(session_id="s", dir=tmp_path / "p"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    monkeypatch.setattr(skills, "skills_target", lambda agent=None: target)
    args = argparse.Namespace(action="install-shared", target="sk_1", agent=None, force=False)
    assert cli.cmd_skills(args) == 1 and asked == [] and not target.path.exists()


def test_a3_publish_refuses_a_bundle_no_peer_could_review(skill_dir, monkeypatch, capsys):
    (skill_dir / "huge.md").write_text("é" * 20000)
    monkeypatch.setattr(cli, "_require_own_profile", lambda a: SimpleNamespace())
    class Hub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def publish_skill(self, publication): raise AssertionError("sent")
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    args = argparse.Namespace(action="publish", target=str(skill_dir), name=None, description=None,
                              file=["huge.md"])
    assert cli.cmd_skills(args) == 1
    assert "too large for a peer to review" in capsys.readouterr().err
