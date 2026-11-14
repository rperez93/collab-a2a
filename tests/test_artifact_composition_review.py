"""Independent composition attacks; failures are evidence, not weakened expectations."""
import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from collab import cli, worker
from collab.client.hub_client import HubClient
from collab.client.worker_service import Conversation
from collab.config import SessionProfile
from test_files import _join, _upload


@pytest.mark.parametrize('name,flags,expected_keep', [
    ('review.md',['--temporary'],False),('archive.bin',['--keep'],True)])
@pytest.mark.parametrize('manual',[False,True])
def test_ordered_worker_upload_preserves_explicit_retention_and_source(tmp_path,monkeypatch,client,session,name,flags,expected_keep,manual):
    """Queue ownership must not erase the sender's explicit durable metadata."""
    monkeypatch.delenv('COLLAB_NO_WORKER',raising=False)
    _join(client,session,'bob')
    profile=SessionProfile('s_test','http://testserver','alice','alice',session['host_token'],
        home=str(tmp_path/'sender'),participant_id=session['store'].resolve_name('alice'),is_host=True)
    box=worker.Store(profile.dir)
    box.configure({'agent':'command','command':['unused-test-adapter'],'scope':'test'})
    box.send('older message',to='bob')
    path=tmp_path/name;path.write_bytes(b'private review')
    hub=HubClient(profile.url,profile.token);hub._client.close();hub._client=client;hub._verified_url=profile.url
    monkeypatch.setattr(cli,'_require_own_profile',lambda args:profile)
    monkeypatch.setattr(cli,'_client',lambda p:contextlib.nullcontext(hub))
    monkeypatch.setattr(cli.onboard,'ensure_daemon',lambda *a,**k:None)
    args=cli.build_parser().parse_args(['file','send',str(path),'--to','bob','--source','https://example.test/review',*flags])
    assert cli.cmd_file(args)==0
    assert len(box.outbox())==2
    async def flush():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session['app'])) as http:
            service=Conversation(SimpleNamespace(profile=profile,_http=http))
            await service._flush(box,box.snapshot()['generation'])
    if manual:
        original_client=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original_client(transport=httpx.ASGITransport(app=session['app']),**kw))
        assert cli.cmd_worker(cli.build_parser().parse_args(['worker','off']))==0
    else:
        asyncio.run(flush())
    record=session['store'].files()[0]
    assert record['name']==name
    assert bool(record['kept']) is expected_keep
    assert record['source']=='https://example.test/review'


@pytest.mark.parametrize('shape',['old','malformed','flood','silent'])
@pytest.mark.parametrize('manual',[False,True])
def test_known_success_with_unconfirmed_retention_is_warned_once_not_reuploaded(tmp_path,monkeypatch,shape,manual):
    """Confirmation failure after accepted headers is not permission to duplicate bytes."""
    import json,resource,time
    from collab import file_retention
    monkeypatch.delenv('COLLAB_NO_WORKER',raising=False)
    monkeypatch.setattr(file_retention,'CONFIRM_SECONDS',.05)
    profile=SessionProfile('s_test','http://old','alice','alice','synthetic',home=str(tmp_path))
    box=worker.Store(profile.dir);box.configure({'agent':'command','command':['unused'],'scope':'test'})
    path=tmp_path/'review.md';path.write_text('review')
    box.queue_file(path,to='bob',keep=True,source='https://example.test/review')
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if shape=='silent':await asyncio.sleep(30)
            if shape=='flood':
                while True:yield b'x'*65536
            yield b'not-json' if shape=='malformed' else b'{"id":"f_old"}'
    posts=[]
    async def transport(request):
        posts.append(request)
        return httpx.Response(200,stream=Body())
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            service=Conversation(SimpleNamespace(profile=profile,_http=http))
            generation=box.snapshot()['generation']
            await service._flush(box,generation)
            await service._flush(box,generation)
    wall,cpu=time.monotonic(),time.process_time()
    if manual:
        original_client=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original_client(transport=httpx.MockTransport(transport),**kw))
        monkeypatch.setattr(cli,'_require_own_profile',lambda args:profile)
        monkeypatch.setattr(cli,'_client',lambda p:contextlib.nullcontext(SimpleNamespace()))
        assert cli.cmd_worker(cli.build_parser().parse_args(['worker','off']))==0
        assert cli.cmd_worker(cli.build_parser().parse_args(['worker','off']))==0
    else:
        asyncio.run(go())
    assert time.monotonic()-wall<1 and time.process_time()-cpu<.5
    assert len(posts)==1 and not box.outbox()
    assert len(box.pending())==1 and 'unconfirmed' in box.pending()[0]['question']
    assert 'Keep your original' in box.pending()[0]['question']


@pytest.mark.parametrize('bad_keep',[1,'true',[],{}])
def test_queue_retention_rejects_untyped_policy_before_copy(tmp_path,bad_keep):
    box=worker.Store(tmp_path);path=tmp_path/'review.md';path.write_text('r')
    with pytest.raises(ValueError):box.queue_file(path,to='bob',keep=bad_keep)


