"""The issue reports are reproduced with synthetic state, never live inboxes."""
from __future__ import annotations

import asyncio
import json
import os
import resource
import subprocess
import sys
import time

import pytest

from collab import config
from collab.client.hub_client import HubError
from collab.client.inbox import Inbox
from collab.client.recovery import repair_inbox
from collab.protocol import EXT_PREFIX, MAX_DETAIL, Envelope
from collab.server import hub as hub_module
from collab.server.hub import Hub
from collab.server.store import Store


@pytest.mark.parametrize('subject', ['task', 'project'])
@pytest.mark.parametrize('text', ['a' * 4001, 'é' * 4090 + 'TAIL', '  preserved\n' + 'a' * 4100 + '\n'], ids=['ascii', 'multibyte', 'whitespace'])
def test_long_details_and_comments_are_returned_and_stored_whole(client, host_headers, session, subject, text):
    """A successful response must never promise more content than was stored."""
    path = EXT_PREFIX + '/' + subject + 's'
    response = client.post(path, headers=host_headers, json={'action': 'propose', 'title': 'whole', 'detail': text})
    assert response.status_code == 200, response.text
    record = response.json()[subject]
    assert record['detail'] == text
    response = client.post(EXT_PREFIX + '/comments', headers=host_headers,
                           json={'subject': subject, 'id': record['id'], 'text': text})
    assert response.status_code == 200, response.text
    assert response.json()['comment']['text'] == text
    assert session['store'].comments(subject, record['id'])[0]['text'] == text


@pytest.mark.parametrize('subject', ['task', 'project'])
def test_oversized_content_is_refused_without_a_partial_record(client, host_headers, session, subject):
    """The finite content limit must fail before the storage mutation."""
    path = EXT_PREFIX + '/' + subject + 's'
    response = client.post(path, headers=host_headers,
                           json={'action': 'propose', 'title': 'too long', 'detail': 'é' * (MAX_DETAIL + 1)})
    assert response.status_code == 413
    assert '8,000' in response.json()['detail']
    response = client.post(path, headers=host_headers, json={'action': 'propose', 'title': 'target'})
    record = response.json()[subject]
    response = client.post(EXT_PREFIX + '/comments', headers=host_headers,
                           json={'subject': subject, 'id': record['id'], 'text': 'a' * (MAX_DETAIL + 1)})
    assert response.status_code == 413
    assert session['store'].comments(subject, record['id']) == []


def test_replay_hides_other_peoples_dms_but_advances_past_them(client, host_headers, session):
    """Empty private pages are not an end-of-history marker or a loss count."""
    store = session['store']
    first = store.append(Envelope(kind='chat', text='visible'))
    hidden = store.append(Envelope(kind='chat', text='secret', to='bob', to_id='p_bob', sender_id='p_carol'))
    last = store.append(Envelope(kind='chat', text='visible again'))
    assert client.get(EXT_PREFIX + '/replay').status_code == 401
    response = client.get(EXT_PREFIX + '/replay', headers=host_headers, params={'after': first.seq, 'limit': 1})
    page = response.json()
    assert page == {'events': [], 'cursor': hidden.seq, 'through': last.seq}
    response = client.get(EXT_PREFIX + '/replay', headers=host_headers,
                          params={'after': page['cursor'], 'through': page['through']})
    assert [item['text'] for item in response.json()['events']] == ['visible again']


def test_repair_recovers_only_visible_absent_events_without_rewinding_the_cursor(profile):
    """Late replay must preserve read flags and the daemon's high-water mark."""
    events = [Envelope(kind='chat', text=str(i), seq=i, sender='other') for i in (1, 3, 5)]
    box = Inbox(profile.dir)
    box.record(events[-1])
    box.mark_read([5])
    box.close()

    class Client:
        def replay_page(self, after, *, through=None, limit=200):
            return {'events': [e.to_dict() for e in events if e.seq > after], 'cursor': 5, 'through': 5}

    assert repair_inbox(profile, Client())['recovered'] == 2
    assert repair_inbox(profile, Client())['recovered'] == 0
    box = Inbox(profile.dir)
    assert box.last_seq() == 5
    assert [e.seq for e in box.take_unread()] == [1, 3]
    assert len(profile.dir.joinpath('inbox.jsonl').read_text().splitlines()) == 3
    box.close()


