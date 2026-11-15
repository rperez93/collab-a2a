"""Synthetic own-ancestry files must keep lookup cost bounded before tmux IPC."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROBE = r'''
import json,os,resource,socket,subprocess,sys,tempfile,time
from pathlib import Path
from collab.client import tmux_context as c
from collab import hosttool

def memory():
 return {row.split(':')[0]:int(row.split()[1]) for row in Path('/proc/self/status').read_text().splitlines() if row.startswith(('VmRSS:','VmHWM:'))}
def record(pid,parent):
 fields=['0']*20;fields[0]='S';fields[1]=str(parent);fields[19]=str(pid)
 return (str(pid)+' (owned synthetic) '+' '.join(fields)).encode()
with tempfile.TemporaryDirectory(prefix='owned-tmux-proc-') as folder:
 root=Path(folder);proc=root/'proc';private=root/'private';private.mkdir(mode=0o700);endpoint=private/'s'
 with socket.socket(socket.AF_UNIX) as sock:sock.bind(str(endpoint))
 for pid,parent,tty in [(100,101,'/dev/null'),(101,102,'/dev/pts/7'),(102,1,'/dev/null')]:
  directory=proc/str(pid);(directory/'fd').mkdir(parents=True);(directory/'fd/0').symlink_to(tty);(directory/'stat').write_bytes(record(pid,parent));(directory/'environ').write_bytes(b'')
 metadata=f'TMUX={endpoint},444,0\0TMUX_PANE=%6\0'.encode();environment=proc/'101/environ';mode=sys.argv[1]
 if mode=='oversized':
  with environment.open('wb') as stream:
   stream.write(metadata)
   for _ in range(256):stream.write(b'x'*4096)
 elif mode=='fifo':environment.unlink();os.mkfifo(environment)
 else:environment.write_bytes(metadata)
 c._PROC=proc;c.os.getpid=lambda:100;c.own_tty=lambda **kwargs:'/dev/pts/7';hosttool.detect=lambda:'codex';c.shutil.which=lambda _: '/synthetic/tmux'
 os.environ.pop('TMUX',None);os.environ.pop('TMUX_PANE',None)
 c.sockets=lambda **kwargs:(_ for _ in ()).throw(AssertionError('unrelated socket scan'))
 calls=[]
 def query(*args,**kwargs):calls.append(True);return subprocess.CompletedProcess(args,0,'%6\t/dev/pts/7\n','')
 c.run=query;c._CACHE.clear();before=memory();usage=resource.getrusage(resource.RUSAGE_SELF);cpu=usage.ru_utime+usage.ru_stime;start=time.monotonic();fds=len(list(Path('/proc/self/fd').iterdir()))
 for _ in range(30):
  target=c.detect()
  assert (target==c.Target(str(endpoint),'%6')) if mode=='valid' else target is None
 assert len(list(Path('/proc/self/fd').iterdir()))==fds
 usage=resource.getrusage(resource.RUSAGE_SELF)
 print(json.dumps({'mode':mode,'lookups':30,'queries':len(calls),'wall_seconds':time.monotonic()-start,'self_cpu_seconds':usage.ru_utime+usage.ru_stime-cpu,'before_kib':before,'after_kib':memory(),'no_fd_growth':True,'scope':'Synthetic proc/host metadata only; no actual native host acceptance, provider, IPC client or user state'}))
'''


@pytest.mark.skipif(not Path('/proc/self/status').exists(), reason='Linux process-image memory unavailable')
@pytest.mark.parametrize('mode', ['valid', 'oversized', 'fifo'])
def test_repeated_ancestor_metadata_reads_are_bounded_and_close_their_descriptors(mode):
    """Measure SELF CPU/post-exec VmHWM, not fork-inherited ru_maxrss."""
    env = {**os.environ, 'COLLAB_NO_WORKER': '1', 'PYTHONNOUSERSITE': '1',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    result = subprocess.run([sys.executable, '-c', PROBE, mode], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    measured = json.loads(result.stdout)
    print(json.dumps(measured, sort_keys=True))
    assert measured['wall_seconds'] < 1
    assert measured['self_cpu_seconds'] < .5
    assert measured['after_kib']['VmHWM'] < 64 * 1024
    assert measured['after_kib']['VmHWM'] - measured['before_kib']['VmHWM'] < 8 * 1024
