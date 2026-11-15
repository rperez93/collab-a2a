"""Real HTTP strong remote acceptance; synthetic key, never native prompts."""
import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from collab import cli, wake, remote_delivery
from collab.client import approval as strong
from collab.client.hub_client import HubClient
from collab.protocol import Envelope, KIND_CHAT
from collab.remote_requests import Inbox, make_offer, wire_text, receive_envelope
from test_strong_approval_client import world, no_real_prompt


@pytest.fixture
def remote(world, tmp_path, monkeypatch):
    profile = SimpleNamespace(dir=tmp_path, session_id='s_test', participant_id=world.bob.id,
                              name='bob', working_path=str(tmp_path))
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: profile)
    monkeypatch.setattr(cli, '_client', lambda profile: HubClient(world.base, 'bob-token'))
    command = [shutil.which('collab'), 'wake', 'deliver', '--to', 'codex', '--target', 'thread-remote']
    wake.write_config(tmp_path, wake.WakeConfig(command=command))
    sender = world.store.participant_for_token(world.host_token)
    offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=world.bob.id,
                       target='thread-remote', kind='prompt', text='REMOTE_EXACT_PAYLOAD')
    env = world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
        to='bob', to_id=world.bob.id, room=None, text=wire_text(offer)))
    receive_envelope(profile, env)
    inbox = Inbox(tmp_path)
    inbox.bind('thread-remote', command, tmp_path)
    original = strong.ApprovalClient.capabilities
    monkeypatch.setattr(strong.ApprovalClient, 'capabilities', lambda self: {
        **original(self), 'available': True, 'remote_delivery': True})
    return SimpleNamespace(world=world, profile=profile, inbox=inbox, offer=offer, env=env, command=command)


def accept(r):
    return cli.main(['remote', 'accept', r.offer['id']])


def test_strong_remote_acceptance_delivers_exactly_once(remote, monkeypatch):
    assert accept(remote) == 0
    assert remote.inbox.list()[0]['state'] == 'accepted'
    window = remote.world.hello.windows[-1]
    canonical = json.loads(window['review_text'])
    assert all(window[k] == canonical[k] for k in ('subject', 'destination', 'authority'))
    attempts = []
    async def exchange(command, prompt, cwd, env, timeout, callback):
        attempts.append((command, prompt, cwd))
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared is not None
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is True
    assert len(attempts) == 1 and b'REMOTE_EXACT_PAYLOAD' in attempts[0][1]
    assert attempts[0][0] == remote.command
    assert remote_delivery.prepare(remote.profile) is None
    assert remote.inbox.list()[0]['state'] == 'delivered'


def test_lost_consumption_answer_never_dispatches_or_retries(remote, monkeypatch):
    original = strong.ApprovalClient._call
    consumed = []
    def call(self, method, path, body=None):
        result = original(self, method, path, body)
        if path.endswith('/consume'):
            consumed.append(result)
            raise strong.Uncertain('lost response after commit')
        return result
    monkeypatch.setattr(strong.ApprovalClient, '_call', call)
    assert accept(remote) == 1
    assert len(consumed) == 1
    assert remote.inbox.list()[0]['state'] == 'approving'
    assert remote_delivery.prepare(remote.profile) is None
    assert accept(remote) == 1
    assert len(consumed) == 1


@pytest.mark.parametrize('prior_accepted', [False, True])
def test_changed_configuration_after_human_gesture_refuses(remote, monkeypatch, prior_accepted):
    first_id = remote.offer['id']
    if prior_accepted:
        assert accept(remote) == 0
        sender = remote.world.store.participant_for_token(remote.world.host_token)
        remote.offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=remote.world.bob.id,
                                  target='thread-remote', kind='prompt', text='second offer')
        env = remote.world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
            to='bob', to_id=remote.world.bob.id, room=None, text=wire_text(remote.offer)))
        receive_envelope(remote.profile, env)
    original = remote.world.hello.open_window
    def window(request, fetch):
        result = original(request, fetch)
        wake.write_config(remote.profile.dir, wake.WakeConfig(command=[]))
        return result
    monkeypatch.setattr(remote.world.hello, 'open_window', window)
    assert accept(remote) == 1
    states = {r['id']: r['state'] for r in remote.inbox.list()}
    assert states[remote.offer['id']] == 'pending'
    if prior_accepted:
        assert states[first_id] == 'failed'
    assert remote_delivery.prepare(remote.profile) is None


