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


def test_concurrent_uploads_of_one_delivery_leave_one_matching_blob(client, host_headers):
    """Two retries racing for the same key: one record, and its blob matches its hash."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    bodies = [b"A" * 200_000, b"B" * 200_000]
    def upload(body):
        return client.post("/ext/collab/v1/files", headers=host_headers,
                           files={"file": ("same.bin", body, "application/octet-stream")},
                           params={"room": "general", "delivery": "o_race"}).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, bodies))
    assert results[0]["id"] == results[1]["id"]
    fetched = client.get(f"/ext/collab/v1/files/{results[0]['id']}/content", headers=host_headers).content
    recorded = client.get("/ext/collab/v1/files", headers=host_headers).json()["files"]
    record = next(f for f in recorded if f["id"] == results[0]["id"])
    assert hashlib.sha256(fetched).hexdigest() == record["sha256"]


def _file_events(store, file_id):
    with store._lock:
        rows = store._db.execute("SELECT payload FROM events WHERE kind='file'").fetchall()
    return [r for r in rows if file_id in r[0]]


def test_a_stored_but_unannounced_delivery_is_announced_once_by_its_retry(client, host_headers):
    """The first request stored the file and died before publishing; the retry announces it, once."""
    params = {"room": "general", "delivery": "o_lost"}
    body = {"file": ("x.bin", b"payload", "application/octet-stream")}
    first = client.post("/ext/collab/v1/files", headers=host_headers, files=body, params=params).json()
    store = client.app.state.store
    with store._lock:
        store._db.execute("DELETE FROM worker_deliveries WHERE delivery_key=?", (f"file:{first['id']}",))
        store._db.execute("DELETE FROM events WHERE kind='file'")
        store._db.commit()
    for _ in range(2):
        again = client.post("/ext/collab/v1/files", headers=host_headers, files=body, params=params).json()
        assert again["duplicate"]
    assert len(_file_events(store, first["id"])) == 1


def test_a_row_whose_blob_never_landed_is_completed_not_acknowledged(client, host_headers):
    params = {"room": "general", "delivery": "o_noblob"}
    body = {"file": ("x.bin", b"payload", "application/octet-stream")}
    first = client.post("/ext/collab/v1/files", headers=host_headers, files=body, params=params).json()
    from pathlib import Path
    blob = Path(client.app.state.store.path).parent / "files" / first["id"]
    blob.unlink()  # the row committed, the blob never landed
    client.post("/ext/collab/v1/files", headers=host_headers, files=body, params=params)
    assert client.get(f"/ext/collab/v1/files/{first['id']}/content", headers=host_headers).content == b"payload"


def test_worker_off_sends_its_queue_with_the_delivery_key_and_checks_bytes(tmp_path, monkeypatch):
    import argparse
    import hashlib
    from types import SimpleNamespace
    from collab import cli, worker
    stash = tmp_path / "stash.bin"
    stash.write_bytes(b"abc")
    store = worker.Store(tmp_path)
    store.configure({"agent": "claude", "scope": "Coordinate"})
    monkeypatch.setattr(store.__class__, "outbox", lambda self: [{
        "id": "o_1", "kind": "file", "stash": str(stash), "name": "stash.bin",
        "sha256": hashlib.sha256(b"abc").hexdigest(), "to": "bob"}])
    calls = []
    class Hub:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def upload_file(self, path, **kw): calls.append(kw)
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    monkeypatch.setattr(store.__class__, "acknowledge", lambda self, i: None)
    profile = SimpleNamespace(dir=tmp_path, name="me", session_id="s")
    monkeypatch.setattr(cli, "_require_profile", lambda a: profile)
    try:
        cli.cmd_worker(argparse.Namespace(worker_action="off"))
    except Exception:
        pass  # what follows the flush is not under test
    assert calls and calls[0]["delivery"] == "o_1"
    stash.write_bytes(b"changed")
    calls.clear()
    try:
        cli.cmd_worker(argparse.Namespace(worker_action="off"))
    except Exception:
        pass
    assert calls == [], "changed bytes are never sent"
