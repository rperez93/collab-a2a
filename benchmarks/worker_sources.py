"""Measure repeated worker-source lifecycle using disposable state and children."""
import asyncio
import gc
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import tracemalloc

from collab import config, worker
from collab.config import SessionProfile
from collab.worker_telemetry import SourcePoller, configure_source


async def measure(root):
    profile = SessionProfile(session_id='benchmark', url='http://unused', name='synthetic',
                             host_name='synthetic', token='unused', home=str(root / 'home'))
    source = shlex.join([sys.executable, '-c',
        "print('{\"tokens_in\":12,\"tokens_out\":3,\"quotas\":{\"five_hour\":10}}')"])
    configure_source(profile.dir, command=source, quota_scope='independent')
    poller = SourcePoller(profile)
    tracemalloc.start()
    samples = []
    started, cpu = time.monotonic(), time.process_time()
    try:
        for index in range(100):
            poller.next_at = 0  # measure repeated production lifecycle, without interval sleeps
            await poller.tick()
            await poller.task
            if index % 10 == 9:
                gc.collect()
                samples.append({'calls': index + 1, 'retained_bytes': tracemalloc.get_traced_memory()[0],
                                'fds': len(list(Path('/proc/self/fd').iterdir()))})
        value = worker.metrics(profile.dir)
        assert value['quotas']['five_hour']['used_pct'] == 10
        assert value['tokens_in'] == 12  # snapshots must not accumulate on every poll
        return {'calls': 100, 'wall_seconds': time.monotonic() - started,
                'controller_cpu_seconds': time.process_time() - cpu,
                'tracemalloc_current_peak_bytes': tracemalloc.get_traced_memory(), 'samples': samples,
                'memory': {line.split(':')[0]: line.split(':', 1)[1].strip()
                           for line in Path('/proc/self/status').read_text().splitlines()
                           if line.startswith(('VmRSS:', 'VmHWM:'))}}
    finally:
        await poller.stop()
        tracemalloc.stop()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='collab-worker-source-benchmark-') as directory:
        root = Path(directory)
        os.environ.update(COLLAB_CONFIG=str(root / 'config.json'), COLLAB_HOME=str(root / 'home'),
                          COLLAB_STATE_DIR=str(root / 'state'))
        config._CACHE.clear()
        print(json.dumps(asyncio.run(measure(root)), indent=2))
