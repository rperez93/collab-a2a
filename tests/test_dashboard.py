"""R12: everyone's quota and skills, kept current by the daemon; unknown stays unknown."""
from collab.cli import dashboard_rows


SNAPSHOT = {"you": "alice", "participants": [
    {"name": "alice", "connected": True, "skills_count": 2,
     "stats": {"quotas": {"five_hour": {"used_pct": 13, "resets_at": "1"}},
               "worker": {"budget_remaining": 41}}},
    {"name": "bob", "connected": True, "skills_count": 0, "stats": {}},
    {"name": "carol", "connected": False, "skills_count": 1, "stats": {"quotas": {"seven_day": {}}}},
]}
SKILLS = {"participants": [{"name": "alice", "skills": [{"name": "review", "description": "r"},
                                                       {"name": "deploy", "description": "d"}]},
                           {"name": "carol", "skills": [{"name": "plan", "description": "p"}]}]}


def test_quota_and_skills_for_everyone_with_unknowns_left_unknown():
    rows = {r["name"]: r for r in dashboard_rows(SNAPSHOT, SKILLS)}
    assert rows["alice"]["you"] and rows["alice"]["quotas"]["five_hour"]["used_pct"] == 13
    assert rows["alice"]["worker_budget"] == 41
    assert [s["name"] for s in rows["alice"]["skills"]] == ["review", "deploy"]
    assert rows["bob"]["quotas"] == {} and rows["bob"]["worker_budget"] is None
    assert rows["bob"]["skills_state"] == "not shared"
    assert rows["carol"]["quotas"]["seven_day"]["used_pct"] is None, "never shown as 0%"


def test_no_cache_and_an_old_hub_are_reported_as_such():
    assert {r["skills_state"] for r in dashboard_rows(SNAPSHOT, None)} == {"unknown"}
    assert {r["skills_state"] for r in dashboard_rows(SNAPSHOT, {"unsupported": True})} == {"hub too old"}


def test_the_daemon_fetches_lists_only_when_a_count_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from collab.client.daemon import Daemon
    calls = []
    fake = SimpleNamespace(snapshot={"participants": [{"id": "p1", "skills_count": 2}]},
                           _schedule_refresh=lambda name, op: calls.append(name))
    Daemon._maybe_refresh_skills(fake, client=None)
    Daemon._maybe_refresh_skills(fake, client=None)
    assert calls == ["skills"]
    fake.snapshot["participants"][0]["skills_count"] = 3
    Daemon._maybe_refresh_skills(fake, client=None)
    assert calls == ["skills", "skills"]
