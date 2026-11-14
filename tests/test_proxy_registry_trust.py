"""The proxy must establish local routing authority before forwarding a bearer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.testclient import TestClient

from collab import peers, proxy, routes


@pytest.fixture
def upstream():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def announce(tmp_path, monkeypatch, address):
    directory = tmp_path / 'peers'
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(directory))
    record = peers.announce(session_id='s_test', name='host', role='host',
        url=address, local_url=address, repo='/unused', home='/unused', pid=os.getpid())
    return directory, record


@pytest.mark.parametrize('unsafe', ['directory_mode', 'record_mode', 'directory_symlink',
                                  'record_symlink', 'wrong_owner', 'wrong_user', 'wrong_machine',
                                  'malformed_address'])
def test_untrusted_registry_never_receives_synthetic_bearer(tmp_path, monkeypatch, upstream, unsafe):
    address, received = upstream
    directory, record = announce(tmp_path, monkeypatch, address)
    if unsafe == 'directory_mode':
        directory.chmod(0o777)
    elif unsafe == 'record_mode':
        record.chmod(0o666)
    elif unsafe == 'directory_symlink':
        link = tmp_path / 'registry-link'
        link.symlink_to(directory, target_is_directory=True)
        monkeypatch.setenv('COLLAB_PEERS_DIR', str(link))
    elif unsafe == 'record_symlink':
        target = tmp_path / 'foreign-record'
        record.rename(target)
        record.symlink_to(target)
    elif unsafe == 'wrong_owner':
        uid = os.getuid()
        monkeypatch.setattr(routes.os, 'getuid', lambda: uid + 1)
    else:
        data = json.loads(record.read_text())
        if unsafe == 'malformed_address':
            data.update(local_url={'host': '127.0.0.1'}, url=['http://127.0.0.1:1'])
        else:
            data['user' if unsafe == 'wrong_user' else 'machine_id'] = 'not-this-owner'
        record.write_text(json.dumps(data))
    with TestClient(proxy.build_app(proxy.Routes(ttl=0), primary=lambda: 's_test')) as client:
        for path in ('/s/s_test/health', '/health'):
            response = client.get(path, headers={'Authorization': 'Bearer synthetic-test-only'})
            assert response.status_code == 404
    assert received == []


@pytest.mark.parametrize('conflicting', [True, False])
def test_competing_hosts_are_not_folded_into_an_arbitrary_route(tmp_path, monkeypatch, upstream, conflicting):
    address, received = upstream
    directory, record = announce(tmp_path, monkeypatch, address)
    other = json.loads(record.read_text())
    if conflicting:
        other.update(url='http://127.0.0.1:1', local_url='http://127.0.0.1:1')
    duplicate = directory / 'another-host.json'
    duplicate.write_text(json.dumps(other))
    duplicate.chmod(0o600)
    with TestClient(proxy.build_app(proxy.Routes(ttl=0), primary=lambda: 's_test')) as client:
        response = client.get('/s/s_test/health', headers={'Authorization': 'Bearer synthetic-test-only'})
        assert response.status_code == (404 if conflicting else 200)
    assert received == ([] if conflicting else ['Bearer synthetic-test-only'])


@pytest.mark.parametrize('kind', ['nonjson', 'oversized', 'fifo'])
def test_hostile_registry_scan_is_complete_or_refused_with_bounded_work(tmp_path, kind):
    """Measure the actual proxy route reader in a child, including a valid prefix.

    A valid first record must not become a route if the work cap leaves the
    remainder unseen: a conflicting record could be beyond the cutoff.
    """
    if not hasattr(os, 'mkfifo') or not Path('/proc/self/status').exists():
        pytest.skip('POSIX FIFO and Linux RSS measurement')
    code = r'''
import json,os,sys,time
from pathlib import Path
from types import SimpleNamespace
from collab import peers,proxy,routes
root=Path(sys.argv[1]); root.mkdir(mode=0o700)
os.environ['COLLAB_PEERS_DIR']=str(root)
record=peers.announce(session_id='s_test',name='host',role='host',
    url='http://127.0.0.1:1',repo='/unused',home='/unused',pid=os.getpid())
kind=sys.argv[2]
hostile=root/'hostile.json'
if kind=='fifo': os.mkfifo(hostile,0o600)
elif kind=='oversized':
    hostile.write_bytes(b'x'*(routes.MAX_ROUTE_BYTES+1)); hostile.chmod(0o600)
class Entries:
    total=0
    def __init__(self): self.count=0
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def __iter__(self): return self
    def __next__(self):
        self.count+=1; Entries.total+=1
        assert self.count<=routes.MAX_REGISTRY_ENTRIES,'read past registry budget'
        return SimpleNamespace(name=record.name if self.count==1 else
            'ignored.txt' if kind=='nonjson' else 'hostile.json')
routes.os.scandir=lambda *args: Entries()
def rss():
    return int(next(x.split()[1] for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('VmRSS:')))
before=rss(); cpu=time.process_time(); wall=time.monotonic()
for _ in range(10): assert proxy.Routes(ttl=0).table()=={}
result={'kind':kind,'cpu_seconds':time.process_time()-cpu,'wall_seconds':time.monotonic()-wall,
        'rss_growth_kib':max(0,rss()-before),'entries':Entries.total}
assert result['entries']==10*routes.MAX_REGISTRY_ENTRIES
assert result['cpu_seconds']<3 and result['wall_seconds']<8,result
assert result['rss_growth_kib']<16*1024,result
print(json.dumps(result))
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'registry'), kind],
                            env=environment, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
    measured = json.loads(result.stdout)
    print(json.dumps(measured))
    assert measured['entries'] == 10 * routes.MAX_REGISTRY_ENTRIES