def test_repair_caps_a_peer_that_keeps_offering_more_pages(profile):
    """One repair has a finite page budget even when the peer has huge history."""
    class Client:
        def replay_page(self, after, *, through=None, limit=200):
            return {'events': [], 'cursor': after + 1, 'through': 1_000_000}
    result = repair_inbox(profile, Client(), max_pages=3)
    assert result == {'recovered': 0, 'cursor': 3, 'through': 1_000_000, 'complete': False, 'pages': 3, 'recovered_seqs': []}


async def test_a_slow_subscriber_is_disconnected_before_a_higher_seq_can_hide_loss(tmp_path, monkeypatch):
    """A flooding publisher cannot grow memory or silently evict replayable rows."""
    monkeypatch.setattr(hub_module, 'QUEUE_MAXSIZE', 2)
    store = Store(tmp_path / 'hub.db')
    hub = Hub(store, session_id='s', host_name='host')
    sub = await hub.subscribe('alice')
    started, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for i in range(10_000):
        await hub._deliver(Envelope(kind='chat', text='x' * 1000, seq=i + 1))
    assert sub.close_reason == 'slow-consumer'
    assert sub.queue.qsize() == 1 and await sub.queue.get() is None
    assert hub.slow_subscriber_disconnects == 1
    assert time.process_time() - cpu < 2
    assert time.monotonic() - started < 3
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 16 * 1024
    store.close()


