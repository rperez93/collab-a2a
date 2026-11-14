"""Route authority comes from private records and session health, never route convenience."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from collab import peers, routes, telemetry_setup, worker
from collab.client import onboard
from collab.client.daemon import saved_hub_address


@pytest.mark.parametrize("directory", ["absent", None, "invalid"])
def test_a_partial_profile_cannot_fall_back_to_another_agents_route_state(monkeypatch, directory):
    """A read-only caller with no private directory has no route metadata authority."""
    profile = SimpleNamespace(url="https://original.invalid", token="synthetic")
    if directory != "absent":
        profile.dir = directory
    monkeypatch.setattr(routes.os, "open", lambda *a, **k: pytest.fail("no private route file exists"))
    assert routes.load(profile) == {}
    assert routes.generation(profile) == (profile.url, profile.token, None, None)


def announce(tmp_path, monkeypatch, session_id, local_url, *, public="https://original.invalid", invite="invite"):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setattr(peers, "machine_id", lambda: "owned-machine")
    monkeypatch.setattr(peers, "current_user", lambda: "owned-user")
    return peers.announce(session_id=session_id, name="host", role="host", url=public,
                          local_url=local_url, repo="/synthetic", home="/synthetic", invite=invite)


@pytest.mark.asyncio
async def test_a_discovered_local_hub_must_pass_session_health_before_the_invite(
        live_server, tmp_path, monkeypatch):
    """M25: a private stale port serving another session must receive no join POST."""
    local = live_server["base"]
    announce(tmp_path, monkeypatch, "s_wrong", local, public=local, invite=live_server["invite"])
    probes = []
    healthy = routes.healthy

    async def observed(candidate, expected, **kwargs):
        probes.append((candidate, expected))
        return await healthy(candidate, expected, **kwargs)

    class NoInviteClient:
        def __init__(self, *args, **kwargs):
            pytest.fail("the invite-bearing client was constructed before session health passed")

    monkeypatch.setattr(routes, "healthy", observed)
    monkeypatch.setattr(onboard, "HubClient", NoInviteClient)
    raw = local + "?collab_session=s_wrong#" + live_server["invite"]
    with pytest.raises(ValueError, match="no invite was submitted"):
        await asyncio.to_thread(onboard.join_session, raw, start_daemon=False)
    assert probes and probes[0] == (local, "s_wrong")
    assert len(live_server["store"].participants()) == 1


@pytest.mark.asyncio
async def test_a_local_join_keeps_the_visible_public_address_as_its_original_authority(
        live_server, tmp_path, monkeypatch):
    """M22: choosing private loopback must not turn it into the saved legacy authority."""
    original, local = "https://original.invalid", live_server["base"]
    announce(tmp_path, monkeypatch, "s_test", local, public=original, invite=live_server["invite"])
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "guest"))
    monkeypatch.setattr(onboard.ctx, "gather", lambda *a, **k: {})
    monkeypatch.setattr(telemetry_setup, "ensure", lambda profile: None)
    monkeypatch.setattr(worker.Store, "ensure_default", lambda self: None)
    profile, _, _ = await asyncio.to_thread(
        onboard.join_session, original + "#" + live_server["invite"],
        name="guest", start_daemon=False)
    assert profile.url == local
    assert routes.load(profile)["original_user_url"] == original, "selected loopback replaced visible authority"
    assert len(live_server["store"].participants()) == 2


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="private registry ownership is POSIX")
@pytest.mark.parametrize("change", ["valid", "directory_mode", "record_mode", "owner", "machine", "user", "ambiguous"])
def test_saved_hub_recovery_requires_private_owned_unambiguous_machine_records(
        profile, tmp_path, monkeypatch, change):
    """M23: loopback syntax cannot substitute for private registry ownership and identity."""
    candidate = "http://127.0.0.1:43210"
    record = announce(tmp_path, monkeypatch, profile.session_id, candidate)
    data = json.loads(record.read_text())
    if change == "directory_mode":
        record.parent.chmod(0o777)
    elif change == "record_mode":
        record.chmod(0o666)
    elif change == "owner":
        uid = os.getuid()
        monkeypatch.setattr(routes.os, "getuid", lambda: uid + 1)
    elif change in ("machine", "user"):
        data["machine_id" if change == "machine" else "user"] = "another-identity"
        record.write_text(json.dumps(data))
    elif change == "ambiguous":
        data["local_url"] = "http://127.0.0.1:43211"
        second = record.parent / "second-owned-host.json"
        second.write_text(json.dumps(data))
        second.chmod(0o600)
    expected = candidate if change == "valid" else ""
    assert saved_hub_address(profile, prefer_local=True) == expected, "private registry trust or ambiguity was bypassed"
