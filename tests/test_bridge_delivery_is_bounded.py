"""A local viewer cannot hold a committed event across a route change forever."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from collab.client import bridge as module
from collab.protocol import Envelope


async def test_a_real_local_viewer_gets_the_ordinary_event_frame():
    from websockets.asyncio.client import connect
    bridge = module.Bridge()
    await bridge.start()
    try:
        async with connect(bridge.url) as client:
            for _ in range(30):
                if bridge.clients:
                    break
                await asyncio.sleep(.01)
            event = Envelope(seq=1, kind="chat", sender="peer", text="unchanged bridge frame")
            await bridge.broadcast(event)
            assert json.loads(await asyncio.wait_for(client.recv(), 1))["seq"] == event.seq
    finally:
        await bridge.stop()


async def test_local_viewer_admission_is_bounded_before_retaining_subscribers():
    bridge = module.Bridge()
    bridge._clients = {object() for _ in range(module.MAX_CLIENTS)}
    class Extra:
        code = None
        async def close(self, *, code, reason):
            self.code = code
    extra = Extra()
    await bridge._handle(extra)
    assert bridge.clients == module.MAX_CLIENTS and extra.code == 1013


def test_silent_viewer_fanout_has_one_deadline_and_bounded_cpu_and_rss():
    """Measure silent send/close peers in a fresh interpreter, not a warm pytest RSS delta."""
    code = r'''
import asyncio,json,time
from pathlib import Path
from collab.client import bridge as module
from collab.protocol import Envelope
module.SEND_SECONDS=.02
module.CLOSE_SECONDS=.02
class Silent:
    def __init__(self): self.transport=self; self.aborted=False
    async def send(self,payload): await asyncio.Event().wait()
    async def close(self,**kwargs): await asyncio.Event().wait()
    def abort(self): self.aborted=True
async def run():
    bridge=module.Bridge()
    peers=[Silent() for _ in range(module.MAX_CLIENTS)]
    bridge._clients=set(peers)
    started=time.monotonic(); cpu=time.process_time()
    await bridge.broadcast(Envelope(seq=1,kind='chat',sender='peer',text='x'*8000))
    elapsed=time.monotonic()-started; cpu=time.process_time()-cpu
    assert bridge.clients==0 and all(peer.aborted for peer in peers)
    rss=int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                 if line.startswith('VmHWM:')))
    print(json.dumps({'subscribers':len(peers),'wall_seconds':elapsed,'cpu_seconds':cpu,'peak_rss_kib':rss}))
asyncio.run(run())
'''
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=3, check=True)
    measured = json.loads(done.stdout)
    print("silent bridge fanout " + json.dumps(measured, sort_keys=True))
    assert measured["wall_seconds"] < .4
    assert measured["cpu_seconds"] < .2
    assert measured["peak_rss_kib"] < 64 * 1024
