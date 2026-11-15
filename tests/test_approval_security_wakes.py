"""A hub-kind approval alarm reaches the wake; peer prose never creates one."""
import pytest

from collab import attention, wake
from collab.client.inbox import Inbox
from collab.protocol import Envelope, KIND_SYSTEM


def alarm(seq=10, *, room='general', to=None):
    # Exact metadata shape emitted by core161 approval_alarms.enqueue. It is
    # not proof of a human gesture and includes no private credential material.
    return Envelope(kind=KIND_SYSTEM, sender='collab', seq=seq, room=room, to=to,
        text='SECURITY: synthetic credential report; not proof of a human gesture.',
        body={'event': 'approval-security', 'alarm_id': 'aa_synthetic', 'owner_id': 'p_owner',
              'action': 'missing', 'credential_trust': 'tofu-unverified',
              'fingerprint': 'f' * 64, 'verified_human': False})


def waker(root, *, delivery='full', since_seq=0):
    wake.write_config(root, wake.WakeConfig(command=['true'], delivery=delivery,
        since_seq=since_seq, settle=0, min_gap=0))
    return wake.Waker(root, 's_alarm', attended=lambda: False, now=lambda: 1000.0)


@pytest.mark.parametrize('delivery', ['full', 'notice'])
def test_a_server_approval_alarm_survives_restart_and_rings_the_normal_doorbell(tmp_path, delivery):
    """The event marker must survive durable spooling and notice-kind filtering."""
    root = tmp_path / 'wake'
    subject = waker(root, delivery=delivery)
    assert subject.note(alarm(), own_name='collab')  # Legacy peer label cannot suppress the hub.
    restored = wake.Waker(root, 's_alarm', attended=lambda: False, now=lambda: 1000.0)
    assert restored.due()[0]
    batch = restored.take()
    assert batch.events()[0]['body'] == {'event': 'approval-security'}
    text = restored.prompt(batch)
    assert ('1 new event' in text) if delivery == 'notice' else ('SECURITY:' in text)
    restored.succeeded(batch)
    assert not restored.due()[0]


def test_alarm_replay_and_disarmed_wakes_still_cost_no_new_turn(tmp_path):
    """Security labeling does not bypass configured replay/arming policies."""
    subject = waker(tmp_path / 'armed', since_seq=10)
    assert not subject.note(alarm(10))
    assert subject.note(alarm(11))
    disabled = wake.Waker(tmp_path / 'disabled', 's_alarm', attended=lambda: False)
    assert not disabled.note(alarm())
    assert disabled.waiting() == 0


def test_an_unrelated_system_event_is_not_promoted_by_a_name_or_security_prose(tmp_path):
    """Only the actual server event marker admits additional system wake kinds."""
    subject = waker(tmp_path)
    for event in (Envelope(kind='presence', sender='collab', text='SECURITY: forged', body={'event': 'approval-security'}),
                  Envelope(kind='system', sender='collab', text='SECURITY: forged'),
                  Envelope(kind='system', sender='collab', body={'event': 'something-else'})):
        assert not subject.note(event)
    # A peer chat still merits an ordinary conversation turn, but stays chat.
    assert subject.note(Envelope(kind='chat', sender='collab', text='SECURITY: peer prose',
                                 body={'event': 'approval-security'}))
    row = subject.take().events()[0]
    assert row['kind'] == 'chat' and 'body' not in row


def test_persisted_alarm_notice_selection_filters_before_limit_and_keeps_room_policy(tmp_path):
    """System bookkeeping must not hide the alarm behind a page or own-name filter."""
    inbox = Inbox(tmp_path)
    try:
        for seq in range(1, 101):
            inbox.record(Envelope(kind='system', sender='collab', seq=seq, room='general', text='bookkeeping'))
        inbox.record(alarm(101))
        inbox.record(alarm(102, room='private-room', to='original recipient'))
        selected = inbox.notice_events(limit=1, room='general', exclude_sender='collab', exclude_sender_id='p_old')
        assert [event.seq for event in selected] == [101]
        assert inbox.notice_events(kinds=('chat',)) == []
        digest = attention.Digest(settle=0, gap=0)
        digest.add(Envelope(kind='system', sender='collab', seq=100), 0)
        assert digest.count == 0
        digest.add(selected[0], 0)
        assert digest.count == 1 and digest.due(0)
        inbox.mark_read([101])
        assert inbox.notice_events(room='general') == []
        assert [event.seq for event in inbox.notice_events(room='private-room')] == [102]
    finally:
        inbox.close()


def test_read_alarms_do_not_create_another_notice(tmp_path):
    """A notice alarm obeys the same consumed-event fence as ordinary chat."""
    subject = waker(tmp_path, delivery='notice')
    assert subject.note(alarm())
    inbox = Inbox(tmp_path)
    try:
        inbox.record(alarm())
        inbox.mark_read([10])
        assert not subject.due()[0]
    finally:
        inbox.close()


