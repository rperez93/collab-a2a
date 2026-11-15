"""Measure the newly added worker-enabled spool scan, never a native provider."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROBE = r'''
import asyncio,json,os,resource,sys,tempfile,time
from pathlib import Path
from collab import wake,worker_notices
from collab.client.daemon import Daemon,DaemonPaths
from collab.config import SessionProfile
from collab.protocol import Envelope

def memory():
 return {line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('VmHWM:','VmRSS:'))}

async def run(root):
 mode=sys.argv[1]
 profile=SessionProfile(session_id='s_resource',name='synthetic',host_name='synthetic-host',participant_id='p_synthetic',token='synthetic-only',url='http://not-contacted.invalid',home=str(root/'profile'))
 profile.dir.mkdir(parents=True,exist_ok=True)
 wake.write_config(profile.dir,wake.WakeConfig(command=['true'],delivery='full',settle=0,min_gap=0))
 subject=Daemon.__new__(Daemon);subject.profile=profile;subject.paths=DaemonPaths(profile.dir);subject._waking=None
 subject.waker=wake.Waker(profile.dir,profile.session_id,now=lambda:1000.0,attended=lambda:False)
 subject.waker.ensure();delivered=[]
 worker_notices.enabled=lambda _:True
 async def quiet():pass
 async def deliver(batch,reminder):
  rows=batch.events();assert rows and all(e['kind']=='system' for e in rows)
  assert 'ordinary-private' not in subject.waker.prompt(batch)
  delivered.extend(e['seq'] for e in rows);subject.waker.succeeded(batch)
 subject._maybe_worker_notice=subject._maybe_task_pickup=quiet;subject._wake=deliver
 if mode=='backlog':
  for seq in range(1,41):subject.waker.note(Envelope(kind='chat',sender='peer',seq=seq,text='ordinary-private'+'🙂'*7980))
 elif mode=='oversized':
  with (subject.waker.batches/'1.jsonl').open('wb') as file:
   for _ in range(128):file.write(b'x'*65536)
  subject.waker.note(Envelope(kind='system',sender='collab',seq=1,body={'event':'approval-security'},text='synthetic alarm'))
 elif mode=='deep':subject.waker.pending.write_text('['*2000+'0'+']'*2000+'\n')
 elif mode=='fifo':os.mkfifo(subject.waker.pending)
 before=memory();usage=resource.getrusage(resource.RUSAGE_SELF);cpu=usage.ru_utime+usage.ru_stime;start=time.monotonic()
 for _ in range(20):await subject._maybe_wake()
 assert delivered==[]
 if mode=='backlog':
  # Repeated alarm-only success must reuse the ordinary remainder rather
  # than add a new outstanding file per alarm and grow heartbeat work.
  for seq in range(100,160):
   subject.waker.note(Envelope(kind='chat',sender='peer',seq=seq*2,text='ordinary-private'))
   subject.waker.note(Envelope(kind='system',sender='collab',seq=seq*2+1,body={'event':'approval-security'},text='synthetic alarm'))
   await subject._maybe_wake();assert subject._waking is not None;await subject._waking
   assert not subject.waker.for_main_pending()
   assert len(subject.waker.outstanding())==1
  assert len(delivered)==60
  ordinary=subject.waker.take().events();assert any(e['kind']=='chat' for e in ordinary)
  assert len(ordinary)<=wake.MAX_BATCH+1
 usage=resource.getrusage(resource.RUSAGE_SELF)
 print(json.dumps({'mode':mode,'wall_seconds':time.monotonic()-start,'cpu_seconds':usage.ru_utime+usage.ru_stime-cpu,'before_kib':before,'after_kib':memory(),'alarm_deliveries':len(delivered),'scope':'Own synthetic daemon/spool; no network/provider/process launch'}))

with tempfile.TemporaryDirectory(prefix='owned-security-wake-') as folder:asyncio.run(run(Path(folder)))
'''


@pytest.mark.skipif(not Path('/proc/self/status').exists(), reason='Linux process-image memory unavailable')
@pytest.mark.parametrize('mode', ['backlog', 'oversized', 'fifo', 'deep'])
def test_the_worker_alarm_path_bounds_normal_backlog_and_damaged_spools(mode):
    """Twenty heartbeat checks and sixty mixed deliveries cannot grow scan work.

    CPU is the fresh process's SELF user+system during the measured exchange;
    memory is Linux post-exec VmHWM/VmRSS, not inherited ru_maxrss. No native
    command is executed: the daemon delivery coroutine is an explicit fixture.
    """
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'COLLAB_NO_WORKER': '1', 'PYTHONNOUSERSITE': '1'}
    result = subprocess.run([sys.executable, '-c', PROBE, mode], env=env, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    measured = json.loads(result.stdout)
    print(json.dumps(measured, sort_keys=True))
    assert measured['wall_seconds'] < 4
    assert measured['cpu_seconds'] < 2
    assert measured['after_kib']['VmHWM'] < 128 * 1024
    assert measured['after_kib']['VmHWM'] - measured['before_kib']['VmHWM'] < 32 * 1024
