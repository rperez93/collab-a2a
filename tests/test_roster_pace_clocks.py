"""A conditional roster must refresh its small pace without resending the board."""
import copy
import json

import httpx
import pytest
from starlette.requests import Request

from collab import batch, roster_clocks as clocks
from collab.client.daemon import Daemon
from collab.server.roster_transport import roster_response


def observation(now, *, completed=(1600, 2200), outstanding=2, closed=None):
    return {"server_time": now, "seq": 1, "batch": {
        "counted_at": now, "total": 4, "done": 4 - outstanding,
        "pace": batch.pace(opened_at=1000, completed=completed,
                           outstanding=outstanding, closed_at=closed, now=now)},
        "participants": [{"name": "alice", "focus": "x" * 10000}]}


def response(payload, tag=None, *, offer=True):
    headers = {clocks.OFFER.lower(): "1"} if offer else {}
    if tag:
        headers["if-none-match"] = tag
    req = Request({"type": "http", "method": "GET", "path": "/participants",
                   "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
    return roster_response(payload, req, conditional=True)


@pytest.mark.parametrize("start,later", [(2300, 2400), (3000, 3100)])
def test_running_and_overdue_reads_send_authoritative_pace_in_a_304(start, later):
    """Overdue per_task also ticks: ignoring only elapsed/eta/since_last still churns."""
    first, payload = response(observation(start)), observation(later)
    second = response(payload, first.headers["etag"])
    assert second.status_code == 304 and second.body == b""
    observed = clocks.read_clocks(second.headers)
    assert observed == {k: payload["batch"][k] for k in ("pace", "counted_at")}
    assert sum(len(second.headers[k]) for k in (clocks.PACE, clocks.COUNTED_AT)) < 512
    assert json.loads(first.body) == observation(start)


def test_overdue_and_real_completion_transitions_invalidate_the_semantic_tag():
    """A due threshold and new progress remain real state despite clock filtering."""
    first = response(observation(2700))
    overdue = response(observation(2900), first.headers["etag"])
    assert overdue.status_code == 200
    changed = response(observation(3100, completed=(1600, 2200, 3000), outstanding=1),
                       overdue.headers["etag"])
    assert changed.status_code == 200
    for state in (observation(3300, completed=(1600, 2200, 3000, 3200), outstanding=0),
                  observation(3300, closed=3200)):
        assert response(state, changed.headers["etag"]).status_code == 200


@pytest.mark.parametrize("field", ["elapsed", "per_task", "state", "counted", "overdue"])
def test_terminal_timings_and_pace_state_are_never_filtered(field):
    """Terminal elapsed/rate are frozen results; arbitrary edits must invalidate."""
    payload = observation(4000, outstanding=0)
    tag = response(payload).headers["etag"]
    value = payload["batch"]["pace"][field]
    payload["batch"]["pace"][field] = (not value if type(value) is bool else
                                       "closed" if isinstance(value, str) else (value or 0) + 1)
    assert response(payload, tag).status_code == 200


def test_legacy_validators_keep_full_pace_and_never_cross_opt_in_modes():
    """Old clients cannot merge new headers, so their changing pace keeps its 200."""
    tag = response(observation(2300), offer=False).headers["etag"]
    assert response(observation(2400), tag, offer=False).status_code == 200
    assert response(observation(2300), tag).status_code == 200
    assert clocks.OFFER.lower() in response(observation(2300)).headers["vary"].lower()


@pytest.mark.parametrize("mutation", ["future", "nonfinite", "oversize", "missing"])
def test_unsupported_clock_schemas_fall_back_to_the_full_body(mutation):
    """An extension must not hide future fields or accept malformed peer clocks."""
    payload = observation(2300)
    pace = payload["batch"]["pace"]
    if mutation == "future":
        pace["future_field"] = "new semantic state"
    elif mutation == "nonfinite":
        pace["eta"] = 1e101
    elif mutation == "oversize":
        pace["state"] = "x" * 3000
    else:
        del pace["counted"]
    first = response(payload)
    assert response(payload, first.headers["etag"]).status_code == 200


@pytest.mark.parametrize("malformed", [None, "missing", "invalid", "oversize", "future"])
async def test_daemon_renews_pace_and_freshness_atomically_or_forces_full_recovery(profile, malformed):
    """Resetting fetched_at without new clocks froze elapsed/ETA on every 304."""
    daemon = Daemon(profile)
    requests = []
    initial, later = observation(2300), observation(2400)
    def handle(request):
        requests.append(request)
        assert request.headers[clocks.OFFER] == "1"
        if len(requests) != 2:
            return httpx.Response(200, json=initial, headers={"ETag": 'W/"state"'})
        headers = clocks.clock_headers(later["batch"])
        if malformed == "missing":
            headers.pop(clocks.PACE)
        elif malformed == "invalid":
            headers[clocks.PACE] = "{"
        elif malformed == "oversize":
            headers[clocks.PACE] = " " * 3000
        elif malformed == "future":
            value = copy.deepcopy(later["batch"])
            value["pace"]["extra"] = 1
            headers[clocks.PACE] = json.dumps(value["pace"])
        return httpx.Response(304, headers=headers)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await daemon._refresh_snapshot(client)
            daemon.snapshot["fetched_at"] = 1
            await daemon._refresh_snapshot(client)
            if malformed is None:
                assert daemon.snapshot["fetched_at"] > 1
                assert daemon.snapshot["batch"]["pace"] == later["batch"]["pace"]
                assert daemon._snapshot_etag == 'W/"state"'
            else:
                assert daemon.snapshot["fetched_at"] == 1
                assert daemon.snapshot["batch"]["pace"] == initial["batch"]["pace"]
                assert daemon._snapshot_etag is None
                assert daemon._snapshot_error
                await daemon._refresh_snapshot(client)
                assert "if-none-match" not in requests[2].headers
    finally:
        daemon.inbox.close()


@pytest.mark.parametrize("board", ["synthetic", "near_2mib"])
@pytest.mark.parametrize("state", ["unchanged", "changing"])
def test_advancing_overdue_clocks_save_bytes_with_bounded_cpu_and_memory(board, state):
    """Fresh processes catch clock-path work or buffering hidden by a warm parent."""
    from pathlib import Path
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "benchmarks" / "roster_clocks.py"
    rows = []
    for variant in ("legacy_pace", "clock_headers"):
        done = subprocess.run([sys.executable, str(script), board, state, variant],
                              capture_output=True, text=True, check=True, timeout=15)
        rows.append(json.loads(done.stdout))
    legacy, observed = rows
    assert legacy["responses_200"] == legacy["requests"]
    if state == "unchanged":
        assert observed["responses_200"] == 1
        assert observed["responses_304"] == observed["requests"] - 1
        assert observed["body_bytes"] + observed["clock_header_bytes"] < legacy["body_bytes"] / 3
        assert observed["clock_header_bytes"] < observed["requests"] * 512
    else:
        assert observed["responses_200"] == observed["requests"]
        assert observed["clock_header_bytes"] == 0
    for row in rows:
        assert row["cpu_seconds"] < 5
        assert row["peak_rss_kib"] < 96 * 1024