def test_queue_and_stash_read_enforce_regular_file_size_limit(tmp_path,monkeypatch):
    from collab import file_retention,protocol
    box=worker.Store(tmp_path);box.configure({'agent':'command','command':['unused'],'scope':'test'})
    path=tmp_path/'oversized.bin';path.write_bytes(b'x'*1025)
    monkeypatch.setattr(protocol,'MAX_FILE_BYTES',1024)
    with pytest.raises(ValueError):box.queue_file(path,to='bob')
    assert not box.outbox()
    assert not list((tmp_path/'outbox-files').glob('*'))
    with pytest.raises(ValueError):file_retention.read_upload(path)


def test_cancellation_after_accepted_headers_warns_and_acknowledges_before_propagating(tmp_path,monkeypatch):
    """Stopping a listener after2xx must not schedule a duplicate upload on restart."""
    monkeypatch.delenv('COLLAB_NO_WORKER',raising=False)
    profile=SessionProfile('s_test','http://old','alice','alice','synthetic',home=str(tmp_path))
    box=worker.Store(profile.dir);box.configure({'agent':'command','command':['unused'],'scope':'test'})
    path=tmp_path/'review.md';path.write_text('review');box.queue_file(path,to='bob',keep=True)
    async def go():
        reading=asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set();await asyncio.sleep(30)
                yield b'{}'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Body()))) as http:
            service=Conversation(SimpleNamespace(profile=profile,_http=http))
            task=asyncio.create_task(service._flush(box,box.snapshot()['generation']))
            await asyncio.wait_for(reading.wait(),1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(go())
    assert not box.outbox() and len(box.pending())==1


@pytest.mark.parametrize('value',[None,'false',0,1,{},[]])
def test_malformed_retention_metadata_never_claims_durable_availability(value):
    """Truthiness is not an acknowledgement that a host retained the artifact."""
    assert cli._file_retention_status({'kept':value,'state':'available'})=='retention unconfirmed by host'
    assert cli._file_retention_status({'kept':True,'state':'available'})=='kept until withdrawal'


@pytest.mark.parametrize('ack',[True,'false'])
def test_actual_temporary_upload_warns_when_ack_is_wrong_or_malformed(tmp_path,monkeypatch,capsys,ack):
    """A temporary request must not silently accept durable or truthy-string metadata."""
    path=tmp_path/'review.md';path.write_text('review')
    record={'id':'f_synthetic','name':'review.md','size':6,'sender':'alice','kept':ack,'state':'available'}
    monkeypatch.setattr(cli,'_require_own_profile',lambda args:SimpleNamespace(room='general'))
    monkeypatch.setattr(cli,'_client',lambda p:contextlib.nullcontext(SimpleNamespace(upload_file=lambda *a,**k:record)))
    assert cli.cmd_file(cli.build_parser().parse_args(['file','send',str(path),'--temporary']))==0
    output=capsys.readouterr()
    assert 'did not confirm requested retention/source' in output.err
    assert 'Keep your original' in output.err
    if ack=='false':
        assert 'retention unconfirmed' in output.out
        assert 'kept until withdrawal' not in output.out
        assert 'withdraw explicitly' not in output.out


@pytest.mark.parametrize('manual',[False,True])
@pytest.mark.parametrize('encoding',['gzip','br','deflate','identity, gzip'])
def test_encoded_confirmation_is_not_iterated_or_reuploaded(tmp_path,monkeypatch,manual,encoding):
    """A stream-generated32MiB gzip bomb must be refused before HTTPX can inflate it."""
    import zlib
    monkeypatch.delenv('COLLAB_NO_WORKER',raising=False)
    profile=SessionProfile('s_test','http://old','alice','alice','synthetic',home=str(tmp_path))
    box=worker.Store(profile.dir);box.configure({'agent':'command','command':['unused'],'scope':'test'})
    path=tmp_path/'review.md';path.write_text('review');box.queue_file(path,to='bob',keep=True)
    iterations=[];posts=[]
    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):
            iterations.append(True)
            compressor=zlib.compressobj(wbits=31)
            encoded=bytearray()
            block=b'x'*65536
            for _ in range(512):
                encoded.extend(compressor.compress(block))
            encoded.extend(compressor.flush())
            assert len(encoded)<40000  # no preallocated32MiB plaintext fixture
            yield bytes(encoded)
    async def transport(request):
        assert request.headers['Accept-Encoding']=='identity'
        posts.append(request)
        return httpx.Response(200,headers={'Content-Encoding':encoding},stream=Bomb())
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            service=Conversation(SimpleNamespace(profile=profile,_http=http))
            await service._flush(box,box.snapshot()['generation'])
            await service._flush(box,box.snapshot()['generation'])
    if manual:
        original_client=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original_client(transport=httpx.MockTransport(transport),**kw))
        monkeypatch.setattr(cli,'_require_own_profile',lambda args:profile)
        monkeypatch.setattr(cli,'_client',lambda p:contextlib.nullcontext(SimpleNamespace()))
        assert cli.cmd_worker(cli.build_parser().parse_args(['worker','off']))==0
        assert cli.cmd_worker(cli.build_parser().parse_args(['worker','off']))==0
    else:
        asyncio.run(go())
    assert not iterations, 'encoded confirmations must be rejected before body iteration'
    assert len(posts)==1 and not box.outbox()
    assert len(box.pending())==1 and 'unconfirmed' in box.pending()[0]['question']
