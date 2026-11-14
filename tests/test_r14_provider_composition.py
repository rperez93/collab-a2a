"""Typed provider failures retain R14's actual one-line attempt contract."""
import json
import re
import time

import pytest

from collab import logcap, worker, worker_runtime
from test_worker_service import conversation, configure, incoming, workers_allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['rate_limit','auth','model','network','other'])
@pytest.mark.parametrize('reported_usage',[False,True])
async def test_typed_failure_leaves_one_opaque_attempt_with_only_reported_usage(conversation,tmp_path,monkeypatch,kind,reported_usage):
    """Provider classification must not create another line or invent usage from an exit."""
    configure(conversation,tmp_path)
    incoming(conversation,1,'PRIVATE_PROMPT_SENTINEL')
    async def fail(*args,**kwargs):
        if reported_usage:
            kwargs['on_usage']({'tokens_in':7,'cost_usd':.01,'provider_text':'PRIVATE_STDERR_SENTINEL'})
        raise worker_runtime.WorkerRuntimeError('PRIVATE_STDERR_SENTINEL',kind=kind,retry_after=120)
    monkeypatch.setattr(worker_runtime,'run_turn',fail)
    began=time.time()
    await conversation.turn()
    assert logcap.flush()
    text=(conversation.root/'worker.log').read_text()
    rows=[json.loads(line) for line in text.splitlines()]
    assert len(rows)==1
    row=rows[0]
    assert re.fullmatch('[0-9a-f]{12}',row['attempt'])
    assert (row['phase'],row['outcome'],row['error'])==('launched','failed','provider-'+kind)
    assert 'PRIVATE_' not in text
    if reported_usage:
        assert row['tokens_in']==7 and row['cost_usd']==.01
    else:
        assert 'tokens_in' not in row and 'cost_usd' not in row
    assert 'tokens_out' not in row
    from collab.runtime_settings import get as setting
    assert conversation.next_at>=began+(900 if kind=='rate_limit' else 3600 if kind in ('auth','model') else setting('worker_retry_delay'))


@pytest.mark.asyncio
async def test_ledger_counts_the_combined_current_deferral_and_consent_decisions(conversation,tmp_path,monkeypatch):
    """Keep current consent routing while reporting what actually committed, not an old subset."""
    from collab.protocol import Envelope
    configure(conversation,tmp_path)
    incoming(conversation,1,'Question')
    conversation.daemon.inbox.record(Envelope(seq=2,kind='chat',sender='charlie',sender_id='p_c',to='alice',to_id='p_a',
        text='skill request',body={'skill_request':{'id':'sr_00000000aaaa','skill':'review','args':''}}))
    async def answer(*args,**kwargs):
        return {'summary':'','replies':[{'text':"I'll check and confirm.",'to':'bob','room':''}],'escalations':[]}
    monkeypatch.setattr(worker_runtime,'run_turn',answer)
    await conversation.turn()
    assert logcap.flush()
    row=json.loads((conversation.root/'worker.log').read_text())
    pending=worker.Store(conversation.root).pending()
    assert pending and row['phase']=='committed' and row['outcome']=='output'
    assert row['escalation_count']==len(pending)
    assert any('Only your human can consent' in item['question'] for item in pending)
