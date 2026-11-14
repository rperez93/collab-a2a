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


def test_a_retried_worker_comment_is_stored_once(client, host_headers):
    """R3: comments delivered by a worker carry a key; a retry is not a second comment."""
    task = client.post("/ext/collab/v1/tasks", headers=host_headers,
                       json={"action": "propose", "title": "T"}).json()
    task_id = (task.get("task") or task)["id"]
    body = {"subject": "task", "id": task_id, "text": "evidence", "collab_worker_delivery": "o_1"}
    first = client.post("/ext/collab/v1/comments", headers=host_headers, json=body)
    second = client.post("/ext/collab/v1/comments", headers=host_headers, json=body)
    assert first.status_code == second.status_code == 200
    comments = client.get("/ext/collab/v1/comments", headers=host_headers,
                          params={"subject": "task", "id": task_id}).json()["comments"]
    assert [c["text"] for c in comments] == ["evidence"]


def test_a_retried_worker_upload_is_stored_once(client, host_headers):
    """R3: a queued file whose delivery was uncertain must not be shared twice."""
    files = {"file": ("notes.txt", b"hello", "application/octet-stream")}
    first = client.post("/ext/collab/v1/files", headers=host_headers, files=files,
                        params={"room": "general", "delivery": "o_file1"})
    second = client.post("/ext/collab/v1/files", headers=host_headers,
                         files={"file": ("notes.txt", b"hello", "application/octet-stream")},
                         params={"room": "general", "delivery": "o_file1"})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"] and second.json().get("duplicate")
    listed = client.get("/ext/collab/v1/files", headers=host_headers).json()["files"]
    assert [f["id"] for f in listed].count(first.json()["id"]) == 1
