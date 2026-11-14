"""Own TCP callbacks are bounded without a real hub/browser/authenticator."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROBE = r'''
import base64,hashlib,json,resource,sys,threading,time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from collab.client import passkey as page,passkey_dispatch as seam

def memory():
 return {row.split(':')[0]:int(row.split()[1]) for row in Path('/proc/self/status').read_text().splitlines() if row.startswith(('VmRSS:','VmHWM:'))}
def b64(value):return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
raw=json.dumps({'api_version':1,'requester_id':'p_peer','subject':'Synthetic','destination':'local','authority':'test','payload':{}}).encode()
request={'request_id':'ar_resource','requester_id':'p_peer','requester_name':'Synthetic','subject':'Synthetic','destination':'local','authority':'test','digest':hashlib.sha256(raw).hexdigest(),'canonical_review':b64(raw),'credential_id':b64(b'owned-key'),'expires_at':int(time.time())+60}
release=threading.Event();threads=[];mode=sys.argv[1];seam.CALLBACK_SECONDS=.05;ports=[]
def callback(**kwargs):
 threads.append(threading.current_thread())
 if mode=='silence':release.wait(2)
 return {'padding':'x'*(1024*1024)}
def opener(url):
 parsed=urlsplit(url);origin=f'{parsed.scheme}://{parsed.netloc}';ports.append(parsed.port)
 headers={'Origin':origin,'X-Collab-Approval-Token':parsed.fragment}
 with httpx.Client(trust_env=False,timeout=2) as client:
  assert client.get(origin+parsed.path+'/review',headers=headers).status_code==200
  response=client.post(origin+parsed.path+'/approve/options',headers=headers,json={})
  assert response.status_code>=400,response.status_code
 return True
page.webbrowser.open_new=opener
before=memory();usage=resource.getrusage(resource.RUSAGE_SELF);cpu=usage.ru_utime+usage.ru_stime;started=time.monotonic()
try:
 result=page.approve_with_passkey(request,callback)
 assert result=={'choice':'unavailable'},result
 assert threads and all(thread.daemon for thread in threads)
 # The owned listener must be gone before the synchronous function returns.
 for port in ports:
  try:httpx.get('http://127.0.0.1:'+str(port),timeout=.1,trust_env=False)
  except httpx.TransportError:pass
  else:raise AssertionError('listener survived synchronous return')
finally:
 release.set()
 for thread in threads:thread.join(.5)
assert all(not thread.is_alive() for thread in threads)
usage=resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({'mode':mode,'result':result['choice'],'wall_seconds':time.monotonic()-started,'cpu_seconds':usage.ru_utime+usage.ru_stime-cpu,'before_kib':before,'after_kib':memory(),'owned_callback_threads':len(threads),'threads_daemon_and_joined':True,'listener_closed':True,'scope':'Own synthetic TCP page and callback only; no browser, authenticator, core approval, provider or child process'}))
'''


@pytest.mark.skipif(not Path('/proc/self/status').exists(), reason='Linux process-image memory unavailable')
@pytest.mark.parametrize('mode', ['silence', 'oversized'])
def test_a_synchronous_page_bounds_hostile_callback_time_and_data(mode):
    """Measure process SELF CPU and post-exec VmHWM, never inherited ru_maxrss."""
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'COLLAB_NO_WORKER': '1', 'PYTHONNOUSERSITE': '1'}
    result = subprocess.run([sys.executable, '-c', PROBE, mode], env=env,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stdout + result.stderr
    measured = json.loads(result.stdout)
    print(json.dumps(measured, sort_keys=True))
    assert measured['wall_seconds'] < 2
    assert measured['cpu_seconds'] < .5
    assert measured['after_kib']['VmHWM'] < 128 * 1024
    assert measured['after_kib']['VmHWM'] - measured['before_kib']['VmHWM'] < 16 * 1024
