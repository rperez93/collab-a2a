"""Retries after an uncertain HTTP response must not repeat a worker reply."""
from collab.protocol import Envelope
from collab.server.store import Store


def test_delivery_key_survives_restart_and_is_scoped_to_authenticated_sender(tmp_path):
    path = tmp_path / "hub.db"
    store = Store(path)
    def message(sender="p_a"):
        return Envelope(kind="chat", sender="alice", sender_id=sender, text="Reply",
                        body={"collab_worker_delivery": "o_123"})
    first, new = store.append_once(message())
    assert new
    store.close()
    store = Store(path)
    try:
        again, new = store.append_once(message())
        assert not new and again.seq == first.seq
        different, new = store.append_once(message("p_b"))
        assert new and different.seq != first.seq
    finally:
        store.close()


def test_authenticated_http_retry_has_one_history_entry(client, host_headers):
    body = {"text": "Durable worker reply", "body": {"collab_worker_delivery": "o_retry"}}
    first = client.post("/ext/collab/v1/messages", headers=host_headers, json=body)
    second = client.post("/ext/collab/v1/messages", headers=host_headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["seq"] == second.json()["seq"]
    history = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    assert len([e for e in history if e.get("text") == body["text"]]) == 1
