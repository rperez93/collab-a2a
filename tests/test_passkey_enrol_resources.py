"""Owned TCP enrollment controls measure bounds, not human/crypto acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import asyncio,json,resource,sys,tempfile,time
from pathlib import Path
import httpx
from collab.client import passkey as p,passkey_adapter as a
from collab.client.passkey_enrol import EnrolService
from collab.config import SessionProfile

class Stream(httpx.AsyncByteStream):
    def __init__(self,value=None,mode='json'):self.value=value;self.mode=mode;self.chunks=0;self.closed=False
    async def __aiter__(self):
        if self.mode=='hub_silence':await asyncio.sleep(10)
        elif self.mode=='hub_flood':
            for _ in range(1000):self.chunks+=1;yield b'x'*65536
        else:yield json.dumps(self.value).encode()
    async def aclose(self):self.closed=True

def memory():
    return {row.split(':')[0]:int(row.split()[1]) for row in Path('/proc/self/status').read_text().splitlines() if row.startswith(('VmRSS:','VmHWM:'))}

async def run(root):
    mode=sys.argv[1];p.BODY_SECONDS=.15;p.BACKEND_SECONDS=.2;a.SECONDS=.12
    fault=[None];streams=[];calls=[]
    async def hub(request):
        path=request.url.path.rsplit('/',1)[-1];calls.append((request.method,path))
        assert request.headers['authorization']=='Bearer synthetic-resource-token'
        assert request.headers['accept-encoding']=='identity'
        assert request.method=='GET','Resource probe must never mutate a key'
        value={'api_version':1,'available':True,'methods':['passkey'],'algorithms':{'passkey':[-7,-257]}} if path=='capabilities' else {'api_version':1,'credentials':[]}
        stream=Stream(value,fault[0] or 'json');streams.append(stream)
        return httpx.Response(200,headers={'content-type':'application/json'},stream=stream)
    profile=SessionProfile(session_id='s_resource',name='Synthetic only',host_name='synthetic-host',participant_id='p_resource',token='synthetic-resource-token',url='http://synthetic-hub.invalid',home=str(root/'profile'))
    transport=a.ApprovalTransport(profile,transport=httpx.MockTransport(hub))
    page=p.PasskeyPage(opener=lambda _:True);service=EnrolService(page,transport)
    await service.human_start();flow=service.flow
    path='/enrol/'+flow.identity;headers={'Origin':page.origin,'X-Collab-Approval-Token':flow.token}
    task=page._task;server=page._server;port=page.port;opened=[];sent=0;status=None;admitted=None
    before=memory();usage=resource.getrusage(resource.RUSAGE_SELF);cpu=usage.ru_utime+usage.ru_stime;start=time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=1) as client:
            if mode in ('silence','shared_cap'):
                opened=[await asyncio.open_connection('127.0.0.1',port) for _ in range(p.MAX_CONNECTIONS)]
                if mode=='shared_cap':
                    for _,writer in opened:
                        writer.write(('POST '+path+'/options HTTP/1.1\r\nHost: localhost:'+str(port)+'\r\n').encode());await writer.drain()
                await asyncio.sleep(.01)
                admitted=len(server.server_state.connections);assert admitted==8,admitted
                ninth=await asyncio.open_connection('127.0.0.1',port);opened.append(ninth)
                assert await asyncio.wait_for(ninth[0].read(1),.3)==b''
                # The same physical listener owns request and enrol routes.
                assert len(page.app.router.routes)==6 and page._enrol_service is service
                assert all(item==b'' for item in await asyncio.wait_for(asyncio.gather(*(r.read(1) for r,_ in opened[:8])),.5))
            elif mode=='guard':
                before_calls=len(calls)
                for changed,suffix in (({'Host':'127.0.0.1:'+str(port)},''),({'Origin':'null'},''),({'X-Collab-Approval-Token':'x'*43},''),({},'?token=not-a-capability')):
                    response=await client.get(page.origin+path+'/status'+suffix,headers={**headers,**changed})
                    assert response.status_code==403,response.status_code
                assert len(calls)==before_calls
                status=403
            elif mode in ('hub_silence','hub_flood'):
                fault[0]=mode
                response=await client.get(page.origin+path+'/status',headers=headers)
                status=response.status_code;assert status==502,status
                assert all(stream.closed for stream in streams)
                assert sum(stream.chunks for stream in streams)<=5
            elif mode=='deep_json':
                response=await client.post(page.origin+path+'/options',headers={**headers,'Content-Type':'application/json'},content=b'{"nested":'+b'['*40+b'0'+b']'*40+b'}')
                status=response.status_code;assert status==502,status
            else:
                reader,writer=await asyncio.open_connection('127.0.0.1',port);opened.append((reader,writer))
                if mode=='headers':initial=b'GET /enrol/assets/enrol.js HTTP/1.1\r\nX-Partial: '
                else:initial=('POST '+path+'/options HTTP/1.1\r\nHost: localhost:'+str(port)+'\r\nOrigin: '+page.origin+'\r\nX-Collab-Approval-Token: '+flow.token+'\r\nContent-Type: application/json\r\n'+('Transfer-Encoding: chunked' if mode=='flood' else 'Content-Length: 100000')+'\r\n\r\n').encode()
                reply=asyncio.create_task(reader.read(4096));writer.write(initial);await writer.drain()
                try:
                    for _ in range(128):
                        if reply.done():break
                        chunk=b' ' if mode=='trickle' else b'x'*16384
                        writer.write(b'4000\r\n'+chunk+b'\r\n' if mode=='flood' else chunk);sent+=len(chunk)
                        await writer.drain();await asyncio.sleep(.01 if mode=='trickle' else .001)
                except (OSError,ConnectionError):pass
                try:raw=await asyncio.wait_for(reply,1)
                except (OSError,ConnectionError):
                    if mode!='headers':raise
                    raw=b''
                if mode=='flood':assert sent>128*1024 and b'413' in raw,(sent,raw[:100])
                elif mode=='trickle':assert b'502' in raw,raw[:100]
                else:assert not raw,raw[:100]
            assert not flow.busy
            assert service.held is None and flow.challenge_id is None and flow.phase=='ready'
            assert not any(method=='POST' for method,_ in calls)
    finally:
        for _,writer in opened:
            writer.close()
            try:await asyncio.wait_for(writer.wait_closed(),.3)
            except (OSError,TimeoutError):pass
        await page.stop();await transport.close()
    assert task.done() and not server.server_state.connections and not server.server_state.tasks
    assert service.flow is None and page._task is None and page._socket is None and page._active==0
    try:
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
    except OSError:pass
    else:
        writer.close();await writer.wait_closed();raise AssertionError('owned listener survived stop')
    usage=resource.getrusage(resource.RUSAGE_SELF)
    print(json.dumps({'mode':mode,'status':status,'admitted_connections':admitted,'body_or_header_bytes':sent,'wall_seconds':time.monotonic()-start,'cpu_seconds':usage.ru_utime+usage.ru_stime-cpu,'before_kib':before,'after_kib':memory(),'cleanup':True,'scope':'Owned synthetic TCP page and mocked capability only; no authenticator/core approval/provider','memory_metric':'Linux post-exec process-image VmHWM/VmRSS; not inherited ru_maxrss or aggregate peak'}))

with tempfile.TemporaryDirectory(prefix='owned-enrol-resource-') as folder:asyncio.run(run(Path(folder)))
'''


@pytest.mark.skipif(not Path('/proc/self/status').exists(), reason='Linux process-image memory is unavailable')
@pytest.mark.parametrize('mode', ['silence', 'shared_cap', 'headers', 'flood', 'trickle', 'deep_json', 'hub_silence', 'hub_flood', 'guard'])
def test_enrolment_uses_bounded_owned_readers_and_releases_its_listener(mode):
    """Exercise actual page admission/body guards and shared transport budgets.

    Fresh subprocesses include the synthetic attacking sockets and mock hub in
    process CPU and memory. The modest ceilings detect eager unbounded readers
    without claiming final R17 or native authenticator performance evidence.
    """
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'COLLAB_NO_WORKER': '1', 'PYTHONNOUSERSITE': '1'}
    done = subprocess.run([sys.executable, '-c', PROBE, mode], env=env, capture_output=True,
                          text=True, timeout=8)
    assert done.returncode == 0, done.stdout + done.stderr
    measured = json.loads(done.stdout)
    print(json.dumps(measured, sort_keys=True))
    assert measured['cleanup'] is True
    assert measured['wall_seconds'] < 2
    assert measured['cpu_seconds'] < .5
    assert measured['after_kib']['VmHWM'] < 128 * 1024
    assert measured['after_kib']['VmHWM'] - measured['before_kib']['VmHWM'] < 16 * 1024
