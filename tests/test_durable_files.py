"""Documents stay available through acknowledgements and expiry sweeps."""
import io
import time

import pytest

from collab.protocol import FILE_TTL_SECONDS, ROOM_FILE_TTL_SECONDS
from test_files import _join, _upload

EXT = '/ext/collab/v1'


@pytest.mark.parametrize('name', ['design.md', 'review.TXT', 'report.pdf', 'evidence.json'])
@pytest.mark.parametrize('private', [False, True])
def test_documents_survive_receipt_and_ttl(client, session, host_headers, monkeypatch, name, private):
    bob = _join(client, session, 'bob')
    record = _upload(client, host_headers, b'durable', name=name, **({'to': 'bob'} if private else {})).json()
    assert record['kept'] is True and record['expires_at'] is None
    assert record['owner'] == record['sender']
    ack = client.post(f"{EXT}/files/{record['id']}/ack", headers=bob).json()
    assert ack['deleted'] is False and ack['kept'] is True
    future = time.time() + FILE_TTL_SECONDS + 60
    monkeypatch.setattr('collab.server.store.time.time', lambda: future)
    assert client.get(f'{EXT}/files', headers=bob).json()['files'][0]['id'] == record['id']
    assert client.get(f"{EXT}/files/{record['id']}/content", headers=bob).content == b'durable'
    assert client.delete(f"{EXT}/files/{record['id']}", headers=host_headers).status_code == 200
    assert client.get(f"{EXT}/files/{record['id']}/content", headers=bob).status_code == 404


def test_temporary_document_expires_with_owner_and_durable_source(client, session, host_headers, monkeypatch):
    bob = _join(client, session, 'bob')
    record = _upload(client, host_headers, b'doc', name='review.md', keep='false',
                     source='https://example.test/reviews/123').json()
    assert record['kept'] is False
    assert record['expires_at'] == record['created_at'] + ROOM_FILE_TTL_SECONDS
    future = record['expires_at'] + 1
    monkeypatch.setattr('collab.server.store.time.time', lambda: future)
    gone = client.get(f"{EXT}/files/{record['id']}/content", headers=bob)
    assert gone.status_code == 410
    assert 'https://example.test/reviews/123' in gone.json()['detail']
    assert record['sender'] in gone.json()['detail']
    assert client.get(f'{EXT}/files', headers=bob).json()['files'] == []
    tombstone = client.get(f'{EXT}/files?include_gone=true', headers=bob).json()['files'][0]
    assert tombstone['state'] == 'expired' and tombstone['source'] == record['source']


def test_expired_private_metadata_is_not_disclosed_to_another_recipient(client, session, host_headers, monkeypatch):
    bob = _join(client, session, 'bob')
    carol = _join(client, session, 'carol')
    record = _upload(client, host_headers, b'doc', name='review.md', keep='false', to='bob',
                     source='https://example.test/private-review').json()
    future = record['expires_at'] + 1
    monkeypatch.setattr('collab.server.store.time.time', lambda: future)
    denied = client.get(f"{EXT}/files/{record['id']}/content", headers=carol)
    assert denied.status_code == 403
    assert 'private-review' not in denied.text
    assert client.get(f'{EXT}/files?include_gone=true', headers=carol).json()['files'] == []
    assert client.get(f"{EXT}/files/{record['id']}/content", headers=bob).status_code == 410


def test_keep_applies_to_binary_and_temporary_doc_still_deletes_on_receipt(client, session, host_headers):
    bob = _join(client, session, 'bob')
    binary = _upload(client, host_headers, b'data', name='a.bin', keep='true').json()
    assert binary['kept']
    doc = _upload(client, host_headers, b'doc', name='review.md', keep='false').json()
    ack = client.post(f"{EXT}/files/{doc['id']}/ack", headers=bob).json()
    assert ack['deleted'] is True
    ack = client.post(f"{EXT}/files/{binary['id']}/ack", headers=bob).json()
    assert ack['deleted'] is False


@pytest.mark.parametrize('source', ['file:///home/private', 'https://user:pass@example.test/review',
                                    'http://example.test/review', 'https://example.test/\x1b',
                                    'https://example.test/' + 'x' * 2048])
def test_invalid_durable_source_is_refused_before_upload(client, host_headers, source):
    response = _upload(client, host_headers, b'doc', name='review.md', source=source)
    assert response.status_code == 422
    assert client.get(f'{EXT}/files', headers=host_headers).json()['files'] == []


def test_old_records_remain_temporary_after_database_migration(tmp_path):
    import sqlite3
    from collab.server.store import Store
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT, size INTEGER, sha256 TEXT, '
                   'sender TEXT, recipient TEXT, room TEXT, created_at REAL, acked_at REAL, '
                   'acked_by TEXT, state TEXT)')
        db.execute("INSERT INTO files VALUES ('f_old','design.md',1,'hash','alice',NULL,'general',0,NULL,NULL,'available')")
    store = Store(path)
    try:
        assert store.get_file('f_old')['kept'] == 0
        assert store.get_file('f_old')['source'] == ''
        assert [r['id'] for r in store.expired_files(1, 1)] == ['f_old']
    finally:
        store.close()


