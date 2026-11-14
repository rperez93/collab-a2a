from remote_consent_helpers import receipt
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from collab import remote_delivery, wake
from collab.remote_requests import Inbox, digest, make_offer


@pytest.fixture
def approved(tmp_path):
    profile = SimpleNamespace(dir=tmp_path, session_id='s_room', participant_id='p_local')
    output = tmp_path / 'received.txt'
    command = [sys.executable, '-c',
               'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())', str(output)]
    wake.write_config(tmp_path, wake.WakeConfig(command=command))
    inbox = Inbox(tmp_path)
    offer = make_offer(session_id='s_room', sender_id='p_peer', recipient_id='p_local',
                       target='coding', kind='prompt', text='Approved test content')
    inbox.receive(offer, session_id='s_room', sender_id='p_peer', recipient_id='p_local')
    inbox.bind('coding', command)
    assert remote_delivery.prepare(profile) is None
    inbox.accept(offer['id'], reviewed_digest=digest(offer), session_id='s_room',
                 recipient_id='p_local', target='coding', command=command, approval=receipt(inbox, offer, 'coding', command))
    return profile, inbox, output


def test_real_adapter_receives_approved_content_once(approved):
    profile, inbox, output = approved
    prepared = remote_delivery.prepare(profile)
    assert asyncio.run(remote_delivery.deliver(profile, prepared)) is True
    assert 'Approved test content' in output.read_text()
    assert inbox.list()[0]['state'] == 'delivered'
    assert remote_delivery.prepare(profile) is None
    prompts = list(inbox.root.glob('approved-*.txt'))
    assert len(prompts) == 1
    assert os.stat(prompts[0]).st_mode & 0o777 == 0o600


def test_command_is_frozen_after_claim(approved):
    profile, inbox, output = approved
    prepared = remote_delivery.prepare(profile)
    wake.write_config(profile.dir, wake.WakeConfig(command=['unapproved-command']))
    assert asyncio.run(remote_delivery.deliver(profile, prepared)) is True
    assert output.exists()


def test_failed_delivery_is_not_replayed(approved, monkeypatch):
    profile, inbox, output = approved
    async def fail(*args, **kwargs):
        raise remote_delivery.WorkerRuntimeError('failure')
    monkeypatch.setattr(remote_delivery, '_exchange', fail)
    assert asyncio.run(remote_delivery.deliver(profile, remote_delivery.prepare(profile))) is False
    assert inbox.list()[0]['state'] == 'uncertain'
    assert remote_delivery.prepare(profile) is None
    assert not output.exists()


def test_cancellation_revokes_grant(approved, monkeypatch):
    profile, inbox, output = approved
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(remote_delivery, '_exchange', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(remote_delivery.deliver(profile, remote_delivery.prepare(profile)))
    assert inbox.list()[0]['state'] == 'uncertain'
    assert remote_delivery.prepare(profile) is None


def test_daemon_schedules_accepted_offer_without_ordinary_wake(approved):
    from collab.client.daemon import Daemon
    profile, inbox, output = approved
    daemon = object.__new__(Daemon)
    daemon.profile = profile
    daemon._waking = None
    daemon._drain_wake_result = lambda task: None
    async def exercise():
        await daemon._maybe_wake()
        assert daemon._waking is not None
        await daemon._waking
    asyncio.run(exercise())
    assert output.exists()
    assert inbox.list()[0]['state'] == 'delivered'


def test_disarm_then_rearm_cannot_revive_approval(approved):
    profile, inbox, output = approved
    original = wake.read_config(profile.dir)
    wake.write_config(profile.dir, wake.WakeConfig(command=[]))
    assert remote_delivery.prepare(profile) is None
    assert inbox.list()[0]['state'] == 'failed'
    wake.write_config(profile.dir, original)
    assert remote_delivery.prepare(profile) is None
    assert not output.exists()


def test_prompt_retention_is_bounded_without_consuming_consent(approved, monkeypatch):
    profile, inbox, output = approved
    monkeypatch.setattr(remote_delivery, 'MAX_PROMPTS', 1)
    old = inbox.root / 'approved-existing.txt'
    old.write_text('pending reader')
    assert remote_delivery.prepare(profile) is None
    assert inbox.list()[0]['state'] == 'accepted'
    os.utime(old, (1, 1))
    assert remote_delivery.prepare(profile) is not None
    assert not old.exists()


def test_relative_adapter_uses_bound_working_directory(approved, tmp_path):
    profile, inbox, output = approved
    profile.working_path = str(tmp_path)
    script = tmp_path / 'adapter.py'
    script.write_text('import pathlib; pathlib.Path("cwd-result").write_text(str(pathlib.Path.cwd()))')
    command = [sys.executable, 'adapter.py']
    wake.write_config(profile.dir, wake.WakeConfig(command=command))
    inbox.bind('coding', command, cwd=tmp_path)
    offer = make_offer(session_id=profile.session_id, sender_id='p_peer',
                       recipient_id=profile.participant_id, target='coding', kind='prompt', text='new review')
    inbox.receive(offer, session_id=profile.session_id, sender_id='p_peer', recipient_id=profile.participant_id)
    inbox.accept(offer['id'], reviewed_digest=digest(offer), session_id=profile.session_id,
                 recipient_id=profile.participant_id, target='coding', command=command,
                 approval=receipt(inbox, offer, 'coding', command, tmp_path), cwd=tmp_path)
    assert asyncio.run(remote_delivery.deliver(profile, remote_delivery.prepare(profile))) is True
    assert (tmp_path / 'cwd-result').read_text() == str(tmp_path)


def test_full_prompt_capacity_does_not_skip_disarm_invalidation(approved, monkeypatch):
    profile, inbox, output = approved
    monkeypatch.setattr(remote_delivery, 'MAX_PROMPTS', 1)
    (inbox.root / 'approved-existing.txt').write_text('reader')
    original = wake.read_config(profile.dir)
    wake.write_config(profile.dir, wake.WakeConfig(command=[]))
    assert remote_delivery.prepare(profile) is None
    assert inbox.list()[0]['state'] == 'failed'
    wake.write_config(profile.dir, original)
    assert remote_delivery.prepare(profile) is None


def test_pending_offer_never_enters_ordinary_wake_even_from_old_queue(tmp_path):
    import json
    from collab.protocol import Envelope
    from collab.remote_requests import wire_text
    waker = wake.Waker(tmp_path, 's_room')
    wake.write_config(tmp_path, wake.WakeConfig(command=['unused'], delivery='prompt'))
    offer = make_offer(session_id='s_room', sender_id='p_peer', recipient_id='p_local',
                       target='coding', kind='goal', text='UNAPPROVED_SENTINEL')
    env = Envelope(seq=2, kind='chat', sender='peer', text=wire_text(offer))
    assert waker.note(env) is False
    assert waker.take() is None
    waker.ensure()
    waker.pending.write_text(json.dumps({'seq': 2, 'kind': 'chat', 'text': env.text}) + '\n')
    assert waker.take() is None
    queued = waker.batches / 'old.jsonl'
    queued.write_text(json.dumps({'seq': 2, 'kind': 'chat', 'text': env.text}) + '\n')
    assert 'UNAPPROVED_SENTINEL' not in waker.turn_prompt(wake.Batch(queued))
    assert waker.take() is None
