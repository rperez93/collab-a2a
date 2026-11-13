"""A fast hub must not turn a burst of presence into one roster per event."""
import asyncio
import json
import resource
import time

import httpx
import pytest

from collab.client.daemon import Daemon, SNAPSHOT_COALESCE


async def settle(daemon):
    await asyncio.wait_for(daemon._refresh_tasks["participants"], 2)


async def test_periodic_reads_keep_the_scheduler_deadline_instead_of_skipping_a_heartbeat(profile):
    """Completing a read after its launch must not postpone an already due timer."""
    daemon = Daemon(profile)
    calls = []
    async def read(client):
        calls.append(time.monotonic())
        await asyncio.sleep(.002)
    daemon._refresh_snapshot = read
    try:
        for _ in range(8):
            daemon._schedule_refresh("participants", lambda: daemon._poll_snapshot(None), interval=.03)
            await asyncio.sleep(.035)
        await settle(daemon)
        assert len(calls) >= 7
        assert max(b - a for a, b in zip(calls, calls[1:])) < .06
    finally:
        daemon.inbox.close()


async def test_a_hundred_spaced_events_share_one_request_without_delaying_chat(profile):
    """Previously a fast response completed before each next event: 100 GETs."""
    daemon = Daemon(profile)
    calls = []
    cpu, rss, began = time.process_time(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, time.monotonic()
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"participants": [], "seq": 100})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for _ in range(100):
                daemon._request_snapshot(client)
                # Separate event-loop turns: the old task completed every time.
                await asyncio.sleep(0)
            await settle(daemon)
        assert len(calls) == 1
        assert daemon.snapshot["seq"] == 100
        assert time.monotonic() - began < 1
        # Measured bounds prevent replacing HTTP amplification with a spin or
        # an event-sized queue: 100 invalidations retain one bit and one task.
        assert time.process_time() - cpu < .5
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss < 16 * 1024
    finally:
        daemon.inbox.close()


@pytest.mark.parametrize("periodic", [False, True])
async def test_an_event_during_an_inflight_read_gets_one_trailing_read(profile, periodic):
    """The hub can snapshot before a task completes and deliver it afterward."""
    daemon = Daemon(profile)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handle(request):
        calls.append(request)
        version = len(calls)
        if version == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"participants": [], "seq": version})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            if periodic:
                daemon._schedule_refresh("participants", lambda: daemon._poll_snapshot(client))
            else:
                daemon._request_snapshot(client)
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(100):
                daemon._request_snapshot(client)
            release.set()
            await settle(daemon)
        assert len(calls) == 2
        assert daemon.snapshot["seq"] == 2
        assert daemon._refresh_due["participants"] > time.monotonic()
    finally:
        release.set()
        daemon.inbox.close()


async def test_continuous_events_cannot_postpone_the_first_read_forever(profile):
    """A sliding debounce starves the board in a busy room; a fixed window does not."""
    daemon = Daemon(profile)
    first = asyncio.Event()
    def handle(request):
        first.set()
        return httpx.Response(200, json={"participants": []})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            began = time.monotonic()
            while not first.is_set():
                daemon._request_snapshot(client)
                await asyncio.sleep(.005)
                assert time.monotonic() - began < SNAPSHOT_COALESCE + .5
            await settle(daemon)
    finally:
        daemon.inbox.close()


async def test_a_silent_roster_read_is_one_cancellable_task_despite_event_flood(profile):
    """A silent peer must cost neither a backlog of tasks nor a busy wait."""
    daemon = Daemon(profile)
    entered = asyncio.Event()
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            daemon._request_snapshot(client)
            await asyncio.wait_for(entered.wait(), 1)
            cpu, rss = time.process_time(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            task = daemon._refresh_tasks["participants"]
            for _ in range(10000):
                daemon._request_snapshot(client)
            await asyncio.sleep(.05)
            assert calls == 1 and daemon._refresh_tasks["participants"] is task
            assert time.process_time() - cpu < .5
            assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss < 16 * 1024
            daemon._stop.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
            daemon._request_snapshot(client)
            assert daemon._refresh_tasks["participants"] is task
    finally:
        daemon.inbox.close()


async def test_a_304_revalidates_the_saved_roster_and_an_old_hub_clears_the_validator(profile):
    """An unchanged response is fresh evidence; an older hub still returns full JSON."""
    daemon = Daemon(profile)
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(304)
        return httpx.Response(200, json={"participants": [], "seq": len(requests)},
                              headers={"ETag": 'W/"roster"'} if len(requests) == 1 else {})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await daemon._refresh_snapshot(client)
            daemon.snapshot["fetched_at"] = 1
            await daemon._refresh_snapshot(client)
            saved = json.loads((daemon.paths.root / "snapshot.json").read_text())
            assert saved["seq"] == 1 and saved["fetched_at"] > 1
            assert requests[1].headers["if-none-match"] == 'W/"roster"'
            await daemon._refresh_snapshot(client)
            await daemon._refresh_snapshot(client)
            assert "if-none-match" not in requests[3].headers
            assert daemon.snapshot["seq"] == 4
    finally:
        daemon.inbox.close()


@pytest.mark.parametrize("change", ["url", "token"])
async def test_validators_never_cross_authenticated_routes(profile, change):
    """A route/token switch cannot validate a different viewer's cached data."""
    daemon = Daemon(profile)
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"participants": []}, headers={"ETag": '"old"'})
        return httpx.Response(304)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await daemon._refresh_snapshot(client)
            stamp = daemon.snapshot["fetched_at"]
            setattr(profile, change, "http://new.invalid" if change == "url" else "new-token")
            await daemon._refresh_snapshot(client)
        assert "if-none-match" not in requests[1].headers
        assert daemon.snapshot["fetched_at"] == stamp
        assert daemon._snapshot_error == "hub answered 304"
    finally:
        daemon.inbox.close()