def test_changed_review_is_refused_before_window(remote, monkeypatch):
    original = strong.ApprovalClient.review
    def review(self, identity):
        result = original(self, identity)
        result['review']['binding']['cwd'] = '/different'
        return result
    monkeypatch.setattr(strong.ApprovalClient, 'review', review)
    assert accept(remote) == 1
    assert remote.world.hello.windows == []
    assert remote.inbox.list()[0]['state'] == 'pending'


def test_remote_capability_is_separate_and_has_no_terminal_fallback(remote, monkeypatch):
    monkeypatch.setattr(strong.ApprovalClient, 'capabilities', lambda self: {'available': True, 'supported_methods': ['hello']})
    from collab import consent
    monkeypatch.setattr(consent, 'request', lambda *a, **k: pytest.fail('terminal fallback'))
    assert accept(remote) == 1
    assert remote.world.hello.windows == []


def test_decline_is_local_and_never_consumes(remote):
    remote.world.hello.choice = 'decline'
    assert accept(remote) == 0
    assert remote.inbox.list()[0]['state'] == 'declined'
    assert remote_delivery.prepare(remote.profile) is None


@pytest.mark.parametrize('change', ['command', 'expiry'])
def test_change_after_prepare_never_launches_or_replays(remote, monkeypatch, change):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared is not None
    if change == 'command':
        wake.write_config(remote.profile.dir, wake.WakeConfig(command=[]))
    else:
        monkeypatch.setattr(remote_delivery.time, 'time', lambda: remote.offer['expires_at'] + 1)
    async def forbidden(*a, **k):
        pytest.fail('stale prepared attempt launched')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    assert remote.inbox.list()[0]['state'] == 'uncertain'
    assert remote_delivery.prepare(remote.profile) is None


def test_receiver_selected_goal_uses_the_reviewed_local_target(remote):
    sender = remote.world.store.participant_for_token(remote.world.host_token)
    remote.offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=remote.world.bob.id,
                              target='receiver-selected', kind='goal', text='reviewed connection goal')
    env = remote.world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
        to='bob', to_id=remote.world.bob.id, room=None, text=wire_text(remote.offer)))
    receive_envelope(remote.profile, env)
    assert accept(remote) == 0
    reviewed = json.loads(remote.world.hello.windows[-1]['review_text'])
    assert reviewed['offer']['kind'] == 'goal'
    assert reviewed['binding']['target'] == 'thread-remote'
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared[0]['id'] == remote.offer['id']
    assert prepared[0]['delivery_target'] == 'thread-remote'


def test_a_prepared_delivery_tuple_cannot_be_executed_twice(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    attempts = []
    async def exchange(*args):
        attempts.append(args)
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is True
    try:
        asyncio.run(remote_delivery.deliver(remote.profile, prepared))
    except ValueError:
        pass
    assert len(attempts) == 1


def test_concurrent_prepared_delivery_reuse_has_one_launch_owner(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    attempts = []
    async def exchange(*args):
        attempts.append(args)
        await asyncio.sleep(.02)
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    async def race():
        return await asyncio.gather(remote_delivery.deliver(remote.profile, prepared),
                                    remote_delivery.deliver(remote.profile, prepared))
    assert sorted(asyncio.run(race())) == [False, True]
    assert len(attempts) == 1
    assert remote.inbox.list()[0]['state'] == 'delivered'


def test_old_inflight_record_is_not_relaunched_after_marker_migration(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    import sqlite3
    with sqlite3.connect(remote.inbox.path) as db:
        db.execute('ALTER TABLE offers DROP COLUMN delivery_started')
    async def forbidden(*a, **k):
        pytest.fail('old in-flight record relaunched')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    assert remote.inbox.list()[0]['state'] == 'dispatching'
    assert remote_delivery.prepare(remote.profile) is None


def test_rebinding_away_and_back_cannot_revive_prepared_delivery(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    other = remote.command[:-1] + ['another-thread']
    remote.inbox.bind('another-thread', other, remote.profile.working_path)
    remote.inbox.bind('thread-remote', remote.command, remote.profile.working_path)
    async def forbidden(*a, **k):
        pytest.fail('revoked prepared delivery revived')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    assert remote.inbox.list()[0]['state'] == 'failed'


def test_failed_launch_binding_cannot_be_retried_with_restored_arguments(remote):
    assert accept(remote) == 0
    offer = remote_delivery.prepare(remote.profile)[0]
    assert remote.inbox.start_delivery(offer['id'], offer['receipt'], target='thread-remote',
        command=remote.command, cwd='/wrong') is None
    assert remote.inbox.start_delivery(offer['id'], offer['receipt'], target='thread-remote',
        command=remote.command, cwd=remote.profile.working_path) is None
    assert remote.inbox.list()[0]['state'] == 'failed'
