"""Bounded local pickup polling under oversized, deep, locked and silent inputs."""
import json
import os
from pathlib import Path
import resource
import sqlite3
import tempfile
import time

with tempfile.TemporaryDirectory(prefix='collab-pickup-bench-') as temp:
    root = Path(temp)
    os.environ['COLLAB_CONFIG'] = str(root/'config.json')
    (root/'config.json').write_text('{}')
    from collab import task_pickup
    snapshot = {'fetched_at':1000, 'you':'alice', 'you_id':'p_a', 'batch':{'id':'b'},
                'participants':[], 'projects':[], 'tasks':[{'id':'t','batch':'b','state':'TASK_STATE_SUBMITTED'}]}
    status = {'state':'live','heartbeat':1000,'activity':{'state':'idle','since':500}}
    path = root/'snapshot.json'
    path.write_text(json.dumps(snapshot))
    (root/'status.json').write_text(json.dumps(status))
    rows=[]
    def measure(name, count, action):
        wall,cpu=time.monotonic(),time.process_time()
        for _ in range(count): action()
        rows.append({'case':name,'iterations':count,'wall_seconds':time.monotonic()-wall,
                     'cpu_seconds':time.process_time()-cpu,
                     'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    measure('normal snapshot',1000,lambda:task_pickup.opportunity(root,1000))
    notice=task_pickup.claim(root,now=1000)
    db=sqlite3.connect(root/'task-pickup-notices.db')
    db.execute('BEGIN IMMEDIATE')
    measure('locked ledger',100,lambda:task_pickup.claim(root,now=1000))
    db.rollback();db.close()
    path.write_bytes(b'x'*(3*1024*1024))
    measure('oversized record',100,lambda:task_pickup.claim(root,now=1000))
    path.write_text('['*2000+']'*2000)
    measure('deep JSON',100,lambda:task_pickup.claim(root,now=1000))
    path.unlink();os.mkfifo(path)
    measure('silent FIFO',100,lambda:task_pickup.claim(root,now=1000))
    print(json.dumps(rows,indent=2))
