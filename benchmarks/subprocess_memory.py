"""Reproduce the viewer's small retained allocations without importing Collab.

Run with the same Python as runtime.py. Four batches of 500 harmless local
`true` calls distinguish interpreter retention from live pipe/child leakage.
"""
import gc
import io
import json
import platform
import subprocess
import tracemalloc

from runtime import fd_count

tracemalloc.start()
baseline = tracemalloc.take_snapshot()
samples = []
for batch in range(4):
    for _ in range(500):
        subprocess.run(['true'], capture_output=True, text=True, check=True)
    gc.collect()
    snapshot = tracemalloc.take_snapshot()
    growth = [row for row in snapshot.compare_to(baseline, 'lineno')
              if 'subprocess.py' in str(row.traceback)]
    samples.append({'calls': (batch + 1) * 500,
                    'retained_subprocess_bytes': sum(row.size_diff for row in growth),
                    'live_text_wrappers': sum(isinstance(value, io.TextIOWrapper) for value in gc.get_objects()),
                    'open_fds': fd_count()})
print(json.dumps({'python': platform.python_version(), 'samples': samples,
                  'limits': 'No Collab imports. Retained allocations do not prove a leak; this short run does not establish a long-term bound.'}, indent=2))
