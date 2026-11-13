"""Measure a selected participant's live roster reads without another daemon.

Run this same script with an absolute PYTHONPATH pointing first to the before
revision and then the after revision. --workspace selects YOUR active agent's
workspace; COLLAB_HOME can select its exact home. No join or mutation endpoint
is called, and all temporary inbox/snapshot files are removed on exit.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import resource
import statistics
import tempfile
import time

import httpx

from collab.client.daemon import Daemon
from collab.compatibility import check_host, request_headers
from collab.config import SessionProfile
from collab.protocol import EXT_PREFIX


async def measure(args):
    profile = SessionProfile.current(args.workspace)
    if profile is None:
        raise ValueError("select your joined participant with --workspace or COLLAB_HOME")
    target = (args.url or profile.url).rstrip("/")
    options = {}
    if target == profile.url:
        try:
            from collab.routes import options as route_options
            options = route_options(profile)
        except ImportError:
            pass  # The baseline revision predates local TLS routes.
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, **options) as client:
        await check_host(client, target)
        health = await client.get(target + EXT_PREFIX + "/health")
        health.raise_for_status()
        if health.json().get("session_id") != profile.session_id:
            raise ValueError("target does not serve your selected session; no bearer was submitted")
        latencies = []
        for _ in range(5):
            began = time.monotonic()
            response = await client.get(target + EXT_PREFIX + "/participants",
                                        headers=request_headers(profile.token))
            response.raise_for_status()
            latencies.append(time.monotonic() - began)
        with tempfile.TemporaryDirectory(prefix="collab-roster-burst-") as temporary:
            copied = replace(profile, home=temporary, url=target)
            copied.dir.mkdir(parents=True)
            daemon = Daemon(copied)
            requests = body_bytes = 0
            class Meter:
                async def get(self, *positional, **keywords):
                    nonlocal requests, body_bytes
                    response = await client.get(*positional, **keywords)
                    requests += 1
                    body_bytes += response.num_bytes_downloaded
                    return response
            cpu, began = time.process_time(), time.monotonic()
            task = None
            try:
                for _ in range(args.events):
                    daemon._request_snapshot(Meter())
                    await asyncio.sleep(args.spacing)
                task = daemon._refresh_tasks["participants"]
                await asyncio.wait_for(task, 15)
                if daemon.snapshot.get("session_id") != profile.session_id:
                    raise ValueError("no valid selected-session roster was received")
                elapsed, used_cpu = time.monotonic() - began, time.process_time() - cpu
                if Path("/proc/self/status").exists():
                    peak = int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                                    if line.startswith("VmHWM:")))
                else:
                    import sys
                    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    if sys.platform == "darwin":
                        peak //= 1024
                return {"route": args.label, "invalidations": args.events,
                        "spacing_s": args.spacing, "requests": requests,
                        "response_body_bytes": body_bytes,
                        "elapsed_s": round(elapsed, 6), "cpu_s": round(used_cpu, 6),
                        "median_rtt_ms": round(statistics.median(latencies) * 1000, 3),
                        "process_peak_rss_kib": peak}
            finally:
                daemon._stop.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                daemon.inbox.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--url", help="explicit route to your selected session")
    parser.add_argument("--label", default="selected", help="nonsecret label in output")
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--spacing", type=float, default=.001)
    args = parser.parse_args()
    if not 1 <= args.events <= 10000 or not 0 <= args.spacing <= 1:
        parser.error("events must be 1..10000 and spacing must be 0..1 seconds")
    try:
        result = asyncio.run(measure(args))
    except Exception as exc:
        # HTTP errors can contain target URLs. The exception type is enough
        # for this measurement artifact; never serialize request objects or
        # profile/credential data into a benchmark failure report.
        parser.exit(1, f"roster benchmark failed ({type(exc).__name__})\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
