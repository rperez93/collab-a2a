"""Synthetic overdue-pace transport comparison; no sessions or provider calls.

Run with an absolute branch PYTHONPATH and three arguments:
    synthetic|near_2mib unchanged|changing legacy_pace|clock_headers
The two variants use identical gzip/serialization; only clock negotiation differs.
"""
import json
from pathlib import Path
import resource
import sys
import time
from starlette.requests import Request
from starlette.responses import JSONResponse
from collab.server.roster_transport import roster_response

board, changing, variant = sys.argv[1:]
changing = changing == "changing"
count = 100 if board == "synthetic" else 20
seed = "Measure requests, bytes, CPU and RSS before shipping."
detail = seed if board == "synthetic" else (seed * 160)[:8000]
task_count = 80 if board == "synthetic" else 250
payload = {
    "server_time": 123.0, "seq": 1, "you": "alice", "you_id": "p-alice",
    "batch": {"id": "B_measure", "state": "closed", "total": task_count,
              "done": 40, "holding": [], "counted_at": 123.0},
    "participants": [
        {"name": f"agent-{i}", "connected": True,
         "focus": "measuring the shared task board",
         "stats": {"tokens_in": 12000, "tokens_out": 500}}
        for i in range(40)],
    "tasks": [
        {"id": f"T_{i}", "title": "Verify communication efficiency",
         "detail": detail, "state": "working", "owner": f"agent-{i % 40}"}
        for i in range(task_count)],
}
from collab.batch import pace
payload_bytes = len(JSONResponse(payload).body)
clock_header_bytes = 0
body_bytes = responses_200 = responses_304 = 0
tag = ""
started = time.process_time()
for index in range(count):
    payload["server_time"] += 1
    payload["batch"]["counted_at"] += 1
    payload["batch"]["pace"] = pace(opened_at=1, completed=[10, 20], outstanding=2, now=123+index)
    if changing:
        payload["seq"] += 1
        payload["tasks"][index % task_count]["detail"] = detail[:7997] + str(index)
    if variant == "baseline":
        response = JSONResponse(payload)
    else:
        headers = [(b"accept-encoding", b"gzip")]
        if variant == "clock_headers":
            headers.append((b"collab-roster-clocks", b"1"))
        if tag:
            headers.append((b"if-none-match", tag.encode()))
        request = Request({"type": "http", "method": "GET", "path": "/participants",
                           "headers": headers})
        response = roster_response(payload, request, conditional=True)
        tag = response.headers["etag"]
    clock_header_bytes += sum(len(k)+len(v)+4 for k,v in response.raw_headers if k.startswith(b"collab-batch-"))
    body_bytes += len(response.body)
    responses_200 += response.status_code == 200
    responses_304 += response.status_code == 304
cpu = time.process_time() - started
if sys.platform.startswith("linux"):
    # getrusage can include the launcher's fork-time high water before exec;
    # VmHWM belongs to this interpreter's current address space instead.
    rss = int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                   if line.startswith("VmHWM:")))
else:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform == "darwin":
    rss //= 1024
print(json.dumps({"board": board, "state": "changing" if changing else "unchanged",
                  "variant": variant, "requests": count, "payload_bytes": payload_bytes,
                  "body_bytes": body_bytes, "clock_header_bytes": clock_header_bytes, "responses_200": responses_200,
                  "responses_304": responses_304, "cpu_seconds": cpu,
                  "peak_rss_kib": rss}))
