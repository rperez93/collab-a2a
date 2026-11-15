"""Delivering a human-approved remote prompt on the receiver's own listen
stream (`wake set --agent claude`): printed once, never typed anywhere, and
delivered only when written. Every id and text here is invented."""
import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from collab import remote_delivery, wake
from collab.remote_requests import Inbox, delivery_sha256, digest, make_offer
from collab.server.approval_remote import delivery_binding


def listen_command():
    return [shutil.which('collab') or '/usr/bin/collab', 'wake', 'deliver', '--to', 'listen',
            '--target', wake.LISTEN_TARGET]


@pytest.fixture
def approved(tmp_path, monkeypatch):
    command = listen_command()
    # Delivery tests run without the installed binary's identity check; the
    # parser itself is tested below against the real recipe.
    real = remote_delivery.destination
    monkeypatch.setattr(remote_delivery, 'destination',
                        lambda c: {'adapter': 'listen', 'session_id': wake.LISTEN_TARGET, 'scope': 'listen-stream'}
                        if c == command else real(c))
    profile = SimpleNamespace(dir=tmp_path, session_id='s_room', participant_id='p_local')
    wake.write_config(tmp_path, wake.WakeConfig(command=command, timeout=2))
    inbox = Inbox(tmp_path)
    offer = make_offer(session_id='s_room', sender_id='p_peer', recipient_id='p_local',
                       target=wake.LISTEN_TARGET, kind='prompt', text='Run the tests and say what failed.')
    inbox.receive(offer, session_id='s_room', sender_id='p_peer', recipient_id='p_local', source_seq=1)
    inbox.bind(wake.LISTEN_TARGET, command)
    options = dict(reviewed_digest=digest(offer), session_id='s_room', recipient_id='p_local',
                   target=wake.LISTEN_TARGET, command=command)
    attempt = inbox.begin_strong_attempt(offer['id'], **options)
    inbox.finish_strong_attempt(offer['id'], attempt, 'rc_' + offer['id'], **options,
                                delivery_digest=delivery_sha256(offer, wake.LISTEN_TARGET))
    return profile, inbox, offer


async def _with_monitor(profile, prepared, *, mark=True):
    """Deliver while a monitor polls the stream, as `collab listen` does."""
    shown = []

    async def monitor():
        for _ in range(40):
            for record in remote_delivery.claim_for_stream(profile.dir):
                shown.append(remote_delivery.stream_line(record, False))
                if mark:
                    remote_delivery.mark_printed(profile.dir, record['request_id'])
            await asyncio.sleep(0.05)
    watcher = asyncio.create_task(monitor())
    result = await remote_delivery.deliver(profile, prepared)
    watcher.cancel()
    return result, shown


def test_the_prompt_is_printed_once_on_the_stream_and_counts_as_delivered(approved):
    profile, inbox, offer = approved
    result, shown = asyncio.run(_with_monitor(profile, remote_delivery.prepare(profile)))
    assert result is True and len(shown) == 1
    line = shown[0]
    assert line.startswith(f"[remote prompt {offer['id']}] approved by your human")
    assert f'collab remote reply {offer["id"]}' in line
    assert json.dumps(offer['text'])[1:-1] in line and '\n' not in line
    assert inbox.list()[0]['state'] == 'delivered'
    assert [r['state'] for r in inbox.owed()[0]] == ['delivered']
    assert remote_delivery.claim_for_stream(profile.dir) == []        # never twice


def test_with_no_monitor_it_is_withdrawn_unprinted_and_reported_failed(approved):
    profile, inbox, offer = approved
    assert asyncio.run(remote_delivery.deliver(profile, remote_delivery.prepare(profile))) is False
    assert inbox.list()[0]['state'] == 'failed'
    assert [r['state'] for r in inbox.owed()[0]] == ['failed']
    # A monitor that starts later never prints a withdrawn prompt.
    assert remote_delivery.claim_for_stream(profile.dir) == []
    assert remote_delivery.prepare(profile) is None                   # never relaunched


def test_a_monitor_that_took_it_but_never_confirmed_is_uncertain(approved, monkeypatch):
    profile, inbox, offer = approved
    monkeypatch.setattr(remote_delivery, 'STREAM_GRACE', 0.3)
    result, shown = asyncio.run(_with_monitor(profile, remote_delivery.prepare(profile), mark=False))
    assert result is False and len(shown) == 1
    assert inbox.list()[0]['state'] == 'uncertain'


def test_the_recipe_and_the_hub_accept_the_listen_destination(monkeypatch):
    recipe = wake.recipe('claude')
    assert recipe.delivers == wake.OPEN_SESSION and not recipe.needs_target
    command = recipe.command(collab=shutil.which('collab') or '/usr/bin/collab')
    assert command[1:] == ['wake', 'deliver', '--to', 'listen', '--target', wake.LISTEN_TARGET]
    if shutil.which('collab'):
        assert remote_delivery.destination(command) == {
            'adapter': 'listen', 'session_id': wake.LISTEN_TARGET, 'scope': 'listen-stream'}
    assert delivery_binding({'target': wake.LISTEN_TARGET, 'command': command, 'cwd': '/home/dev/project',
                             'destination': {'adapter': 'listen', 'session_id': wake.LISTEN_TARGET,
                                             'scope': 'listen-stream'}})


def test_wake_deliver_to_listen_types_nothing_and_succeeds(monkeypatch, capsys):
    from collab import cli
    monkeypatch.setenv('COLLAB_WAKE_PROMPT', '')
    monkeypatch.setattr('sys.stdin', SimpleNamespace(isatty=lambda: False, read=lambda: 'a batch'))
    assert cli.main(['wake', 'deliver', '--to', 'listen', '--target', wake.LISTEN_TARGET]) == 0


def test_a_forged_stream_file_is_not_printed(tmp_path):
    folder = remote_delivery.stream_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / 'rq_abc.json').write_text(json.dumps({'request_id': 'rq_other', 'text': 'x',
                                                    'sender_id': 'p', 'reply': 'r'}))
    (folder / 'rq_../x.json'.replace('/', '_')).write_text('{}')
    assert remote_delivery.claim_for_stream(tmp_path) == []


def test_bind_needs_no_target_for_the_listen_stream(tmp_path, monkeypatch, capsys):
    from collab import cli
    profile = SimpleNamespace(dir=tmp_path, session_id='s_room', participant_id='p_local')
    monkeypatch.setattr(cli, '_require_profile', lambda args: profile)
    command = listen_command()
    from collab import remote_cli
    monkeypatch.setattr(remote_cli, 'destination',
                        lambda c: {'adapter': 'listen', 'session_id': wake.LISTEN_TARGET, 'scope': 'listen-stream'})
    wake.write_config(tmp_path, wake.WakeConfig(command=command))
    assert cli.main(['remote', 'bind']) == 0
    assert Inbox(tmp_path).bound_target(command) == wake.LISTEN_TARGET
