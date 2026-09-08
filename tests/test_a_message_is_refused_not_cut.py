"""A message over the limit is refused with the reason, never delivered in part.

The hub never cut a message. The wake prompt did: each message folded into a
batch was cut at two thousand characters with «…[truncated; `collab recv` has
it all]» appended, and a woken agent acted on the first two thousand
characters of an instruction, because going back for the rest is a step it was
not asked to take. The user saw it as «the hub truncates my messages».

Now there is one limit, `protocol.MAX_MESSAGE`, and it is refused at the moment
of sending — by `collab send` before the round trip, and by the hub on both of
its routes for clients that do not come that way — with the reason and the
alternative in the sender's terms. What reaches the wake is whole.
"""

from __future__ import annotations

import json
import time

import pytest

from collab.protocol import MAX_MESSAGE, message_refusal


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join",
                    json={"invite": session["invite"], "name": name, "hello": {}})
    assert r.status_code == 200, r.text
    return r.json()


def _headers(joined):
    return {"Authorization": f"Bearer {joined['token']}"}


def test_the_reason_names_the_length_the_limit_and_the_alternative():
    reason = message_refusal("x" * (MAX_MESSAGE + 1))
    assert f"{MAX_MESSAGE + 1:,}" in reason and f"{MAX_MESSAGE:,}" in reason
    assert "collab file send" in reason and "split" in reason
    assert message_refusal("x" * MAX_MESSAGE) == "", "the limit itself is allowed"


def test_the_rest_route_refuses_with_413_and_the_reason(client, session, host_headers):
    bob = _join(client, session, "bob")
    r = client.post("/ext/collab/v1/messages", headers=_headers(bob),
                    json={"text": "x" * (MAX_MESSAGE + 1)})
    assert r.status_code == 413, r.text
    assert "limit" in r.json()["detail"] and "collab file send" in r.json()["detail"]
    # And nothing was stored: a refusal is not a delivery in part.
    history = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    assert not any(e.get("sender") == "bob" and e.get("kind") == "chat" for e in history)


def test_the_a2a_route_refuses_with_the_same_reason(client, session):
    bob = _join(client, session, "bob")
    body = client.post("/a2a", headers={**_headers(bob), "A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "m1", "role": "ROLE_USER",
                        "parts": [{"data": {"collab": "v1", "kind": "chat",
                                            "text": "x" * (MAX_MESSAGE + 1)},
                                   "mediaType": "application/json"}]}}}).json()
    assert "error" in body, body
    assert "limit" in body["error"]["message"] and "collab file send" in body["error"]["message"]


def test_a_message_at_the_limit_is_carried(client, session, host_headers):
    bob = _join(client, session, "bob")
    r = client.post("/ext/collab/v1/messages", headers=_headers(bob),
                    json={"text": "y" * MAX_MESSAGE})
    assert r.status_code == 200, r.text


def test_send_refuses_before_the_round_trip(monkeypatch, capsys):
    """The sender is at this prompt; the reason belongs in front of them at
    once, and no hub is needed to say it."""
    from collab import cli
    called = []
    monkeypatch.setattr(cli, "_require_own_profile", lambda args: object())
    monkeypatch.setattr(cli, "_client", lambda profile: called.append(profile) or None)
    import argparse
    args = argparse.Namespace(text=["x" * (MAX_MESSAGE + 1)], to=None, room=None, thread=None)
    assert cli.cmd_send(args) == 1
    assert not called, "refused without reaching for the hub"
    out = capsys.readouterr()
    assert "limit" in out.out + out.err and "collab file send" in out.out + out.err


def test_the_wake_carries_a_long_message_whole(tmp_path):
    """What the batch holds is what was said. The cut at two thousand
    characters, with its note, is gone."""
    from collab import wake
    from collab.protocol import Envelope, KIND_CHAT
    clock = [time.time()]
    wake.write_config(tmp_path, wake.WakeConfig(command=["true"]))
    waker = wake.Waker(tmp_path, "s_test", now=lambda: clock[0])
    text = "z" * (MAX_MESSAGE - 10)
    assert waker.note(Envelope(kind=KIND_CHAT, sender="alice", text=text))
    line = json.loads(waker.pending.read_text().splitlines()[-1])
    assert line["text"] == text
    assert "truncated" not in line["text"]
