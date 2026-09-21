"""Opt-in real-provider verification. Uses billed Claude/Codex calls with synthetic
prompts and isolated Collab state. Run only when explicitly authorized.
No messages are sent to a session; publication is checked with MockTransport.
"""
import asyncio,json,os,tempfile,time
from pathlib import Path
from collab.worker_runtime import run_turn, DEFAULT_MODELS
from collab.worker import Store, metrics
from collab.config import SessionProfile
from collab.client.daemon import Daemon
import httpx

async def main():
    with tempfile.TemporaryDirectory(prefix='collab-native-verification-') as temp:
        root=Path(temp)
        os.environ.update(COLLAB_CONFIG=str(root/'config.json'),COLLAB_HOME=str(root/'home'),COLLAB_STATE_DIR=str(root/'state'),COLLAB_PEERS_DIR=str(root/'peers'),COLLAB_NO_UPDATE_CHECK='1')
        (root/'config.json').write_text('{}')
        for agent in ('claude','codex'):
            p=SessionProfile(session_id='synthetic-'+agent,url='https://synthetic.invalid',name='test-agent',host_name='synthetic',token='synthetic-token',home=str(root/agent),participant_id='p_test')
            p.save()
            store=Store(p.dir)
            store.configure({'agent':agent,'model':DEFAULT_MODELS[agent],'scope':'Verify usage capture in one synthetic turn.'})
            started=time.monotonic()
            row={'agent':agent,'model':DEFAULT_MODELS[agent]}
            try:
                result=await run_turn(agent,DEFAULT_MODELS[agent],{'self':{'name':'test-agent'},'scope':'Telemetry verification only. Return summary "verified" and empty replies and escalations. No tools or external actions.','messages':[]},p.dir,timeout=90,on_usage=store.record_usage)
                usage=metrics(p.dir)
                assert usage.get('tokens_in',0)+usage.get('tokens_out',0)>0, usage
                daemon=Daemon(p)
                sent=[]
                def receive(request):
                    sent.append(json.loads(request.content))
                    return httpx.Response(200,json={'ok':True})
                try:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
                        await daemon._report_stats(client)
                    assert sent and sent[-1]['stats']['worker']['tokens_out']==usage['tokens_out']
                    assert 'tokens_out' not in sent[-1]['stats']
                finally:
                    daemon.inbox.close()
                row.update(ok=True,usage={k:usage[k] for k in ('model','tokens_in','tokens_out','tokens_cached','tokens_cache_write','cost_usd','cost_kind') if k in usage},valid_result=isinstance(result,dict),published_separately=True)
            except Exception as exc:
                row.update(ok=False,error=str(exc))
            row['wall_seconds']=round(time.monotonic()-started,3)
            print(json.dumps(row),flush=True)
if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-provider-calls',action='store_true')
    if not parser.parse_args().allow_provider_calls:
        parser.error('pass --allow-provider-calls only with explicit authorization')
    asyncio.run(main())