def test_a_bound_custom_home_survives_a_fresh_process_and_is_actor_scoped(tmp_path, monkeypatch):
    """An explicit choice must outlive the shell without becoming global state."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / 'custom'
    profile = config.SessionProfile(session_id='s', url='http://hub', name='b', host_name='a', token='t', home=str(home))
    profile.save()
    assert config.bind_home(home)
    code = 'from collab.config import collab_home; print(collab_home())'
    env = dict(os.environ)
    env.pop('COLLAB_HOME', None)
    assert subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip() == str(home)
    env['COLLAB_AGENT_ID'] = 'other'
    assert subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip() != str(home)
    (home / 'current').unlink()
    with pytest.raises(config.HomeSelectionError, match='missing'):
        config.collab_home()
    monkeypatch.setenv('COLLAB_HOME', str(home))
    assert config.collab_home() == home


def test_an_unidentified_process_refuses_multiple_existing_agent_sessions(tmp_path, monkeypatch):
    """Losing the target ID cannot quietly turn active sessions into «offline»."""
    monkeypatch.chdir(tmp_path)
    for actor in ('first', 'second'):
        monkeypatch.setenv('COLLAB_AGENT_ID', actor)
        home = config.ensure_home()
        config.SessionProfile(session_id='s', url='http://hub', name=actor, host_name='host', token='t', home=str(home)).save()
    for key in config.AGENT_SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(config.HomeSelectionError, match='stable agent ID'):
        config.collab_home()


async def test_overflow_ends_the_feed_and_reconnection_replays_every_durable_event(tmp_path, monkeypatch):
    """Execute the actual SSE generator so a close sentinel cannot be ignored."""
    from collab.server.events import event_stream

    class Request:
        headers = {"last-event-id": "0"}
        query_params = {}
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(hub_module, 'QUEUE_MAXSIZE', 2)
    store = Store(tmp_path / 'hub.db')
    hub = Hub(store, session_id='s', host_name='host')
    response = await event_stream(Request(), hub, 'alice')
    stream = response.body_iterator
    assert (await anext(stream))['event'] == 'ready'
    for i in range(3):
        await hub.publish(Envelope(kind='chat', text=str(i)))
    assert (await anext(stream))['event'] == 'retry'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not hub.connected()
    response = await event_stream(Request(), hub, 'alice')
    stream = response.body_iterator
    frames = [await anext(stream) for _ in range(3)]
    assert [json.loads(frame['data'])['text'] for frame in frames] == ['0', '1', '2']
    assert (await anext(stream))['event'] == 'ready'
    await stream.aclose()
    store.close()


def test_diagnostic_gap_sampling_has_a_fixed_memory_budget(tmp_path):
    """A billion hidden sequence numbers must not allocate a billion integers."""
    box = Inbox(tmp_path)
    box.record(Envelope(kind='chat', seq=1))
    box.record(Envelope(kind='chat', seq=1_000_000_000))
    started, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    gaps = box.gaps()
    assert gaps == list(range(2, 1002))
    assert time.process_time() - cpu < 0.5
    assert time.monotonic() - started < 1
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 8 * 1024
    box.close()


def test_a_partial_repair_keeps_durable_notices_after_failure_and_retry(profile):
    """A worker cursor ahead of recovered history must not silently hide it."""
    class Client:
        def replay_page(self, after, *, through=None, limit=200):
            if after:
                raise HubError('disconnected on second page')
            return {'events': [Envelope(kind='chat', seq=1).to_dict()], 'cursor': 1, 'through': 2}
    with pytest.raises(HubError, match='second page'):
        repair_inbox(profile, Client())
    box = Inbox(profile.dir)
    assert box.pending_repairs() == [1]
    assert not box.record(Envelope(kind='chat', seq=1), repaired=True)
    box.acknowledge_repairs([1])
    assert box.pending_repairs() == []
    box.close()


def test_a_failed_inbox_write_cannot_leave_a_phantom_repair_notice(profile, monkeypatch):
    """The repair marker and event commit or roll back together."""
    box = Inbox(profile.dir)
    box.jsonl.mkdir()
    with pytest.raises(IsADirectoryError):
        box.record(Envelope(kind='chat', seq=1), repaired=True)
    assert box.pending_repairs() == []
    assert box.last_seq() == 0
    box.close()


def test_a_flooding_replay_peer_is_cut_off_with_bounded_cpu_and_memory():
    """The response must be capped while arriving, before JSON buffering."""
    import httpx
    from collab.client.hub_client import HubClient

    class Flood(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            while True:
                yield b'x' * 65536
        def close(self):
            self.closed = True

    stream = Flood()
    client = HubClient('http://testserver', 'token')
    client._client.close()
    from collab.compatibility import advertisement
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(json.dumps(advertisement()).encode()))
        if request.url.path.endswith('/compatibility') else httpx.Response(200, stream=stream)))
    started, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        with pytest.raises(HubError, match='8192 KiB'):
            client.replay_page()
    finally:
        client.close()
    assert stream.closed
    assert time.process_time() - cpu < 1
    assert time.monotonic() - started < 2
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 24 * 1024


def test_a_replay_peer_cannot_defeat_the_deadline_by_dribbling_bytes(monkeypatch):
    """Each received chunk returns to the clock, even without a newline."""
    import httpx
    from collab.client.hub_client import HubClient
    clock = [0.0]
    class Dribble(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                clock[0] += 4
                yield b'x'
    client = HubClient('http://testserver', 'token')
    client._client.close()
    from collab.compatibility import advertisement
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(json.dumps(advertisement()).encode()))
        if request.url.path.endswith('/compatibility') else httpx.Response(200, stream=Dribble())))
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    try:
        with pytest.raises(HubError, match='deadline'):
            client.replay_page()
    finally:
        client.close()
    assert clock[0] == 16


def test_a_silent_replay_peer_times_out_without_busy_waiting():
    """A peer sending no body still meets the I/O budget and releases the socket."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from collab.client.hub_client import HubClient
    release = threading.Event()

    class Silent(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith('/compatibility'):
                from collab.compatibility import advertisement
                body = json.dumps(advertisement()).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.flush()
            release.wait(10)
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Silent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        with HubClient(f'http://127.0.0.1:{server.server_port}', 'token') as client:
            with pytest.raises(HubError, match='cannot read replay'):
                client.replay_page()
        assert 4.5 < time.monotonic() - started < 7
        assert time.process_time() - cpu < 1
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 16 * 1024
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
