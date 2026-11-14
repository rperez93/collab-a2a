"""Independent regression probe: an old provider failure cannot alert a new config."""
import pytest
from collab import worker, worker_runtime
from test_worker_service import conversation, configure, incoming, workers_allowed

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["auth", "model"])
async def test_old_generation_provider_error_does_not_escalate_new_configuration(conversation, tmp_path, monkeypatch, kind):
    configure(conversation, tmp_path)
    store=worker.Store(conversation.root)
    incoming(conversation,1,'Question')
    async def run(*args,**kwargs):
        store.configure({**store.snapshot()['config'], 'scope':'Replacement configuration'})
        raise worker_runtime.WorkerRuntimeError('old provider auth failure',kind=kind)
    monkeypatch.setattr(worker_runtime,'run_turn',run)
    await conversation.turn()
    assert store.status()['error']=='', 'old health writes already have a generation guard'
    assert store.pending()==[], 'old-generation provider error must not create a decision for the new configuration'
