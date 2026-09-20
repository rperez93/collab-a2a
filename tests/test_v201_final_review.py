"""Counterexamples found in the final independent v2.0.1 review."""
import asyncio
from types import SimpleNamespace

import httpx

from collab import worker
from collab.client.worker_service import Conversation


async def test_exact_handoffs_work_with_the_supported_python_310_asyncio_api(tmp_path, monkeypatch):
    """Python 3.10 has wait_for but no asyncio.timeout context manager."""
    monkeypatch.delattr(asyncio, 'timeout', raising=False)
    sent = []
    async def respond(request):
        sent.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, url='http://synthetic', token='test'), _http=client)
        store = worker.Store(tmp_path)
        store.configure({'agent': 'codex', 'scope': 'Coordinate only'})
        store.send('Exact authorized handoff', to='bob')
        await Conversation(daemon)._flush(store, store.snapshot()['generation'])
    assert len(sent) == 1
    assert not store.outbox()


def test_a_partial_source_switch_cannot_lend_its_predecessors_account_relationship(tmp_path):
    """Token-only reporting can precede a new source's first quota report."""
    store = worker.Store(tmp_path)
    store.report_stats({'source': 'account A', 'quota_scope': 'independent',
                        'quotas': {'five_hour': {'used_pct': 30}}})
    store.report_stats({'source': 'account B', 'tokens_in': 10})
    store.report_stats({'source': 'account B', 'quotas': {'five_hour': {'used_pct': 50}}})
    assert worker.metrics(tmp_path)['quota_scope'] == 'unknown'
