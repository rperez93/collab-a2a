"""Recycled labels must never transfer stored file permissions."""
import io
from pathlib import Path

import pytest

from collab.server.store import Store

PREFIX = '/ext/collab/v1'


def join(client, session, name):
    r = client.post(PREFIX+'/join', json={'protocol_major':2,'version':'2.0.0',
        'invite':session['invite'],'name':name,'hello':{}})
    assert r.status_code == 200
    return {'Collab-Protocol-Major':'2','Collab-Version':'2.0.0',
            'Authorization':'Bearer '+r.json()['token']}


def upload(client, sender, recipient=None, **params):
    if recipient:params['to']=recipient
    r=client.post(PREFIX+'/files',headers=sender,params=params,
                  files={'file':('private.txt',io.BytesIO(b'synthetic private bytes'))})
    assert r.status_code==200,r.text
    return r.json()


def rename(client, headers, label):
    r=client.post(PREFIX+'/rename',headers=headers,json={'name':label})
    assert r.status_code==200,r.text


def test_a_recipient_rename_swap_preserves_download_listing_and_ack(client, session, host_headers):
    """A different token taking the old recipient label receives no file access."""
    bob=join(client,session,'bob');carol=join(client,session,'carol')
    record=upload(client,host_headers,'bob')
    rename(client,bob,'bobby');rename(client,carol,'bob')
    endpoint=PREFIX+'/files/'+record['id']
    assert client.get(endpoint+'/content',headers=carol).status_code==403
    assert client.post(endpoint+'/ack',headers=carol).status_code==403
    assert record['id'] not in [f['id'] for f in client.get(PREFIX+'/files',headers=carol).json()['files']]
    assert client.get(endpoint+'/content',headers=bob).content==b'synthetic private bytes'
    assert record['id'] in [f['id'] for f in client.get(PREFIX+'/files',headers=bob).json()['files']]
    assert client.post(endpoint+'/ack',headers=bob).status_code==200


def test_a_sender_rename_swap_does_not_transfer_withdrawal_or_receipts(client, session, host_headers):
    """Only the original sender identity retains deletion and private receipts."""
    bob=join(client,session,'bob');carol=join(client,session,'carol')
    record=upload(client,bob,'alice')
    rename(client,bob,'bobby');rename(client,carol,'bob')
    endpoint=PREFIX+'/files/'+record['id']
    assert client.delete(endpoint,headers=carol).status_code==403
    assert client.get(endpoint+'/content',headers=bob).status_code==200
    assert client.post(endpoint+'/ack',headers=host_headers).status_code==200
    def received(headers):
        return [e for e in client.get(PREFIX+'/history',headers=headers).json()['events']
                if e['kind']=='file' and e['body'].get('action')=='received' and e['body'].get('id')==record['id']]
    assert received(bob)
    assert not received(carol)
    remaining=upload(client,bob,'alice')
    assert client.delete(PREFIX+'/files/'+remaining['id'],headers=bob).status_code==200


@pytest.mark.parametrize('evidence', [True,False])
def test_old_file_migration_never_uses_the_current_holder_of_a_recycled_name(client, session, host_headers, evidence):
    """Only the original ID-bearing announcement can recover an older private row."""
    bob=join(client,session,'bob');carol=join(client,session,'carol')
    record=upload(client,host_headers,'bob');store=session['store']
    original=record['recipient_id']
    rename(client,bob,'bobby');rename(client,carol,'bob')
    path=store.path
    # Recreate only the historical file-table shape, in the owned fixture DB.
    with store._lock:
        store._db.execute('ALTER TABLE files DROP COLUMN sender_id')
        store._db.execute('ALTER TABLE files DROP COLUMN recipient_id')
        if not evidence:store._db.execute("DELETE FROM events WHERE kind='file'")
        store._db.commit()
    migrated=Store(path)
    try:
        row=migrated.get_file(record['id'])
        assert row['recipient_id']==(original if evidence else None)
        assert row['sender_id']==(record['sender_id'] if evidence else None)
    finally:migrated.close()
    # Reopening cannot opportunistically rebind an unresolved legacy label.
    again=Store(path)
    try:assert again.get_file(record['id'])['recipient_id']==(original if evidence else None)
    finally:again.close()
    endpoint=PREFIX+'/files/'+record['id']+'/content'
    assert client.get(endpoint,headers=carol).status_code==403
    assert client.get(endpoint,headers=bob).status_code==(200 if evidence else 403)


def test_the_real_hub_client_pins_a_reply_to_the_original_id(live_server):
    """Exercise the shipped client transport after a rename, not a fake send method."""
    from collab.client.hub_client import HubClient, HubError
    from collab.protocol import Envelope
    store=live_server['store']
    bob=store.add_participant('bob','reply-original')
    other=store.add_participant('other','reply-replacement')
    store.rename(bob.id,'bobby');store.rename(other.id,'bob')
    with HubClient(live_server['base'],live_server['host_token']) as client:
        client.send(Envelope(kind='chat',sender='alice',to='bob',to_id=bob.id,text='stable-reply-marker'))
        with pytest.raises(HubError):
            client.send(Envelope(kind='chat',sender='alice',to='bob',to_id='p_missing',text='must-not-broadcast'))
    assert any(e.text=='stable-reply-marker' and e.to=='bobby' for e in store.history(viewer=bob.id))
    assert not any(e.text in ('stable-reply-marker','must-not-broadcast') for e in store.history(viewer=other.id))


