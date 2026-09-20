"""File transfer: size cap, privacy, checksum, and delete-on-receipt."""

from __future__ import annotations

import hashlib
import io

from collab.protocol import MAX_FILE_BYTES


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join",
                    json={"protocol_major": 2, "version": "2.0.0", "invite": session["invite"], "name": name, "hello": {}})
    return {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {r.json()['token']}"}


def _upload(client, headers, content: bytes, name="artifact.bin", **params):
    return client.post("/ext/collab/v1/files", headers=headers,
                       files={"file": (name, io.BytesIO(content), "application/octet-stream")},
                       params=params)


def test_upload_download_roundtrip(client, session, host_headers):
    bob = _join(client, session, "bob")
    payload = b"\x00\x01binary build artifact\xff" * 100

    up = _upload(client, host_headers, payload, name="build.tar.gz")
    assert up.status_code == 200
    record = up.json()
    assert record["size"] == len(payload)
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    assert record["download_url"].endswith(f"/files/{record['id']}/content")

    down = client.get(f"/ext/collab/v1/files/{record['id']}/content", headers=bob)
    assert down.status_code == 200
    assert down.content == payload, "bytes must survive exactly"
    assert down.headers["X-Collab-Sha256"] == record["sha256"]


def test_upload_is_announced_so_the_other_agent_learns_of_it(client, session, host_headers):
    _join(client, session, "bob")
    up = _upload(client, host_headers, b"data", name="report.pdf").json()
    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    shared = [e for e in events if e["kind"] == "file"]
    assert shared and shared[-1]["body"]["name"] == "report.pdf"
    assert shared[-1]["body"]["id"] == up["id"]


def test_file_is_deleted_once_receipt_is_confirmed(client, session, host_headers):
    bob = _join(client, session, "bob")
    record = _upload(client, host_headers, b"payload", name="a.bin").json()

    ack = client.post(f"/ext/collab/v1/files/{record['id']}/ack", headers=bob)
    assert ack.status_code == 200
    assert ack.json()["deleted"] is True

    # The only copy is gone, so a second fetch must fail rather than half-work.
    again = client.get(f"/ext/collab/v1/files/{record['id']}/content", headers=bob)
    assert again.status_code == 404
    assert client.get("/ext/collab/v1/files", headers=bob).json()["files"] == []


def test_ack_is_announced_back_to_the_sender(client, session, host_headers):
    bob = _join(client, session, "bob")
    record = _upload(client, host_headers, b"x", name="a.bin", to="bob").json()
    client.post(f"/ext/collab/v1/files/{record['id']}/ack", headers=bob)
    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    assert any(e["kind"] == "file" and e["body"].get("action") == "received" for e in events)


def test_oversized_upload_is_refused(client, host_headers):
    too_big = b"x" * (MAX_FILE_BYTES + 1024)
    r = _upload(client, host_headers, too_big, name="huge.bin")
    assert r.status_code == 413
    assert "10MB" in r.json()["detail"]
    assert client.get("/ext/collab/v1/files", headers=host_headers).json()["files"] == []


def test_a_file_sent_to_one_person_is_private(client, session, host_headers):
    bob = _join(client, session, "bob")
    carol = _join(client, session, "carol")
    record = _upload(client, host_headers, b"secret", name="key.pem", to="bob").json()

    assert client.get(f"/ext/collab/v1/files/{record['id']}/content",
                      headers=carol).status_code == 403
    assert client.get(f"/ext/collab/v1/files/{record['id']}/content",
                      headers=bob).status_code == 200
    assert client.get("/ext/collab/v1/files", headers=carol).json()["files"] == []


def test_download_requires_a_token(client, host_headers):
    record = _upload(client, host_headers, b"x").json()
    assert client.get(f"/ext/collab/v1/files/{record['id']}/content").status_code == 401


def test_sender_can_withdraw_a_file(client, session, host_headers):
    bob = _join(client, session, "bob")
    record = _upload(client, host_headers, b"oops", name="wrong.bin").json()
    assert client.delete(f"/ext/collab/v1/files/{record['id']}", headers=bob).status_code == 403
    assert client.delete(f"/ext/collab/v1/files/{record['id']}",
                         headers=host_headers).status_code == 200
    assert client.get(f"/ext/collab/v1/files/{record['id']}/content",
                      headers=bob).status_code == 404
