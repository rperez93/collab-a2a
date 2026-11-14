"""Skill coordination shares the durable worker voice without weakening identity."""
import argparse
import asyncio
import contextlib
from types import SimpleNamespace

import httpx
import pytest

from collab import cli, consent, worker
from collab.client.hub_client import HubClient, HubError
from collab.client.worker_service import Conversation
from collab.config import SessionProfile
from collab.protocol import Envelope


@pytest.fixture
def setup_queue(tmp_path, monkeypatch, session, client):
    """Use the real CLI/client/hub with only listener launch and receipts stubbed."""
    monkeypatch.delenv('COLLAB_NO_WORKER', raising=False)
    store = session['store']
    bob = store.add_participant('bob', 'bob-token')
    old = store.participant_by_id(store.resolve_name('alice'))
    replacement = store.add_participant('mallory', 'mallory-token')
    profile = SessionProfile('s_test', 'http://testserver', 'bob', 'alice', 'bob-token',
                             home=str(tmp_path/'receiver'), participant_id=bob.id)
    request_id = 'sr_00000000aaaa'
    store.append(Envelope(kind='chat', sender='alice', sender_id=old.id, to='bob', to_id=bob.id,
        text='request', body={'skill_request': {'id': request_id, 'skill': 'review', 'args': 'private'}}))
    outbox = worker.Store(profile.dir)
    outbox.configure({'agent': 'command', 'command': ['unused-test-adapter'], 'scope': 'test'})
    older = outbox.send('older queued message', to='bob')
    hub = HubClient(profile.url, profile.token)
    hub._client.close()
    hub._client = client
    hub._verified_url = profile.url
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: profile)
    monkeypatch.setattr(cli, '_client', lambda p: contextlib.nullcontext(hub))
    monkeypatch.setattr(cli.onboard, 'ensure_daemon', lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(consent, 'request', lambda *a, **k: consent.Receipt('synthetic-test'))
    def consume(*a, **k):
        assert len(outbox.outbox()) == 1, 'consume authority before adding acceptance to queue'
        calls.append('consume')
    monkeypatch.setattr(consent, 'consume', consume)
    monkeypatch.setattr(consent, 'audit', lambda *a, **k: calls.append(k['event']))
    return SimpleNamespace(profile=profile, outbox=outbox, older=older, store=store,
        old=old, replacement=replacement, request_id=request_id, hub=hub, calls=calls)


@pytest.mark.parametrize('action', ['ask', 'decline', 'done', 'accept'])
def test_actual_skill_cli_appends_after_old_queue_without_direct_send(setup_queue, session, action, capsys):
    """Enabled-worker skill verbs used to speak directly ahead of older queued messages."""
    q = setup_queue
    if action == 'done':
        q.store.append(Envelope(kind='chat', sender='bob', sender_id=q.profile.participant_id,
            to='alice', to_id=q.old.id, text='accepted',
            body={'skill_response': {'id': q.request_id, 'state': 'accepted'}}))
    before = len(q.store.since(0))
    args = argparse.Namespace(action=action, target='review' if action=='ask' else q.request_id,
        to='alice', args='new arguments', note='private answer', json=False)
    assert cli.cmd_skills_remote(args) == 0
    assert len(q.store.since(0)) == before, 'queueing must not make a direct network write'
    rows = q.outbox.outbox()
    assert [r['id'] for r in rows[:1]] == [q.older] and len(rows) == 2
    if action == 'ask':
        assert rows[1]['body']['skill_request']['args'] == 'new arguments'
        assert rows[1].get('delivery') is None
    else:
        assert rows[1]['delivery'] == 'to-id' and rows[1]['to_id'] == q.old.id
        assert rows[1]['body']['skill_response']['state'] == {'accept':'accepted','decline':'declined'}.get(action, action)
    text = capsys.readouterr().out
    assert 'queued' in text
    if action == 'accept':
        assert q.calls == ['consume', 'authorized', 'queued']
        assert 'running it now' not in rows[1]['text'] and 'now run the skill' not in text
    # The stored original ID survives a name swap before actual async delivery.
    q.store.rename(q.old.id, 'old-alice')
    q.store.rename(q.replacement.id, 'alice')
    trace = []
    async def flush():
        asgi = httpx.ASGITransport(app=session['app'])
        async def transport(request):
            trace.append(request.url.path)
            return await asgi.handle_async_request(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            service = Conversation(SimpleNamespace(profile=q.profile, _http=http))
            await service._flush(q.outbox, q.outbox.snapshot()['generation'])
    asyncio.run(flush())
    assert not q.outbox.outbox()
    assert trace == ['/ext/collab/v1/messages', '/ext/collab/v1/messages' if action=='ask' else '/ext/collab/v1/messages/to-id']
    events = [e for e in q.store.since(0) if e.seq > before]
    assert events[0].text == 'older queued message'
    if action != 'ask':
        assert events[-1].to_id == q.old.id


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('refused', [False, True])
def test_by_id_outbox_retains_thread_body_identity_and_refused_delivery(setup_queue, monkeypatch, manual, refused):
    """No old-hub refusal may drop or name-retarget a queued stable-ID response."""
    q=setup_queue
    q.outbox.acknowledge(q.older)
    record=q.outbox.send('response', to='alice', to_id=q.old.id, thread='t1', body={'skill_response': {'state':'declined'}})
    seen=[]
    def record_request(path, body):
        seen.append((path, body))
    if manual:
        def request(method,path,**kwargs):
            record_request(path,kwargs['json'])
            if refused: raise HubError('404 old hub')
            return {'to_id':q.old.id}
        monkeypatch.setattr(q.hub,'_request',request)
        monkeypatch.setattr(q.hub,'send',lambda *a:pytest.fail('must never use ordinary send'))
        assert cli.cmd_worker(argparse.Namespace(worker_action='off'))==0
    else:
        async def go():
            async def transport(request):
                import json
                record_request(request.url.path,json.loads(request.content))
                return httpx.Response(404 if refused else 200)
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
                service=Conversation(SimpleNamespace(profile=q.profile,_http=http))
                await service._flush(q.outbox,q.outbox.snapshot()['generation'])
                if refused:
                    await service._flush(q.outbox,q.outbox.snapshot()['generation'])
        asyncio.run(go())
    assert len(seen)==1 and seen[0][0]=='/ext/collab/v1/messages/to-id'
    assert seen[0][1]=={'text':'response','to_id':q.old.id,'thread':'t1',
        'body':{'skill_response':{'state':'declined'},'collab_worker_delivery':record}}
    assert bool(q.outbox.outbox()) is refused
    if refused:
        row=q.outbox.outbox()[0]
        assert row['id']==record and row['to_id']==q.old.id and row['thread']=='t1'
        if not manual: assert row['retry_at']>0


def test_delivery_modes_cannot_confuse_r4_and_r7(setup_queue):
    """A stable reply cannot simultaneously claim the remote-offer capability route."""
    with pytest.raises(ValueError):
        setup_queue.outbox.send('bad',to='alice',to_id=setup_queue.old.id,recipient_id=setup_queue.old.id)


def test_failed_acceptance_queue_records_consumed_authority_without_delivery(setup_queue, monkeypatch):
    """A full outbox must not turn consumed consent into a delivered/running claim."""
    q=setup_queue
    monkeypatch.setattr(worker,'MAX_PENDING',1)
    assert cli.cmd_skills_remote(argparse.Namespace(action='accept',target=q.request_id,
        to=None,args=None,note='',json=False))==1
    assert [row['id'] for row in q.outbox.outbox()]==[q.older]
    assert q.calls==['consume','authorized','queue-failed']
