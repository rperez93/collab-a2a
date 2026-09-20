"""Independent final attacks on publication identity, recovery and capacity."""
import hashlib

import pytest

from collab import worker
from collab.capacity import estimate_capacity
from collab.client.hub_client import HubError
from collab.client.inbox import Inbox
from collab.client.recovery import repair_inbox
from collab.compatibility import request_headers
from collab.protocol import Envelope, EXT_PREFIX


def test_extremely_small_child_budgets_stay_bounded_by_native_slots_and_tightest_quota():
    """A finite positive budget must never overflow its intermediate ratio."""
    options = dict(concurrency_limit=8, active_children=2, reserve_percent=20,
                   percent_per_child={"fast": 1e-320, "week": 10}, now=1000)
    report = {"quota_observed_at": 999, "quotas": {"fast": {"used_pct": 10}, "week": {"used_pct": 30}}}
    result = estimate_capacity(report, **options)
    assert result["maximum_additional_children"] == 3 and result["binding_window"] == "week"
    for invalid in (-1, None, float("nan"), float("inf")):
        report["quotas"]["week"]["used_pct"] = invalid
        assert estimate_capacity(report, **options)["status"] == "unknown"
    report["quotas"]["week"]["used_pct"] = 100
    assert estimate_capacity(report, **options)["maximum_additional_children"] == 0
    report["quotas"]["week"] = {"used_pct": 0, "observed_at": 1001}
    assert estimate_capacity(report, **options)["status"] == "unknown"
    report["quotas"]["week"] = {"used_pct": 0}
    report["worker"] = {"quota_scope": "shared-with-main", "quotas": {"week": {"used_pct": 0}}}
    assert estimate_capacity(report, **options)["maximum_additional_children"] == 6
    unknown = estimate_capacity(report, **{**options, "concurrency_limit": None})
    assert unknown["status"] == "unknown" and unknown["maximum_additional_children"] is None


def test_a_reused_display_name_cannot_inherit_or_withdraw_the_previous_publishers_skill(client, session, host_headers):
    """Publication ownership must follow participant identity through renames."""
    store = session["store"]
    original = store.add_participant("bob", "first-token", is_host=False, meta={})
    text = "---\nname: chosen\ndescription: Review this\n---\nUntrusted instructions."
    body = {"name": "chosen", "description": "Review this", "content": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}
    endpoint = EXT_PREFIX + "/shared-skills"
    made = client.post(endpoint, headers=request_headers("first-token"), json=body)
    assert made.status_code == 200
    first = made.json()["skill"]
    store.rename(original.id, "original-bob")
    replacement = store.add_participant("bob", "second-token", is_host=False, meta={})
    forged = client.post(endpoint, headers=request_headers("second-token"), json={
        **body, "id": first["id"], "owner_id": original.id, "owner": "original-bob"})
    assert forged.status_code == 200
    assert forged.json()["skill"]["owner_id"] == replacement.id
    assert forged.json()["skill"]["id"] != first["id"]
    assert client.delete(endpoint + "/" + first["id"], headers=request_headers("second-token")).status_code == 404
    store.revoke(original.id)
    assert client.get(endpoint + "/" + first["id"], headers=host_headers).status_code == 404
    inventory = client.get(endpoint, headers=host_headers).json()
    assert inventory["untrusted"] and all("content" not in row for row in inventory["skills"])
    assert [row["owner_id"] for row in inventory["skills"]] == [replacement.id]


def test_interrupted_http_repair_crosses_whole_hidden_pages_without_disclosing_private_messages(client, session, host_headers, profile):
    """An interrupted repair must resume past private-only pages and preserve reads."""
    hub = session["store"]
    first = hub.append(Envelope(kind="chat", text="visible first"))
    for index in range(401):
        hub.append(Envelope(kind="chat", text=f"private {index}", sender_id="other-a", to_id="other-b", to="other-b"))
    last = hub.append(Envelope(kind="chat", text="visible last"))
    box = Inbox(profile.dir)
    box.record(last)
    box.mark_read([last.seq])
    box.close()
    class Client:
        fail = True
        def replay_page(self, after, *, through=None, limit=200):
            if after and self.fail:
                raise HubError("interrupted after first page")
            params = {"after": after, "limit": limit}
            if through is not None:
                params["through"] = through
            response = client.get(EXT_PREFIX + "/replay", headers=host_headers, params=params)
            assert response.status_code == 200
            assert "private" not in response.text
            return response.json()
    transport = Client()
    with pytest.raises(HubError, match="interrupted"):
        repair_inbox(profile, transport)
    transport.fail = False
    result = repair_inbox(profile, transport)
    assert result["complete"] and result["pages"] == 3
    box = Inbox(profile.dir)
    try:
        assert box.last_seq() == last.seq
        assert [row.seq for row in box.take_unread()] == [first.seq]
        assert box.pending_repairs() == [first.seq]
        assert "private" not in box.jsonl.read_text()
    finally:
        box.close()


def test_interruption_between_repair_notice_and_inbox_acknowledgement_does_not_repeat_decisions(tmp_path, monkeypatch):
    """Two SQLite databases cannot transact together; restart must deduplicate."""
    store = worker.Store(tmp_path)
    store.configure({"agent": "codex", "scope": "Coordinate"})
    store.commit_turn(expected_cursor=0, cursor=5, summary="Already answered newer messages",
                      replies=[], escalations=[])
    box = Inbox(tmp_path)
    box.record(Envelope(kind="chat", seq=1, text="Recovered question"), repaired=True)
    original = box.acknowledge_repairs
    def interrupted(seqs):
        raise OSError("synthetic interruption after worker commit")
    monkeypatch.setattr(box, "acknowledge_repairs", interrupted)
    try:
        with pytest.raises(OSError, match="synthetic interruption"):
            worker.notify_repairs(tmp_path, box)
        assert len(store.pending()) == 1 and box.pending_repairs() == [1]
        monkeypatch.setattr(box, "acknowledge_repairs", original)
        worker.notify_repairs(tmp_path, box)
        assert len(worker.Store(tmp_path).pending()) == 1
        assert worker.Store(tmp_path).snapshot()["cursor"] == 5
        assert not box.pending_repairs()
    finally:
        box.close()


def test_an_unrepresentable_numeric_quota_is_unknown_instead_of_crashing():
    from collab.capacity import estimate_capacity
    result = estimate_capacity({'quotas': {'account': {'used_pct': 10 ** 400}}, 'observed_at': 100},
                               concurrency_limit=4, active_children=0, task_budget_percent=5, now=100)
    assert result['status'] == 'unknown'
    assert result['maximum_additional_children'] is None
