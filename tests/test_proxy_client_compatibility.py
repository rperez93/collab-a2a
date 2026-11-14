"""Real clients through the proxy, including an optional pinned 2.1.2 checkout.

Set COLLAB_LEGACY_CLIENT_SRC to the src directory of tag v2.1.2 to exercise
the old implementation in a separate interpreter; no source is downloaded.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from collab import peers, proxy
from collab.server.tunnel import free_port


CLIENT = r'''
import json, sys
import collab
from collab.client.hub_client import HubClient
from collab.protocol import Envelope
args = json.load(sys.stdin)
if args['legacy']:
    assert collab.__version__ == '2.1.2', collab.__version__
with HubClient(args['base']) as client:
    assert client.health()['session_id'] == 's_test'
    joined = client.join(args['invite'], 'compat-client', {})
    client.token = joined['token']
    assert client.snapshot()['session_id'] == 's_test'
    client.send(Envelope(kind='chat', text='proxy compatibility marker', room='general'))
    assert any(event.text == 'proxy compatibility marker' for event in client.history())
print(json.dumps({'version': collab.__version__, 'joined': True, 'round_trip': True}))
'''


@pytest.fixture
def proxy_server(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    peers.announce(session_id='s_test', name='alice', role='host',
                   url=live_server['base'], local_url=live_server['base'],
                   repo=str(tmp_path), home=str(tmp_path), pid=os.getpid())
    port = free_port()
    app = proxy.build_app(primary=lambda: 's_test')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(base + '/ext/collab/v1/health', timeout=.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.05)
        else:
            pytest.fail('proxy did not become ready')
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), 'test proxy did not stop'


@pytest.mark.parametrize('legacy', [False, True], ids=['current', 'v2.1.2'])
@pytest.mark.parametrize('route', ['routed', 'legacy-root', 'direct-hub'])
def test_client_join_and_message_round_trip(route, legacy, proxy_server, live_server, tmp_path):
    source = os.environ.get('COLLAB_LEGACY_CLIENT_SRC') if legacy else str(Path(__file__).resolve().parents[1] / 'src')
    if not source:
        pytest.skip('set COLLAB_LEGACY_CLIENT_SRC to the v2.1.2 src checkout')
    assert (Path(source) / 'collab' / '__init__.py').is_file()
    base = (live_server['base'] if route == 'direct-hub' else
            proxy_server + ('/s/s_test' if route == 'routed' else ''))
    env = {**os.environ, 'PYTHONPATH': str(Path(source).resolve()), 'COLLAB_NO_WORKER': '1'}
    result = subprocess.run([sys.executable, '-c', CLIENT], cwd=tmp_path, env=env,
                            input=json.dumps({'base': base, 'invite': live_server['invite'], 'legacy': legacy}),
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['round_trip'] is True


def test_unknown_routed_session_never_falls_back_to_primary(proxy_server, live_server):
    response = httpx.post(proxy_server + '/s/s_missing/ext/collab/v1/join',
                          json={'invite': live_server['invite'], 'name': 'wrong-route'}, timeout=5)
    assert response.status_code == 404
