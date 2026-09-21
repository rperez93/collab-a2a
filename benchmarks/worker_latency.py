"""Measure local worker scheduling with a deterministic, unbilled provider.

Run with an absolute PYTHONPATH pointing to the revision under test. The
--heartbeat-only switch reproduces 2.1.1's three-second intake polling. This
isolates scheduling delay; it does not claim to measure provider latency.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import tempfile
import time
from types import SimpleNamespace


async def run(heartbeat_only):
    with tempfile.TemporaryDirectory(prefix='collab-worker-latency-') as temp:
        root = Path(temp)
        os.environ.update(COLLAB_CONFIG=str(root/'config.json'), COLLAB_HOME=str(root/'home'))
        (root/'config.json').write_text('{}')
        from collab import worker, worker_runtime
        from collab.client.inbox import Inbox
        from collab.client.worker_service import Conversation
        from collab.protocol import Envelope
        import httpx
        inbox = Inbox(root)
        daemon = SimpleNamespace(profile=SimpleNamespace(dir=root, name='alice', participant_id='p_a',
                                 url='https://synthetic.invalid', token='synthetic'),
                                 inbox=inbox, snapshot={'participants': [{'name': 'bob'}]}, _http=None)
        store = worker.Store(root)
        store.configure({'agent': 'codex', 'model': 'gpt-5.6-luna', 'scope': 'Synthetic benchmark'})
        sent = []
        async def fake(*args, **kwargs):
            return {'summary': '', 'replies': [{'text': 'Synthetic reply', 'to': 'bob', 'room': ''}], 'escalations': []}
        worker_runtime.run_turn = fake
        async def receive(request):
            sent.append(time.monotonic())
            return httpx.Response(200)
        cpu = time.process_time()
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            daemon._http = client
            service = Conversation(daemon)
            await service.tick()
            await service.task
            arrived = time.monotonic()
            inbox.record(Envelope(seq=1,kind='chat',sender='bob',sender_id='p_b',text='Question'))
            while not sent:
                if heartbeat_only:
                    await asyncio.sleep(3)
                await service.tick()
                if service.task:
                    await service.task
                if time.monotonic()-arrived > 12:
                    raise RuntimeError('No bounded response')
            await service.stop()
        inbox.close()
        print(json.dumps({'heartbeat_only': heartbeat_only, 'reply_seconds': sent[0]-arrived,
                          'cpu_seconds': time.process_time()-cpu,
                          'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--heartbeat-only', action='store_true')
    asyncio.run(run(parser.parse_args().heartbeat_only))
