"""R12: everyone's quota and skills, kept current by the daemon; unknown stays unknown."""
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
    assert {r["skills_state"] for r in dashboard_rows(SNAPSHOT, None)} == {"unknown"}
    assert {r["skills_state"] for r in dashboard_rows(SNAPSHOT, {"unsupported": True})} == {"hub too old"}


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
