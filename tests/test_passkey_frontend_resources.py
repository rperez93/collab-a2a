"""The local page must stay cheap while an owned synthetic peer misbehaves."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import asyncio, hashlib, json, resource, sys, time
from pathlib import Path
import httpx
from collab.client import passkey as p

def memory():
    rows=Path('/proc/self/status').read_text().splitlines()
    return {line.split(':')[0]:int(line.split()[1]) for line in rows if line.startswith(('VmRSS:','VmHWM:'))}

class Adapter:
    async def review(self, request_id):
        raw=b'{"payload":"'+b'x'*(p.MAX_REVIEW-14)+b'"}'
        return p.Review(raw,hashlib.sha256(raw).digest(),'Synthetic','peer','target','test only',time.time()+60,enrolled=True)
    async def begin(self,*args): raise AssertionError('no ceremony in resource probe')
    async def complete(self,*args): raise AssertionError('no approval in resource probe')
    async def decline(self,*args): raise AssertionError('no decision in resource probe')

async def main():
    p.BODY_SECONDS=.08
    page=p.PasskeyPage(Adapter(),opener=lambda url:True)
    url=await page.window_url('resource_probe')
    token=url.split('#')[1];headers={'Origin':page.origin,'X-Collab-Approval-Token':token}
    async with httpx.AsyncClient(trust_env=False) as client:
        # Populate a reviewed request before testing an input-body refusal.
        response=await client.get(page.origin+'/consent/resource_probe/review',headers=headers)
        assert response.status_code==200 and len(response.json()['canonical_review'].encode())<=p.MAX_REVIEW
        before=memory();usage=resource.getrusage(resource.RUSAGE_SELF)
        cpu_before=usage.ru_utime+usage.ru_stime;started=time.monotonic()
        mode=sys.argv[1];opened=[];response_bytes=b'';sent_bytes=0
        try:
            if mode=='silence':
                opened=[await asyncio.open_connection('127.0.0.1',page.port) for _ in range(p.MAX_CONNECTIONS)]
                assert all(value==b'' for value in await asyncio.wait_for(asyncio.gather(*(reader.read() for reader,_ in opened)),1))
            elif mode=='review':
                for _ in range(20):
                    response=await client.get(page.origin+'/consent/resource_probe/review',headers=headers)
                    assert response.status_code==200
            else:
                reader,writer=await asyncio.open_connection('127.0.0.1',page.port);opened=[(reader,writer)]
                if mode=='headers':
                    initial=b'GET / HTTP/1.1\r\nX-Unterminated: '
                else:
                    initial=('POST /consent/resource_probe/approve/result HTTP/1.1\r\nHost: localhost:'+str(page.port)+
                        '\r\nOrigin: '+page.origin+'\r\nX-Collab-Approval-Token: '+token+
                        '\r\nContent-Type: application/json\r\n'+
                        ('Transfer-Encoding: chunked' if mode=='flood' else 'Content-Length: 100000')+'\r\n\r\n').encode()
                # Observe the refusal while sending. Continuing to write after a
                # server closes can reset TCP and discard its already-sent
                # status, falsely reporting a missing bounded-body refusal.
                reply = asyncio.create_task(reader.read(4096))
                writer.write(initial);await writer.drain()
                try:
                    if mode=='trickle':
                        for _ in range(100):
                            if reply.done(): break
                            writer.write(b' ');sent_bytes+=1;await writer.drain();await asyncio.sleep(.01)
                    else:
                        chunk=b'x'*16384
                        for _ in range(128):
                            if reply.done(): break
                            writer.write((b'4000\r\n'+chunk+b'\r\n') if mode=='flood' else chunk)
                            sent_bytes+=len(chunk)
                            # A 16 KiB/ms sender still exceeds the body cap in
                            # milliseconds, while letting the refusal reader run.
                            await writer.drain();await asyncio.sleep(.001)
                except (OSError,ConnectionError): pass
                try: response_bytes=await asyncio.wait_for(reply,1)
                except (OSError,ConnectionError): pass
                if mode=='flood':
                    assert sent_bytes>p.MAX_REQUEST,sent_bytes
                    assert b'413' in response_bytes,response_bytes[:200]
                if mode=='trickle':assert b'408' in response_bytes,response_bytes[:200]
        finally:
            for _,writer in opened:
                writer.close()
                try: await writer.wait_closed()
                except (OSError,ConnectionError):pass
        after=memory();usage=resource.getrusage(resource.RUSAGE_SELF)
        report={'mode':mode,'sent_body_or_header_bytes':sent_bytes,'wall_seconds':time.monotonic()-started,
                'cpu_seconds':usage.ru_utime+usage.ru_stime-cpu_before,
                'before':before,'after':after,'memory_metric':'Linux post-exec process image; peak growth is after-before VmHWM',
                'scope':'Own synthetic localhost fixture only, no real WebAuthn/hub/provider/approval'}
        await page.stop()
        print(json.dumps(report))

asyncio.run(main())
'''


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="Linux process memory is unavailable")
@pytest.mark.parametrize("mode", ["silence", "headers", "flood", "trickle", "review"])
def test_a_hostile_local_peer_costs_neither_unbounded_memory_nor_a_core(mode):
    """Measure real silent/flooding/trickling sockets and twenty full 64 KiB reviews.

    The budget excludes interpreter startup, includes the owned attacking peer,
    and uses fresh-image /proc rather than fork-inherited ru_maxrss. The 16 MiB
    growth ceiling allows protocol buffers and client/server JSON copies while
    exposing an unbounded eager read. These are component-fixture measurements,
    not final R17 process-tree or connected-host performance claims.
    """
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "COLLAB_NO_WORKER": "1", "PYTHONNOUSERSITE": "1"}
    done = subprocess.run([sys.executable, "-c", PROBE, mode], capture_output=True,
                          text=True, env=env, timeout=10)
    assert done.returncode == 0, done.stdout + done.stderr
    measured = json.loads(done.stdout)
    print(json.dumps(measured, sort_keys=True))
    assert measured["wall_seconds"] < 2
    assert measured["cpu_seconds"] < .5
    assert measured["after"]["VmHWM"] - measured["before"]["VmHWM"] < 16 * 1024