@pytest.mark.parametrize('target', [{'to_id':'p_missing'},{'to_id':''},{'to_id':123},{'to_id':None},
                                   {'to_id':'one','toId':'two'}])
def test_invalid_stable_targets_never_fall_back_to_a_name_or_room(client, session, host_headers, target):
    """A failed explicit identity lookup must not become a public or recycled DM."""
    join(client,session,'bob')
    r=client.post(PREFIX+'/messages',headers=host_headers,json={'to':'bob','text':'invalid-target-marker',**target})
    assert r.status_code in (400,404)
    assert not any(e.text=='invalid-target-marker' for e in session['store'].history())


def test_a_hub_without_the_id_endpoint_cannot_receive_a_downgraded_sensitive_reply(client, session):
    """Remove only the new route: the name endpoint must never receive a fallback."""
    from collab.client.hub_client import HubClient, HubError
    from collab.protocol import Envelope
    store=session['store']
    original=store.add_participant('bob','mixed-original')
    replacement=store.add_participant('replacement','mixed-other')
    store.rename(original.id,'bobby');store.rename(replacement.id,'bob')
    app=session['app'];routes=app.router.routes[:]
    app.router.routes[:]=[r for r in routes if getattr(r,'path',None)!=PREFIX+'/messages/by-id']
    hub=HubClient('http://testserver',session['host_token'])
    transport=hub._client
    hub._client=client
    try:
        with pytest.raises(HubError):
            hub.send(Envelope(kind='chat',sender='alice',to='bob',to_id=original.id,
                              text='sensitive-mixed-version-marker'))
        assert not any(e.text=='sensitive-mixed-version-marker' for e in store.history())
    finally:
        hub._client=transport;hub.close();app.router.routes[:]=routes


def test_the_guarded_endpoint_requires_an_explicit_principal(client, session, host_headers):
    """A name-only body on the new path must not silently use ordinary routing."""
    join(client,session,'bob')
    r=client.post(PREFIX+'/messages/by-id',headers=host_headers,json={'to':'bob','text':'unbound-id-endpoint'})
    assert r.status_code==400
    assert not any(e.text=='unbound-id-endpoint' for e in session['store'].history())


def test_private_event_storage_never_replays_a_missing_identity_as_public(tmp_path):
    """Defense in depth if an internal publisher forgets a private recipient ID."""
    from collab.protocol import Envelope
    store=Store(tmp_path/'private-event.db')
    try:
        sender=store.add_participant('sender','sender-token')
        unrelated=store.add_participant('unrelated','other-token')
        store.append(Envelope(kind='chat',sender=sender.name,sender_id=sender.id,
                              to='missing',text='unresolved-private-marker'))
        assert not any(e.text=='unresolved-private-marker' for e in store.history(viewer=unrelated.id))
        assert any(e.text=='unresolved-private-marker' for e in store.history(viewer=sender.id))
        assert not any(e.text=='unresolved-private-marker' for e in store.since(0,viewer=unrelated.id))
    finally:store.close()


def test_queue_time_file_target_remains_original_after_rename(live_server, tmp_path):
    """Exercise bounded roster resolution, persistent queue and actual guarded upload."""
    from collab.client.hub_client import HubClient
    from collab.worker import Store as WorkerStore
    store=live_server['store']
    original=store.add_participant('bob','file-original')
    replacement=store.add_participant('other','file-replacement')
    payload=tmp_path/'queued.txt';payload.write_bytes(b'queued-synthetic-file')
    queue=WorkerStore(tmp_path/'worker')
    queue.configure({'agent':'command','command':['never-executed'],'scope':'Synthetic queue only'})
    with HubClient(live_server['base'],live_server['host_token']) as client:
        ident=client.resolve_recipient('bob')
        record_id=queue.queue_file(payload,to='bob',to_id=ident)
        store.rename(original.id,'bobby');store.rename(replacement.id,'bob')
        row=next(r for r in queue.outbox() if r['id']==record_id)
        shared=client.upload_file(Path(row['stash']),to=row['to'],to_id=row['to_id'],delivery=row['id'])
        assert shared['recipient_id']==original.id and shared['recipient']=='bobby'
        with HubClient(live_server['base'],'file-replacement') as other:
            assert shared['id'] not in [f['id'] for f in other.list_files()]
        with HubClient(live_server['base'],'file-original') as receiver:
            assert shared['id'] in [f['id'] for f in receiver.list_files()]
    queue.acknowledge(record_id)