def test_private_document_uses_ids_after_sender_and_recipient_names_are_reused(client, session, host_headers):
    sender = _join(client, session, 'writer')
    bob = _join(client, session, 'bob')
    record = _upload(client, sender, b'private-review', name='review.md', to='bob',
                     source='https://example.test/private-original').json()
    client.post(f'{EXT}/rename', headers=bob, json={'name': 'roberto'})
    client.post(f'{EXT}/rename', headers=sender, json={'name': 'author'})
    replacement_bob = _join(client, session, 'bob')
    replacement_sender = _join(client, session, 'writer')
    for stranger in (replacement_bob, replacement_sender):
        assert client.get(f"{EXT}/files/{record['id']}/content", headers=stranger).status_code == 403
        assert client.get(f'{EXT}/files?include_gone=true', headers=stranger).json()['files'] == []
        assert client.post(f"{EXT}/files/{record['id']}/ack", headers=stranger).status_code == 403
        assert client.delete(f"{EXT}/files/{record['id']}", headers=stranger).status_code == 403
    assert client.get(f"{EXT}/files/{record['id']}/content", headers=bob).content == b'private-review'
    assert client.get(f'{EXT}/files', headers=bob).json()['files'][0]['owner'] == 'author'
    assert client.delete(f"{EXT}/files/{record['id']}", headers=sender).status_code == 200
    ack = client.post(f"{EXT}/files/{record['id']}/ack", headers=bob).json()
    assert ack['state'] == 'withdrawn' and ack['deleted']


def test_withdrawal_during_kept_collection_cannot_resurrect_or_announce_availability(
        client, session, host_headers, monkeypatch):
    store = session['store']
    bob = _join(client, session, 'bob')
    record = _upload(client, host_headers, b'private', name='review.md', to='bob').json()
    original = store.record_collection
    def withdraw_first(file_id, participant_id):
        store.mark_file(file_id, 'withdrawn')
        return original(file_id, participant_id)
    monkeypatch.setattr(store, 'record_collection', withdraw_first)
    response = client.post(f"{EXT}/files/{record['id']}/ack", headers=bob).json()
    assert response['state'] == 'withdrawn' and response['deleted']
    assert store.collectors(record['id']) == []
    events = client.get(f'{EXT}/history', headers=host_headers).json()['events']
    assert not any(e.get('body', {}).get('action') == 'received' for e in events)
    store.mark_file(record['id'], 'collected', acked_by='bob')
    assert store.get_file(record['id'])['state'] == 'withdrawn'


def test_cli_retention_policy_and_old_host_warning(tmp_path, monkeypatch, capsys):
    import contextlib
    from types import SimpleNamespace
    from collab import cli
    path = tmp_path / 'review.md'
    path.write_text('review')
    calls = []
    def upload(path, **kw):
        calls.append(kw)
        return {'id': 'f_old', 'name': path.name, 'size': 6, 'sender': 'alice'}
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: SimpleNamespace(room='general'))
    monkeypatch.setattr(cli, '_client', lambda profile: contextlib.nullcontext(SimpleNamespace(upload_file=upload)))
    assert cli.main(['file', 'send', str(path), '--json']) == 0
    output = capsys.readouterr()
    assert calls[-1]['keep'] is True
    assert 'did not confirm' in output.err
    assert 'Warning' not in output.out
    assert cli.main(['file', 'send', str(path), '--temporary', '--json']) == 0
    assert calls[-1]['keep'] is False
    assert 'did not confirm' in capsys.readouterr().err, 'old host omitted even the requested temporary acknowledgement'
    assert cli._file_retention_status({'kept': True, 'state': 'withdrawn'}) == 'no longer on host'


def test_kept_receipt_wording_is_shared_by_renderers():
    from collab.protocol import file_outcome
    assert file_outcome({'deleted': False, 'kept': True, 'remaining': 0}) == 'kept on the host until explicit withdrawal'
    assert file_outcome({'deleted': True, 'kept': True}) == 'deleted from the host'


def test_legacy_name_claim_after_upload_is_not_bound_to_private_file(tmp_path):
    import sqlite3
    from collab.server.store import Store
    path = tmp_path / 'legacy.db'
    store = Store(path)
    original = store.add_participant('bob', 'synthetic-original')
    sender = store.add_participant('writer', 'synthetic-writer')
    store.add_file('f_legacy', name='review.md', size=1, sha256='hash', sender='writer',
                   recipient='bob', room='general')
    store.rename(original.id, 'roberto')
    store.add_participant('bob', 'synthetic-new-bob')
    store.close()
    # Rebuild the actual old table shape, preserving its original upload time.
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE files RENAME TO files_current')
        db.execute('CREATE TABLE files AS SELECT id,name,size,sha256,sender,recipient,room,'
                   'created_at,acked_at,acked_by,state FROM files_current')
        db.execute('DROP TABLE files_current')
    migrated = Store(path)
    try:
        file = migrated.get_file('f_legacy')
        assert file['sender_id'] == sender.id
        assert file['recipient_id'] is None
        assert file['kept'] == 0
    finally:
        migrated.close()
