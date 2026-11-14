"""A pooled TLS POST must see the closed entrance before slow upstream reaping."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PROBE = r'''
"""Owned synthetic listeners and HTTPX only; no live hub, credentials, or retries."""
import asyncio, contextlib, hashlib, json, os, socket, ssl, sys, threading, time
from pathlib import Path
from tempfile import TemporaryDirectory
import httpx
from collab.server import local_tls


def memory():
    return {row.split(':')[0]:int(row.split()[1]) for row in Path('/proc/self/status').read_text().splitlines()
            if row.startswith(('VmRSS:','VmHWM:'))}


async def run(directory, transport, mode):
    received=[]; handlers=set(); connection_count=[0]; close_times={}; frontend_socket={}; handoff_socket={}
    first_done=threading.Event(); start_second=threading.Event()
    close_first=asyncio.Event(); backend_closed=asyncio.Event()
    proxy_closing=asyncio.Event(); release_proxy_close=asyncio.Event()
    original_open=asyncio.open_connection
    loop=asyncio.get_running_loop();original_handoff=loop.connect_accepted_socket
    wrapped_connections=[0]; forwarded_connections=[0]
    async def backend(reader, writer):
        current=asyncio.current_task();handlers.add(current)
        connection_count[0]+=1;connection=connection_count[0]
        try:
            while True:
                try:
                    headers=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
                except (asyncio.TimeoutError,asyncio.IncompleteReadError,ConnectionError): return
                assert len(headers)<8192
                length=next((int(row.split(b':',1)[1]) for row in headers.split(b'\r\n') if row.lower().startswith(b'content-length:')),0)
                assert 0<=length<=4096
                body=await asyncio.wait_for(reader.readexactly(length),1)
                ident=json.loads(body)['id']
                received.append({'request_id':ident,'connection':connection})
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}')
                await writer.drain()
                if ident==1:
                    await close_first.wait();return
        finally:
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()
            if connection==1:backend_closed.set()
            handlers.discard(current)
    backend_server=await asyncio.start_server(backend,'127.0.0.1',0,limit=8192)
    port=backend_server.sockets[0].getsockname()[1]
    proxy=None; verification=True
    class OwnedCloseBarrier:
        def __init__(self,writer): self.writer=writer
        def __getattr__(self,name):return getattr(self.writer,name)
        async def wait_closed(self):
            close_times['upstream_wait_enter']=time.monotonic()
            proxy_closing.set()
            await release_proxy_close.wait()
            await self.writer.wait_closed()
    async def instrumented_open(host,*args,**kwargs):
        reader,writer=await original_open(host,*args,**kwargs)
        wrapped_connections[0]+=1
        return reader,OwnedCloseBarrier(writer) if wrapped_connections[0]==1 else writer
    if transport=='tls':
        context=local_tls.server_context(directory,'127.0.0.1');assert context is not None
        async def observed_handoff(factory,connection,*args,**kwargs):
            if not handoff_socket:
                handoff_socket.update(proto=connection.proto,nodelay=connection.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY))
            return await original_handoff(factory,connection,*args,**kwargs)
        loop.connect_accepted_socket=observed_handoff
        if mode=='gated':asyncio.open_connection=instrumented_open
        proxy=local_tls.LocalTLS(f'http://127.0.0.1:{port}','127.0.0.1',context)
        original_forward=proxy._forward
        class FrontWriter:
            def __init__(self,writer,first): self.writer=writer; self.first=first
            def __getattr__(self,name):return getattr(self.writer,name)
            def close(self):
                if self.first:close_times['tls_close_called']=time.monotonic()
                self.writer.close()
        async def traced_forward(reader,writer):
            forwarded_connections[0]+=1
            if forwarded_connections[0]==1:
                sock=writer.get_extra_info('socket')
                frontend_socket.update(proto=sock.proto,nodelay=sock.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY))
            await original_forward(reader,FrontWriter(writer,forwarded_connections[0]==1))
        proxy._forward=traced_forward
        await proxy.start()
        base=f'https://127.0.0.1:{proxy.port}'
        verification=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        verification.load_verify_locations(cadata=local_tls.read_certificate(directory))
    else:base=f'http://127.0.0.1:{port}'
    before=memory();cpu=time.process_time();started=time.monotonic()
    def client():
        attempts=[]
        # No transport retries, no application retry: every request ID is sent once.
        with httpx.Client(verify=verification,trust_env=False,timeout=.3,
                          limits=httpx.Limits(max_connections=1,max_keepalive_connections=1,keepalive_expiry=5)) as http:
            for ident in range(1,3):
                if ident==2:
                    assert start_second.wait(3)
                t=time.monotonic()
                try:
                    response=http.post(base+'/tasks',json={'id':ident})
                    assert response.content == b'{}'
                    attempts.append({'request_id':ident,'status':response.status_code,'seconds':time.monotonic()-t})
                except Exception as exc:
                    attempts.append({'request_id':ident,'error':type(exc).__name__,'detail':str(exc),'seconds':time.monotonic()-t})
                if ident==1:first_done.set()

        return attempts
    job=asyncio.create_task(asyncio.to_thread(client))
    try:
        while not first_done.is_set():await asyncio.sleep(.001)
        close_first.set()
        await asyncio.wait_for(backend_closed.wait(),1)
        if transport=='tls' and mode=='gated':
            await asyncio.wait_for(proxy_closing.wait(),1)
            await asyncio.sleep(.01)
            start_second.set()
            await asyncio.sleep(.05)
            release_proxy_close.set()
        else:
            await asyncio.sleep(.02)
            start_second.set()
        attempts=await asyncio.wait_for(job,8)
        elapsed=time.monotonic()-started;used=time.process_time()-cpu;after=memory()
        result={'transport':transport,'mode':mode,'source_sha256':hashlib.sha256(Path(local_tls.__file__).read_bytes()).hexdigest(),
                'wall_seconds':elapsed,'process_cpu_seconds':used,'before_kib':before,'after_kib':after,
                'attempt_count':len(attempts),'upstream_received_count':len(received),'connections':connection_count[0],
                'errors':[row for row in attempts if 'error' in row],
                'second_attempt':next((row for row in attempts if row['request_id']==2),None),
                'handoff_socket':handoff_socket,'frontend_socket':frontend_socket,'close_times':close_times,'tls_close_before_upstream_wait':close_times.get('tls_close_called',float('inf'))<=close_times.get('upstream_wait_enter',float('-inf')),
                'second_reached_upstream':any(row['request_id']==2 for row in received),
                'missing_upstream_ids':[row['request_id'] for row in attempts if not any(r['request_id']==row['request_id'] for r in received)],
                'scope':'Synthetic raw HTTP backend, pinned production LocalTLS, owned sync HTTPX client; no live hub/tasks/credentials or POST retries. Gated mode deliberately holds owned upstream wait_closed.'}
    finally:
        release_proxy_close.set();start_second.set();close_first.set()
        if proxy:await proxy.close()
        asyncio.open_connection=original_open
        loop.connect_accepted_socket=original_handoff
        backend_server.close();await backend_server.wait_closed()
        for task in list(handlers):task.cancel()
        await asyncio.gather(*list(handlers),return_exceptions=True)
        await asyncio.gather(job,return_exceptions=True)
    result['proxy_tasks_after_cleanup']=len(proxy.tasks) if proxy else 0
    result['backend_tasks_after_cleanup']=len(handlers)
    print(json.dumps(result))

with TemporaryDirectory(prefix='collab-owned-tls-probe-') as temp:
    asyncio.run(run(Path(temp),sys.argv[1],sys.argv[2]))
'''


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is absent")
@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="Linux post-exec memory is unavailable")
@pytest.mark.parametrize("transport,mode", [("plain", "normal"), ("tls", "gated")])
def test_a_closed_upstream_is_not_kept_available_for_a_second_pooled_post(transport, mode):
    """A barrier amplifies close propagation only; it does not model the original live load.

    The real synchronous HTTPX client runs in an owned thread, sharing neither
    its pool nor blocking I/O with the server loop. Each request ID is sent
    exactly once. Backend receipt and full response bytes distinguish a lost
    unsent second request from an uncertain committed operation. Absolute memory
    is Linux process-image VmHWM, never inherited ru_maxrss or an aggregate peak.
    """
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                   "COLLAB_NO_WORKER": "1", "PYTHONNOUSERSITE": "1"}
    done = subprocess.run([sys.executable, "-c", PROBE, transport, mode],
                          capture_output=True, text=True, env=environment, timeout=8)
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    print(json.dumps(result, sort_keys=True))
    assert result["attempt_count"] == result["upstream_received_count"] == 2, result
    assert not result["errors"], result
    assert result["second_reached_upstream"]
    assert result["proxy_tasks_after_cleanup"] == result["backend_tasks_after_cleanup"] == 0
    assert result["process_cpu_seconds"] < 2
    assert result["after_kib"]["VmHWM"] < 128 * 1024
    if transport == "tls" and mode == "gated":
        assert result["handoff_socket"]["nodelay"] == 1, result
        assert result["frontend_socket"]["nodelay"] == 1, result
        assert result["tls_close_before_upstream_wait"], result
