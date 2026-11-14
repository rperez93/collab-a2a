from remote_consent_helpers import receipt
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


def test_agent_accept_cannot_use_old_flag_or_hash(local, monkeypatch):
    from collab import consent
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    def refuse(*args, **kwargs):
        raise consent.ConsentRefused('agent terminal refused')
    monkeypatch.setattr(consent, 'request', refuse)
    assert cli.main(['remote', 'accept', offer['id']]) == 1
    for flags in (['--human-approved'], ['--sha256', digest(offer)],
                  ['--human-approved', '--sha256', digest(offer)]):
        with pytest.raises(SystemExit) as error:
            cli.main(['remote', 'accept', offer['id'], *flags])
        assert error.value.code == 2
    assert inbox.list()[0]['state'] == 'pending'


def test_receipt_acceptance_and_decline(local, monkeypatch):
    from collab import consent
    from collab.remote_delivery import working_directory
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    command = list(wake.read_config(inbox.root.parent).command)
    def synthetic_review(root, **kwargs):
        assert kwargs['review']['offer']['text'] == offer['text']
        assert kwargs['review']['local_command'] == command
        return receipt(inbox, offer, 'coding', command, kwargs['review']['local_cwd'])
    monkeypatch.setattr(consent, 'request', synthetic_review)
    assert cli.main(['remote', 'accept', offer['id']]) == 0
    assert inbox.list()[0]['state'] == 'accepted'
    assert cli.main(['remote', 'decline', offer['id']]) == 0
    assert inbox.list()[0]['state'] == 'declined'


def test_changed_local_command_refuses_acceptance(local):
    inbox, offer = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    wake.write_config(inbox.root.parent, wake.WakeConfig(command=['other-adapter']))
    assert cli.main(['remote', 'accept', offer['id']]) == 1
    assert inbox.list()[0]['state'] == 'pending'


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


def test_review_observing_disarm_permanently_revokes_prior_acceptance(local, monkeypatch):
    from collab import consent
    inbox, first = local
    cli.main(['remote', 'bind', '--target', 'coding'])
    command = list(wake.read_config(inbox.root.parent).command)
    def review(root, **kwargs):
        selected = next(row for row in inbox.list() if row['id'] == kwargs['request_id'])
        # The digest covers only the original offer fields.
        selected = {k: v for k, v in selected.items() if k not in ('state', 'sha256')}
        approval = receipt(inbox, selected, 'coding', command, kwargs['review']['local_cwd'])
        if selected['id'] != first['id']:
            wake.write_config(root, wake.WakeConfig(command=[]))
        return approval
    monkeypatch.setattr(consent, 'request', review)
    assert cli.main(['remote', 'accept', first['id']]) == 0
    second = make_offer(session_id='s_room', sender_id='p_peer', recipient_id='p_local',
                        target='coding', kind='prompt', text='second review')
    inbox.receive(second, session_id='s_room', sender_id='p_peer', recipient_id='p_local')
    assert cli.main(['remote', 'accept', second['id']]) == 1
    wake.write_config(inbox.root.parent, wake.WakeConfig(command=command))
    assert {r['id']: r['state'] for r in inbox.list()} == {first['id']: 'failed', second['id']: 'pending'}
    assert inbox.claim(session_id='s_room', recipient_id='p_local', target='coding', command=command) is None
