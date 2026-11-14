"""The owner-head refresh rejects encoded responses and bounds the whole exchange."""
import asyncio
import time
import httpx
import pytest
from collab.client import daemon as module
from collab.config import SessionProfile


def instance(tmp_path):
    profile = SessionProfile('s_bounds', 'http://old', 'viewer', 'host', 'token', home=str(tmp_path))
    daemon = module.Daemon(profile)
    daemon.paths.root.mkdir(parents=True, exist_ok=True)
    return daemon


@pytest.mark.asyncio
async def test_encoded_skills_are_refused_before_httpx_can_inflate_a_chunk(tmp_path):
    """Requesting identity alone allowed a 32KiB gzip body to allocate 65MiB."""
    import zlib
    compressor = zlib.compressobj(wbits=31)
    blocks = [compressor.compress(b'x' * 65536) for _ in range(512)]
    encoded = b''.join(blocks) + compressor.flush()
    reads = []
    class Encoded(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield encoded
    daemon = instance(tmp_path)
    async def transport(request):
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=Encoded())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await daemon._fetch_skills(client, ('encoded',))
    assert reads == [] and not daemon.paths.skills.exists()
    assert daemon._skills_retry_at > time.time()
    assert getattr(daemon, '_skills_etag', None) is None


@pytest.mark.asyncio
async def test_a_real_trickling_skill_response_has_a_whole_exchange_deadline(tmp_path, monkeypatch):
    """Individual timely TCP reads must not hold the named refresh task forever."""
    ended = asyncio.Event()
    async def peer(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), .5)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n')
            await writer.drain()
            for _ in range(100):
                writer.write(b' ')
                await writer.drain()
                await asyncio.sleep(.02)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass
            ended.set()
    server = await asyncio.start_server(peer, '127.0.0.1', 0)
    daemon = instance(tmp_path)
    daemon.profile.url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
    monkeypatch.setattr(module, 'SKILLS_FETCH_SECONDS', .06)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await asyncio.wait_for(daemon._fetch_skills(client, ('trickle',)), .8)
        await asyncio.wait_for(ended.wait(), .8)
    finally:
        server.close()
        await server.wait_closed()
    assert not daemon.paths.skills.exists() and daemon._skills_retry_at > time.time()


@pytest.mark.asyncio
async def test_a_deeply_nested_skill_list_backs_off(tmp_path):
    """A hostile hub's nesting raised RecursionError past the handler and skipped the backoff."""
    body = b'[' * 200000 + b']' * 200000
    daemon = instance(tmp_path)
    async def transport(request):
        return httpx.Response(200, headers={'content-length': str(len(body))}, content=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await daemon._fetch_skills(client, ('nested',))
    assert daemon._skills_retry_at > time.time() and not daemon.paths.skills.exists()
