"""Repeat the synthetic bounded-cardinality worker accounting stress check."""
import json, os, resource, tempfile, time, tracemalloc
from pathlib import Path
from collab import worker, config
with tempfile.TemporaryDirectory(prefix='collab-v201-stats-soak-') as temporary:
    root=Path(temporary)
    os.environ['COLLAB_CONFIG']=str(root/'config.json')
    os.environ['COLLAB_HOME']=str(root/'home')
    os.environ['COLLAB_STATE_DIR']=str(root/'state')
    config._CACHE.clear()
    store=worker.Store(root/'worker')
    tracemalloc.start()
    began=time.monotonic(); cpu=time.process_time(); samples=[]
    for index in range(1000):
        store.record_usage({'model':f'model-{index}', 'tokens_in':100,'tokens_out':20,'tokens_cache_write':5,'cost_usd':.0001})
        store.report_stats({'source':'adapter','quota_scope':'independent','quotas':{'five_hour':index%100},'context_tokens':index})
        if index%100==99:
            import gc
            gc.collect()
            samples.append(tracemalloc.get_traced_memory()[0])
    result={'iterations':1000,'wall_seconds':time.monotonic()-began,'cpu_seconds':time.process_time()-cpu,
        'tracemalloc_current_peak_bytes':tracemalloc.get_traced_memory(),'retained_samples_bytes':samples,
        'model_buckets':len(store.status()['usage_by_model']),'database_bytes':store.path.stat().st_size,
        'fd_count':len(list(Path('/proc/self/fd').iterdir()))}
    result['process_memory']={line.split(':')[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('VmRSS:','VmHWM:'))}
    print(json.dumps(result,indent=2))
