import json
from types import SimpleNamespace

import pytest

from collab import cli, wake
from collab.remote_requests import Inbox, digest, make_offer


@pytest.fixture
def local(tmp_path, monkeypatch):
    profile = SimpleNamespace(dir=tmp_path, session_id='s_room', participant_id='p_local')
    monkeypatch.setattr(cli, '_require_profile', lambda args: profile)
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: profile)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    wake.write_config(tmp_path, wake.WakeConfig(command=['local-adapter', '--session', 'coding']))
    offer = make_offer(session_id='s_room', sender_id='p_peer', recipient_id='p_local',
                       target='coding', kind='prompt', text='Review this \x1b[31m text.')
    inbox = Inbox(tmp_path)
    inbox.receive(offer, session_id='s_room', sender_id='p_peer', recipient_id='p_local')
    return inbox, offer


def test_cli_review_and_binding_do_not_authorize(local, capsys):
    inbox, offer = local
    assert cli.main(['remote', 'bind', '--target', 'coding']) == 0
    assert cli.main(['remote', 'show', offer['id']]) == 0
    output = capsys.readouterr().out
    assert '\x1b' not in output
    assert '\\u001b' in output
    assert digest(offer) in output
    assert inbox.list()[0]['state'] == 'pending'


def test_noninteractive_accept_requires_consent_and_review_hash(local):
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    assert cli.main(['remote', 'accept', offer['id']]) == 1
    assert cli.main(['remote', 'accept', offer['id'], '--human-approved']) == 1
    assert cli.main(['remote', 'accept', offer['id'], '--human-approved', '--sha256', 'wrong']) == 1
    assert inbox.list()[0]['state'] == 'pending'
    assert cli.main(['remote', 'accept', offer['id'], '--human-approved', '--sha256', digest(offer)]) == 0
    assert inbox.list()[0]['state'] == 'accepted'


def test_changed_local_command_refuses_acceptance(local):
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    wake.write_config(inbox.root.parent, wake.WakeConfig(command=['other-adapter']))
    assert cli.main(['remote', 'accept', offer['id'], '--human-approved', '--sha256', digest(offer)]) == 1
    assert inbox.list()[0]['state'] == 'pending'


def test_decline_revokes_accepted_request(local):
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    cli.main(['remote', 'accept', offer['id'], '--human-approved', '--sha256', digest(offer)])
    assert cli.main(['remote', 'decline', offer['id']]) == 0
    assert inbox.list()[0]['state'] == 'declined'


def test_offer_queues_exact_stable_identity_without_acceptance(local, monkeypatch, capsys):
    from collab.worker import Store
    inbox, received = local
    store = Store(inbox.root.parent)
    store.configure({'agent': 'command', 'command': ['unused-test-adapter'], 'scope': 'test'})
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def snapshot(self):
            return {'participants': [{'id': 'p_peer', 'name': 'peer'}]}
    monkeypatch.setattr(cli, '_client', lambda profile: Client())
    assert cli.main(['remote', 'offer', '--to', 'peer', '--target', 'remote-coding',
                     '--text', 'Please review this.', '--kind', 'goal']) == 0
    queued = store.outbox()[0]
    assert queued['recipient_id'] == 'p_peer'
    assert queued['to'] == 'peer'
    assert 'Please review this.' in queued['text']
    assert inbox.list()[0]['state'] == 'pending'
