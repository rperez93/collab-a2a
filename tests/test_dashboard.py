"""R12: everyone's quota and skills, kept current by the daemon; unknown stays unknown."""
import pytest

from collab.cli import dashboard_rows


SNAPSHOT = {"you": "alice", "participants": [
    {"id": "p_a", "name": "alice", "connected": True, "skills_count": 2,
     "stats": {"quotas": {"five_hour": {"used_pct": 13, "resets_at": "1"}},
               "worker": {"budget_remaining": 41}}},
    {"name": "bob", "connected": True, "skills_count": 0, "stats": {}},
    {"name": "carol", "connected": False, "skills_count": 1, "stats": {"quotas": {"seven_day": {}}}},
]}
SKILLS = {"participants": [{"id": "p_a", "name": "alice", "skills": [{"name": "review", "description": "r"},
                                                       {"name": "deploy", "description": "d"}]},
                           {"name": "carol", "skills": [{"name": "plan", "description": "p"}]}]}


def test_quota_and_skills_for_everyone_with_unknowns_left_unknown():
    rows = {r["name"]: r for r in dashboard_rows(SNAPSHOT, SKILLS)}
    assert rows["alice"]["you"] and rows["alice"]["quotas"]["five_hour"]["used_pct"] == 13
    assert "13%" in rows["alice"]["quota_summary"]
    assert rows["alice"]["worker_budget"] == 41
    assert [s["name"] for s in rows["alice"]["skills"]] == ["review", "deploy"]
    assert rows["bob"]["quotas"] == {} and rows["bob"]["worker_budget"] is None
    assert rows["bob"]["skills_state"] == "not shared"
    assert rows["carol"]["quotas"]["seven_day"]["used_pct"] is None, "never shown as 0%"


def test_no_cache_and_an_old_hub_are_reported_as_such():
    states = lambda skills: {r["name"]: r["skills_state"] for r in dashboard_rows(SNAPSHOT, skills)}
    assert states(None) == {"alice": "unknown", "bob": "not shared", "carol": "unknown"}
    assert states({"unsupported": True})["alice"] == "hub too old"
    old_peer = {"participants": [{"name": "zed", "stats": {}}]}  # no skills_count at all
    assert dashboard_rows(old_peer, SKILLS)[0]["skills_state"] == "unknown"


def test_the_daemon_fetches_lists_only_when_a_count_changes_and_retries_failures():
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    calls = []
    fake = SimpleNamespace(snapshot={"participants": [{"id": "p1", "skills_count": 2}]},
                           _schedule_refresh=lambda name, op: calls.append(name))
    Daemon._maybe_refresh_skills(fake, client=None)
    Daemon._maybe_refresh_skills(fake, client=None)
    assert calls == ["skills", "skills"], "no successful fetch yet: still retried"
    fake._skills_signature, fake._skills_at = (("p1", 2),), __import__("time").time()
    Daemon._maybe_refresh_skills(fake, client=None)
    assert len(calls) == 2, "fetched and unchanged: no request"
    fake.snapshot["participants"][0]["skills_count"] = 3
    Daemon._maybe_refresh_skills(fake, client=None)
    assert len(calls) == 3


def test_a_stale_roster_shows_nobody_live_and_no_quota():
    rows = dashboard_rows(SNAPSHOT, SKILLS, live=False)
    assert not any(r["connected"] for r in rows)
    assert all(r["quotas"] == {} and r["quota_summary"] == "" for r in rows)


def test_own_quota_comes_from_local_figures_when_not_shared():
    snap = {"you": "bob", "participants": [{"name": "bob", "connected": True, "stats": {}}]}
    rows = dashboard_rows(snap, None, own_stats={"quotas": {"five_hour": {"used_pct": 40}}})
    assert "40%" in rows[0]["quota_summary"]


def test_lists_follow_the_participant_id_across_a_rename():
    snap = {"you_id": "p_a", "participants": [{"id": "p_a", "name": "alice2", "connected": True,
                                               "skills_count": 2, "stats": {}}]}
    rows = dashboard_rows(snap, SKILLS)
    assert [s["name"] for s in rows[0]["skills"]] == ["review", "deploy"] and rows[0]["you"]


def test_epoch_resets_read_as_a_duration():
    import time
    from collab.stats import _short_reset
    assert _short_reset(str(int(time.time()) + 3 * 3600 + 60)) == "3h"
    assert _short_reset(str(int(time.time()) - 5)) == "due"