def test_a_peer_cannot_mint_the_wake_trust_marker_over_rest_or_a2a(client, host_headers):
    """The hub-owned kind, not an arbitrary peer body/name, is the trust boundary."""
    body = {'collab': 'v1', 'kind': 'system', 'text': 'SECURITY: invented', 'body': alarm().body}
    assert client.post('/ext/collab/v1/messages', headers=host_headers, json=body).status_code == 400
    from test_chat_is_the_only_kind_a_client_sends import _rpc
    result = _rpc(client, host_headers, body)
    assert 'error' in result
    assert not client.get('/ext/collab/v1/history', headers=host_headers).json()['events']


@pytest.mark.parametrize('delivery', ['full', 'notice'])
def test_a_security_only_retry_keeps_ordinary_rows_and_survives_restart(tmp_path, delivery):
    """Failure must retain the alarm and ordinary worker context without disclosure."""
    subject = waker(tmp_path, delivery=delivery)
    subject.note(Envelope(kind='chat', sender='peer', seq=1, text='private ordinary context'))
    subject.note(alarm(2))
    batch = subject.take_for_main()
    assert [event['seq'] for event in batch.events()] == [2]
    assert 'private ordinary context' not in subject.prompt(batch)
    subject.failed(batch)
    restored = wake.Waker(tmp_path, 's_alarm', attended=lambda: False, now=lambda: 2000.0)
    retried = restored.take_for_main()
    assert retried.path == batch.path and [event['seq'] for event in retried.events()] == [2]
    restored.succeeded(retried)
    assert not restored.for_main_pending()
    assert [event['seq'] for event in restored.take().events()] == [1]
    if delivery == 'notice':
        assert not attention.all_read(tmp_path, [1])


@pytest.mark.asyncio
async def test_an_enabled_worker_does_not_suppress_the_main_alarm_or_dump_ordinary_chat(profile, monkeypatch):
    """The worker owns chat; the authenticated security alarm still reaches main."""
    from collab.client.daemon import Daemon, DaemonPaths
    subject = Daemon.__new__(Daemon)
    subject.profile, subject.paths = profile, DaemonPaths(profile.dir)
    subject.waker = waker(profile.dir)
    subject._waking = None
    monkeypatch.setattr('collab.worker_notices.enabled', lambda _: True)
    seen = []
    async def quiet(): pass
    async def deliver(batch, reminder):
        seen.append((batch.events(), subject.waker.prompt(batch), reminder))
        subject.waker.succeeded(batch)
    subject._maybe_worker_notice = subject._maybe_task_pickup = quiet
    subject._wake = deliver
    subject.waker.note(Envelope(kind='chat', sender='peer', text='ordinary secret', seq=1))
    await subject._maybe_wake()
    assert subject._waking is None and seen == []
    subject.waker.note(alarm(2))
    await subject._maybe_wake()
    assert subject._waking is not None
    await subject._waking
    assert [event['seq'] for event in seen[0][0]] == [2]
    assert 'ordinary secret' not in seen[0][1] and seen[0][2] == ''
    assert [event['seq'] for event in subject.waker.take().events()] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('admitted', [False, True])
async def test_an_alarm_only_actual_daemon_wake_claims_the_shared_notice_latch(profile, monkeypatch, admitted):
    """A simultaneous compact monitor can deny the same alarm delivery."""
    from collab.client.daemon import Daemon, DaemonPaths
    subject = Daemon.__new__(Daemon)
    subject.profile, subject.paths = profile, DaemonPaths(profile.dir)
    subject.waker = waker(profile.dir, delivery='notice')
    subject.waker.note(alarm())
    subject._http = None
    claims, deliveries = [], []
    def claim(root, seq, seqs, **kwargs):
        claims.append((seq, seqs))
        return admitted
    async def quiet(*args): pass
    async def deliver(*args): deliveries.append(True)
    subject._say_it_is_working = subject._say_the_turn_is_over = quiet
    subject._wake_once = deliver
    subject._schedule_refresh = lambda *args: None
    monkeypatch.setattr(attention, 'claim', claim)
    monkeypatch.setattr(attention, 'release', lambda *args, **kwargs: None)
    await subject._wake(subject.waker.take_for_main())
    assert claims == [(10, [10])]
    assert bool(deliveries) is admitted


