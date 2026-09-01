"""A repo has one current session; daemons from earlier ones are orphans."""

from __future__ import annotations

from collab import config
from collab.client import daemon as d


def _profile(home, sid):
    p = config.SessionProfile(session_id=sid, url="http://x", name="bob",
                              host_name="alice", token="t", home=str(home))
    p.save()
    return p


def test_orphans_from_earlier_sessions_are_stopped(tmp_path, monkeypatch):
    old = _profile(tmp_path, "s_old")
    keep = _profile(tmp_path, "s_keep")

    stopped: list[int] = []
    monkeypatch.setattr(d, "provably_ours", lambda p: 999)
    monkeypatch.setattr(d, "_terminate", lambda pid: stopped.append(pid) or True)

    result = d.stop_orphans(tmp_path, keep="s_keep")
    assert result == ["s_old"]
    assert stopped == [999], "the current session's daemon must survive"


def test_nothing_to_stop_is_not_an_error(tmp_path, monkeypatch):
    _profile(tmp_path, "s_keep")
    monkeypatch.setattr(d, "provably_ours", lambda p: None)
    assert d.stop_orphans(tmp_path, keep="s_keep") == []


def test_missing_sessions_directory_is_fine(tmp_path):
    assert d.stop_orphans(tmp_path / "nope") == []