def test_hostile_reset_values_cannot_crash_any_roster_view():
    from collab.stats import _short_reset, quota_summary
    for value in ("\u00b2", "9" * 30, "99999999999999"):
        _short_reset(value)
        quota_summary({"quotas": {"5h": {"used_pct": 10, "resets_at": value}}}, with_resets=True)
    assert "999%" in quota_summary({"quotas": {"5h": {"used_pct": 1e308}}})


def test_an_old_quota_is_marked_and_peer_text_is_escaped(capsys, tmp_path, monkeypatch):
    import json, time
    from types import SimpleNamespace
    from collab import cli
    from collab.client.daemon_files import DaemonPaths
    snap = {"you": "alice", "fetched_at": time.time(), "participants": [
        {"name": "ev\x1b[2Jil", "connected": True, "skills_count": 0,
         "stats": {"reported_at": time.time() - 7 * 3600, "model": "m",
                   "quotas": {"5h\x1b]0;x": {"used_pct": 10}}}}]}
    rows = dashboard_rows(snap, None)
    assert rows[0]["quota_stale"] and rows[0]["main_model"] == "m" and rows[0]["worker_model"] == "unknown"
    profile = SimpleNamespace(dir=tmp_path)
    paths = DaemonPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.snapshot.write_text(json.dumps(snap))
    monkeypatch.setattr(cli, "_require_profile", lambda a: profile)
    monkeypatch.setattr(cli, "read_status", lambda p: {"state": "live"})
    monkeypatch.setattr(cli, "daemon_state", lambda status, running=None: "live")
    monkeypatch.setattr(cli, "is_running", lambda p: 1)
    assert cli.cmd_dashboard(SimpleNamespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "\x1b[2J" not in out and "\x1b]0;" not in out and "old)" in out and "models: main m" in out


@pytest.mark.asyncio
async def test_the_daemon_fetch_uses_an_etag_backs_off_and_only_commits_on_success(tmp_path):
    import json, httpx
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    from collab.client.daemon_files import DaemonPaths
    paths = DaemonPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    answers = [httpx.Response(500), httpx.Response(200, json={"participants": []}, headers={"etag": 'W/"a"'}),
               httpx.Response(304)]
    seen = []
    async def transport(request):
        seen.append(request.headers.get("if-none-match"))
        return answers.pop(0)
    fake = SimpleNamespace(profile=SimpleNamespace(url="http://hub", token="t"), paths=paths)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await Daemon._fetch_skills(fake, client, ("sig",))
        assert getattr(fake, "_skills_signature", None) is None and fake._skills_retry_at > 0
        await Daemon._fetch_skills(fake, client, ("sig",))
        assert fake._skills_signature == ("sig",) and paths.skills.exists()
        await Daemon._fetch_skills(fake, client, ("sig2",))
    assert seen == [None, None, 'W/"a"'] and fake._skills_signature == ("sig2",)


def test_404_is_cached_as_hub_too_old(tmp_path):
    import asyncio, json, httpx
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    from collab.client.daemon_files import DaemonPaths
    paths = DaemonPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
            await Daemon._fetch_skills(SimpleNamespace(profile=SimpleNamespace(url="http://hub", token="t"),
                                                       paths=paths), client, ("s",))
    asyncio.run(go())
    assert json.loads(paths.skills.read_text())["unsupported"] is True


def test_nothing_is_fetched_while_nobody_shares():
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    calls = []
    fake = SimpleNamespace(snapshot={"participants": [{"id": "p1", "skills_count": 0}]},
                           _schedule_refresh=lambda name, op: calls.append(name))
    Daemon._maybe_refresh_skills(fake, client=None)
    assert calls == []


@pytest.mark.asyncio
async def test_a_roster_read_cues_the_skill_list_refresh(tmp_path):
    import httpx
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    from collab.client.daemon_files import DaemonPaths
    paths = DaemonPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    cued = []
    fake = SimpleNamespace(profile=SimpleNamespace(url="http://hub", token="t"), paths=paths,
                           snapshot={}, _adopt_identity=lambda: None,
                           _maybe_refresh_skills=lambda client: cued.append(client))
    roster = {"participants": [{"id": "p1", "skills_count": 2}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=roster))) as client:
        await Daemon._fetch_snapshot(fake, client)
    assert cued == [client]