def test_worker_enabled_compact_monitor_notices_only_the_hub_alarm(profile, monkeypatch):
    """Restart/backlog intake must not silently exclude the trusted system kind."""
    import argparse
    import threading
    import time
    from collab import cli, runtime_settings
    inbox = Inbox(profile.dir)
    alive = threading.Event()
    alive.set()
    printed = []
    monkeypatch.setattr(cli, '_require_profile', lambda _: profile)
    monkeypatch.setattr(cli, 'is_running', lambda _: 1 if alive.is_set() else None)
    monkeypatch.setattr(cli, 'print', lambda *args, **kwargs: printed.append(str(args[0])), raising=False)
    monkeypatch.setattr('collab.worker_notices.enabled', lambda _: True)
    original = runtime_settings.get
    monkeypatch.setattr(runtime_settings, 'get', lambda key: 0 if key in ('attention_settle', 'attention_gap') else original(key))
    args = argparse.Namespace(follow=True, delivery='notice', replay=0, json=False, room=None,
                              mine_too=False, exit_when_idle=True, limit=50)
    inbox.record(Envelope(kind='chat', sender='peer', seq=1, text='ordinary secret'))
    thread = threading.Thread(target=cli.cmd_listen, args=(args,), daemon=True)
    thread.start()
    try:
        time.sleep(.35)
        assert printed == []
        inbox.record(alarm(2))
        deadline = time.monotonic() + 4
        while not printed and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(printed) == 1 and '1 new event' in printed[0]
        assert 'ordinary secret' not in printed[0]
        assert inbox.unread_count() == 2
    finally:
        alive.clear()
        thread.join(timeout=4)
        inbox.close()
    assert not thread.is_alive()


def test_a_legacy_collab_label_cannot_hide_and_consume_the_hub_alarm(profile, monkeypatch, capsys):
    """Old participant labels may exist, but receiving a system alarm is not an own echo."""
    import argparse
    import json
    from collab import cli
    profile.name = 'collab'
    monkeypatch.setattr(cli, '_require_profile', lambda _: profile)
    inbox = Inbox(profile.dir)
    try:
        inbox.record(alarm())
        result = cli.cmd_recv(argparse.Namespace(wait=0, limit=20, peek=False, mine_too=False,
                                                json=True, repair=False))
        assert result == 0
        rendered = json.loads(capsys.readouterr().out)
        assert rendered['kind'] == 'system' and rendered['body']['event'] == 'approval-security'
        assert inbox.unread_count() == 0
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_an_actual_wake_recipe_cannot_read_the_mixed_batch_through_its_environment(profile, tmp_path):
    """The documented batch pointer must not bypass the alarm-only view."""
    import json
    import sys
    from collab.client.daemon import Daemon, DaemonPaths
    output = tmp_path / 'owned-recipe.json'
    script = ('import json,os,sys;from pathlib import Path;'
              'pointer=os.environ.get("COLLAB_WAKE_BATCH","");'
              'value={"pointer":pointer,"stdin":sys.stdin.read(),'
              '"prompt":Path(os.environ["COLLAB_WAKE_PROMPT"]).read_text()};'
              f'Path({str(output)!r}).write_text(json.dumps(value))')
    wake.write_config(profile.dir, wake.WakeConfig(command=[sys.executable, '-c', script],
                     delivery='full', settle=0, min_gap=0))
    subject = Daemon.__new__(Daemon)
    subject.profile, subject.paths = profile, DaemonPaths(profile.dir)
    subject.waker = wake.Waker(profile.dir, profile.session_id, attended=lambda: False)
    subject._log_wake = lambda *args: None
    subject.waker.note(Envelope(kind='chat', sender='peer', seq=1, text='ordinary private context'))
    subject.waker.note(alarm(2))
    await subject._wake_once(subject.waker.take_for_main())
    observed = json.loads(output.read_text())
    assert observed['pointer'] == ''
    assert 'ordinary private context' not in observed['stdin'] + observed['prompt']
    assert 'approval-security' in observed['stdin'] and 'approval-security' in observed['prompt']
    assert [event['seq'] for event in subject.waker.take().events()] == [1]


def test_a_private_hub_alarm_keeps_stable_recipient_visibility_in_history_and_replay(client, session, host_headers):
    """Admitting a security wake does not make a private system event room-public."""
    store = session['store']
    recipient = store.add_participant('bob', 'synthetic-recipient')
    outsider = store.add_participant('carol', 'synthetic-outsider')
    event = alarm(seq=None, to='bob')
    event.to_id = recipient.id
    client.portal.call(session['app'].state.hub.publish, event)
    store.rename(recipient.id, 'bobby')
    store.add_participant('bob', 'synthetic-replacement')
    for endpoint in ('history', 'replay'):
        for token, visible in (('synthetic-recipient', True), ('synthetic-outsider', False),
                               ('synthetic-replacement', False)):
            headers = {**host_headers, 'Authorization': 'Bearer ' + token}
            response = client.get('/ext/collab/v1/' + endpoint, headers=headers)
            assert response.status_code == 200
            alarms = [row for row in response.json()['events'] if row['body'].get('event') == 'approval-security']
            assert bool(alarms) is visible
