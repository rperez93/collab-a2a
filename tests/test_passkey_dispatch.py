"""Proof handoff contracts use software-shaped bytes, never a real human/key."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest

from collab.client import passkey as page, passkey_dispatch as seam



# A REAL BROWSER RETURNS AT ONCE. The dispatcher gives the opener 3 s
# (`_fetch(..., timeout=3.0)`), because webbrowser.open only hands the URL over.
# These tests used to run the whole scripted browser session — six HTTP
# requests — inside the opener, so under a loaded machine (load ~14 during the
# merge queue) it overran the 3 s and the dispatcher correctly answered
# «unavailable»: a test artefact that turned the queue red twice. The scripted
# browser now runs on its own thread, and anything it asserts is re-raised at
# teardown, so a failure inside it can never pass silently.
_BROWSER_THREADS = []
_BROWSER_ERRORS = []


def _in_a_browser(work):
    def opener(url):
        def run():
            try:
                work(url)
            except BaseException as exc:      # surfaced at teardown
                _BROWSER_ERRORS.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        _BROWSER_THREADS.append(thread)
        thread.start()
        return True
    return opener


def _browser_finished(timeout=10):
    """Wait for every scripted browser and re-raise what it asserted, BEFORE a
    test reads what the browser recorded: `approve_with_passkey` returns as soon
    as the page records the outcome, which can be before the browser thread has
    appended its last reply (gate review of #190)."""
    for thread in _BROWSER_THREADS:
        thread.join(timeout=timeout)
    assert not any(t.is_alive() for t in _BROWSER_THREADS), "a scripted browser never finished"
    if _BROWSER_ERRORS:
        raise _BROWSER_ERRORS[0]


@pytest.fixture(autouse=True)
def _browser_errors_fail_the_test():
    _BROWSER_THREADS.clear()
    _BROWSER_ERRORS.clear()
    yield
    for thread in _BROWSER_THREADS:
        thread.join(timeout=10)
    assert not any(t.is_alive() for t in _BROWSER_THREADS), "a scripted browser never finished"
    if _BROWSER_ERRORS:
        raise _BROWSER_ERRORS[0]

def encoded(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


@pytest.fixture
def prepared():
    review = {'api_version': 1, 'session_id': 's_owned', 'requester_id': 'p_peer',
        'target_id': 'p_local', 'subject': 'Synthetic request', 'destination': 'participant:p_local',
        'authority': 'skill-request:accept', 'payload': {'source_seq': 42, 'args': 'untrusted review text'}}
    raw = json.dumps(review, indent=1, ensure_ascii=True).encode() + b' \n'
    return {'request_id': 'ar_owned', 'requester_id': 'p_peer', 'requester_name': 'Self chosen',
        'session_id': 's_owned', 'target_id': 'p_local', 'subject': review['subject'],
        'destination': review['destination'], 'authority': review['authority'], 'source_seq': 42,
        'canonical_review': encoded(raw), 'digest': hashlib.sha256(raw).hexdigest(),
        'expires_at': int(time.time()) + 300, 'credential_id': encoded(b'explicit-selected-key')}


def challenge(request, origin):
    return {'api_version': 1, 'request_id': request['request_id'], 'challenge_id': 'ac_owned',
        'credential_id': request['credential_id'], 'digest': request['digest'],
        'method': 'passkey', 'origin': origin, 'rp_id': 'localhost',
        'challenge': encoded(b'h' * 32), 'expires_at': int(time.time()) + 120}


def assertion(request, origin, issued):
    raw = json.dumps({'type': 'webauthn.get', 'origin': origin,
        'challenge': issued['publicKey']['challenge'], 'crossOrigin': False}, indent=2).encode() + b' \n'
    return {'id': request['credential_id'], 'rawId': request['credential_id'], 'type': 'public-key',
        'response': {'clientDataJSON': encoded(raw), 'authenticatorData': encoded(b'a' * 37),
                     'signature': encoded(b'not-a-verified-signature')}}


@pytest.mark.parametrize('choice', ['approve', 'decline', 'cancel'])
def test_the_synchronous_seam_hosts_a_guarded_page_and_returns_the_choice_privately(prepared, monkeypatch, choice):
    """An explicit synthetic page action returns proof, without a hub approve request."""
    calls, browser, supplied = [], [], []
    def fetch_challenge(*, origin):
        calls.append(origin)
        return challenge(prepared, origin)
    def opener(url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        assert parsed.hostname == 'localhost' and not parsed.query
        headers = {'Origin': origin, 'X-Collab-Approval-Token': parsed.fragment}
        with httpx.Client(trust_env=False, timeout=3) as client:
            rejected = client.get(origin + parsed.path + '/review', headers={**headers, 'Origin': 'null'})
            assert rejected.status_code == 403
            response = client.get(origin + parsed.path + '/review', headers=headers)
            assert response.status_code == 200
            assert response.json()['canonical_review'].encode() == page._binary(prepared['canonical_review'], page.MAX_REVIEW)
            assert response.json()['local_cancel'] is True
            assert calls == []  # Loading review never fetches a challenge.
            if choice == 'approve':
                response = client.post(origin + parsed.path + '/approve/options', headers=headers, json={})
                assert response.status_code == 200, response.text
                issued = response.json()
                evidence = assertion(prepared, origin, issued)
                supplied.append(evidence)
                response = client.post(origin + parsed.path + '/approve/result', headers=headers,
                    json={'ceremony_id': issued['ceremony_id'], 'credential': evidence})
                assert response.status_code == 200, response.text
                assert response.json()['status'] == 'proof-ready'
                assert 'waiting for the hub' in response.json()['message']
                assert 'client_data_json' not in response.text and 'signature' not in response.text
            else:
                response = client.post(origin + parsed.path + '/' + choice, headers=headers, json={})
                assert response.status_code == 200, response.text
            browser.append(response.json())
        return True
    monkeypatch.setattr(page.webbrowser, 'open_new', _in_a_browser(opener))
    result = page.approve_with_passkey(prepared, fetch_challenge)
    _browser_finished()
    expected = 'approve' if choice == 'approve' else 'decline' if choice == 'decline' else 'unavailable'
    assert result['choice'] == expected
    assert len(calls) == (1 if choice == 'approve' else 0)
    assert browser and browser[0]['status'] != 'approved'
    if choice == 'approve':
        assert result == {'choice': 'approve', 'challenge_id': 'ac_owned',
            'credential_id': prepared['credential_id'], 'proof': {'type': 'webauthn.get',
                'client_data_json': supplied[0]['response']['clientDataJSON'],
                'authenticator_data': supplied[0]['response']['authenticatorData'],
                'signature': supplied[0]['response']['signature']}}


@pytest.mark.parametrize('fault', ['canonical-missing', 'digest', 'uppercase-digest', 'subject', 'requester', 'source', 'expiry', 'credential'])
def test_invalid_prepared_reviews_never_open_or_fetch_a_challenge(prepared, monkeypatch, fault):
    """No recanonicalization or label-based identity fallback may authorize review."""
    if fault == 'canonical-missing':prepared.pop('canonical_review')
    elif fault == 'digest':prepared['digest'] = '0' * 64
    elif fault == 'uppercase-digest':prepared['digest'] = prepared['digest'].upper()
    elif fault == 'subject':prepared['subject'] = 'replacement'
    elif fault == 'requester':prepared['requester_id'] = 'p_replacement'
    elif fault == 'source':prepared['source_seq'] = 43
    elif fault == 'expiry':prepared.pop('expires_at')
    else:prepared['credential_id'] = 'not base64!'
    called = []
    monkeypatch.setattr(page.webbrowser, 'open_new', lambda _: called.append('open'))
    assert page.approve_with_passkey(prepared, lambda **kwargs: called.append('challenge')) == {'choice': 'unavailable'}
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['request', 'digest', 'credential', 'origin', 'rp', 'challenge', 'expiry'])
async def test_a_callback_challenge_must_match_the_exact_prepared_context(prepared, fault):
    """A local callback result still cannot substitute an origin or another operation."""
    origin = 'http://localhost:12345'
    value = challenge(prepared, origin)
    field = {'request': 'request_id', 'digest': 'digest', 'credential': 'credential_id',
             'origin': 'origin', 'rp': 'rp_id', 'challenge': 'challenge', 'expiry': 'expires_at'}[fault]
    value[field] = int(time.time()) + 151 if fault == 'expiry' else 'different'
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), lambda **kwargs: value)
    with pytest.raises((ValueError, page.NotReady)):
        await adapter.begin(prepared['request_id'], 'approve', origin, bytes.fromhex(prepared['digest']))
    assert adapter.result == {'choice': 'unavailable'} and adapter.finished.is_set()


@pytest.mark.asyncio
async def test_near_expiry_and_skew_bound_only_the_local_ceremony(prepared):
    """Keep the hub challenge unchanged while limiting authenticator time to120seconds."""
    prepared['expires_at'] = time.time() + 30
    origin = 'http://localhost:12345'
    value = challenge(prepared, origin)
    value['expires_at'] = int(time.time()) + 150
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), lambda **kwargs: value)
    ceremony = await adapter.begin(prepared['request_id'], 'approve', origin, bytes.fromhex(prepared['digest']))
    assert ceremony.public_key['challenge'] == value['challenge']
    assert ceremony.expires == prepared['expires_at'] and 0 < ceremony.public_key['timeout'] <= 30000
    assert ceremony.public_key['timeout'] <= 120000


def test_expiry_and_a_failed_opener_return_without_a_challenge(prepared, monkeypatch):
    calls = []
    monkeypatch.setattr(page.webbrowser, 'open_new', lambda _: False)
    assert page.approve_with_passkey(prepared, lambda **kwargs: calls.append(kwargs)) == {'choice': 'unavailable'}
    prepared['expires_at'] = time.time() - 1
    assert page.approve_with_passkey(prepared, lambda **kwargs: calls.append(kwargs)) == {'choice': 'expired'}
    assert calls == []


def test_an_unanswered_review_has_a_bounded_wait_and_closes_its_page(prepared, monkeypatch):
    monkeypatch.setattr(seam, 'MAX_WAIT_SECONDS', .05)
    monkeypatch.setattr(page.webbrowser, 'open_new', lambda _: True)
    began = time.monotonic()
    assert page.approve_with_passkey(prepared, lambda **kwargs: pytest.fail('no click')) == {'choice': 'expired'}
    assert time.monotonic() - began < 2


@pytest.mark.asyncio
async def test_a_timed_out_callback_keeps_its_daemon_slot_until_it_really_exits(prepared, monkeypatch):
    """Timeout must not permit repeated unkillable callbacks to accumulate threads."""
    monkeypatch.setattr(seam, 'CALLBACK_SECONDS', .02)
    monkeypatch.setattr(seam, '_CALLBACK_SLOTS', threading.BoundedSemaphore(1))
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def callback(**kwargs):
        try:
            entered.set()
            release.wait(2)
            return challenge(prepared, kwargs['origin'])
        finally:exited.set()
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), callback)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await adapter.begin(prepared['request_id'], 'approve', 'http://localhost:12345', bytes.fromhex(prepared['digest']))
        assert entered.is_set() and adapter.result == {'choice': 'unavailable'}
        with pytest.raises(page.NotReady):
            await seam._fetch(lambda **kwargs: pytest.fail('busy callback ran'), 'http://localhost:12345')
        adapter.invalidate()
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while not exited.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.001)
    await asyncio.sleep(.01)
    assert exited.is_set() and adapter.result == {'choice': 'unavailable'} and adapter.ceremony is None


def test_concurrent_synchronous_calls_cannot_open_unbounded_human_windows(prepared, monkeypatch):
    """A quick opener must not release the separate lifetime admission slot."""
    entered, release = threading.Event(), threading.Event()
    results = []
    def opener(_):
        entered.set()
        release.wait(2)
        return False
    monkeypatch.setattr(page.webbrowser, 'open_new', opener)
    thread = threading.Thread(target=lambda: results.append(page.approve_with_passkey(prepared, lambda **kwargs: None)))
    thread.start()
    try:
        assert entered.wait(1)
        assert page.approve_with_passkey(prepared, lambda **kwargs: pytest.fail('overlapping callback')) == {'choice': 'unavailable'}
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results == [{'choice': 'unavailable'}]


def test_cancel_during_a_held_challenge_refuses_the_late_result(prepared, monkeypatch):
    """The cancel endpoint must remain available while options owns the window."""
    callback_entered, callback_release = threading.Event(), threading.Event()
    options_finished = threading.Event()
    options_status = []
    def fetch_challenge(*, origin):
        callback_entered.set()
        callback_release.wait(4)
        return challenge(prepared, origin)
    def opener(url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        headers = {'Origin': origin, 'X-Collab-Approval-Token': parsed.fragment}
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(origin + parsed.path + '/review', headers=headers).status_code == 200
            def options():
                try:
                    response = client.post(origin + parsed.path + '/approve/options', headers=headers, json={})
                    options_status.append(response.status_code)
                finally:options_finished.set()
            thread = threading.Thread(target=options)
            thread.start()
            try:
                assert callback_entered.wait(1)
                response = client.post(origin + parsed.path + '/cancel', headers=headers, json={})
                assert response.status_code == 200 and response.json()['status'] == 'cancelled'
                callback_release.set()
                assert options_finished.wait(1)
                assert options_status != [200]
            finally:
                callback_release.set()
                thread.join(2)
        return True
    monkeypatch.setattr(page.webbrowser, 'open_new', _in_a_browser(opener))
    try:
        assert page.approve_with_passkey(prepared, fetch_challenge) == {'choice': 'unavailable'}
    finally:callback_release.set()


def test_synchronous_dispatch_does_not_require_python311_timeout_contexts(prepared, monkeypatch):
    """The package supports3.10; do not accidentally depend on asyncio.timeout."""
    monkeypatch.delattr(asyncio, 'timeout', raising=False)
    monkeypatch.setattr(seam, 'MAX_WAIT_SECONDS', .04)
    monkeypatch.setattr(page.webbrowser, 'open_new', lambda _: True)
    assert page.approve_with_passkey(prepared, lambda **kwargs: pytest.fail('no click')) == {'choice': 'expired'}


@pytest.mark.asyncio
async def test_a_cancelled_collector_cannot_be_turned_into_a_second_proof_choice(prepared):
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), lambda **kwargs: challenge(prepared, kwargs['origin']))
    origin = 'http://localhost:12345'
    digest = bytes.fromhex(prepared['digest'])
    issued = await adapter.begin(prepared['request_id'], 'approve', origin, digest)
    await adapter.cancel(prepared['request_id'], origin, digest)
    evidence = assertion(prepared, origin, {'publicKey': issued.public_key})
    with pytest.raises(ValueError):
        await adapter.complete(prepared['request_id'], 'approve', issued.ceremony_id, evidence, origin, digest)
    assert adapter.result == {'choice': 'unavailable'}


@pytest.mark.parametrize('fault', ['signed-origin', 'credential-shape', 'signature-limit'])
def test_a_rejected_current_browser_proof_finishes_the_synchronous_collector(prepared, monkeypatch, fault):
    """A bound unusable assertion must not strand the CLI for its600-second review."""
    # 5 s ceiling, not 0.75: three browser requests must fit inside it on a
    # loaded machine. The refusal must still END the wait well before it —
    # asserted below — which is the point of the test (gate review of #190).
    monkeypatch.setattr(seam, 'MAX_WAIT_SECONDS', 5.0)
    calls, responses = [], []
    def fetch_challenge(*, origin):
        calls.append(origin)
        return challenge(prepared, origin)
    def opener(url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        headers = {'Origin': origin, 'X-Collab-Approval-Token': parsed.fragment}
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(origin + parsed.path + '/review', headers=headers).status_code == 200
            response = client.post(origin + parsed.path + '/approve/options', headers=headers, json={})
            assert response.status_code == 200, response.text
            issued = response.json()
            evidence = assertion(prepared, origin, issued)
            if fault == 'signed-origin':
                data = {'type': 'webauthn.get', 'origin': 'http://localhost:1',
                        'challenge': issued['publicKey']['challenge']}
                evidence['response']['clientDataJSON'] = encoded(json.dumps(data).encode())
            elif fault == 'credential-shape':
                evidence['response'].pop('signature')
            else:
                evidence['response']['signature'] = encoded(b'x' * 4097)
            response = client.post(origin + parsed.path + '/approve/result', headers=headers,
                json={'ceremony_id': issued['ceremony_id'], 'credential': evidence})
            # Record rather than assert here: on the old source a502/400 must
            # leave the opener successful so the actual collector wait is tested.
            responses.append((response.status_code, response.json()))
        return True
    monkeypatch.setattr(page.webbrowser, 'open_new', _in_a_browser(opener))
    started = time.monotonic()
    result = page.approve_with_passkey(prepared, fetch_challenge)
    elapsed = time.monotonic() - started      # the collector's own time, before the join
    _browser_finished()
    assert result == {'choice': 'unavailable', 'reason': 'browser_assertion_rejected'}
    assert responses == [(200, {'status': 'refused',
        'message': 'The browser assertion was rejected locally. No proof was handed to the dispatcher.'})]
    assert len(calls) == 1 and elapsed < 4.5, "the refusal must end the wait, not the 5 s ceiling"
    assert 'proof' not in result


@pytest.mark.asyncio
async def test_rejected_assertions_have_no_late_retry_or_replacement_choice(prepared):
    """Once refused, complete/begin/cancel and the page hook cannot overwrite it."""
    origin = 'http://localhost:12345'
    digest = bytes.fromhex(prepared['digest'])
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), lambda **kwargs: challenge(prepared, kwargs['origin']))
    issued = await adapter.begin(prepared['request_id'], 'approve', origin, digest)
    valid = assertion(prepared, origin, {'publicKey': issued.public_key})
    invalid = deepcopy(valid)
    invalid['response']['clientDataJSON'] = encoded(b'{"type":"webauthn.get","origin":"http://localhost:1"}')
    outcome = await adapter.complete(prepared['request_id'], 'approve', issued.ceremony_id, invalid, origin, digest)
    assert outcome.status == 'refused' and adapter.finished.is_set() and adapter.ceremony is None
    expected = {'choice': 'unavailable', 'reason': 'browser_assertion_rejected'}
    for operation in (
            lambda: adapter.complete(prepared['request_id'], 'approve', issued.ceremony_id, valid, origin, digest),
            lambda: adapter.begin(prepared['request_id'], 'approve', origin, digest),
            lambda: adapter.cancel(prepared['request_id'], origin, digest),
            lambda: adapter.reject_proof(prepared['request_id'], 'approve', issued.ceremony_id, origin, digest)):
        with pytest.raises(ValueError):await operation()
        assert adapter.result == expected


def test_unrelated_http_refusals_do_not_abort_a_valid_proof_collection(prepared, monkeypatch):
    """No capability/Origin or unbound malformed request may cancel another action."""
    evidence_sent = []
    def opener(url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        headers = {'Origin': origin, 'X-Collab-Approval-Token': parsed.fragment}
        target = origin + parsed.path
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(target + '/review', headers=headers).status_code == 200
            issued = client.post(target + '/approve/options', headers=headers, json={}).json()
            valid = assertion(prepared, origin, issued)
            body = {'ceremony_id': issued['ceremony_id'], 'credential': valid}
            assert client.post(target + '/approve/result', headers={**headers, 'Origin': 'null'}, json=body).status_code == 403
            assert client.post(target + '/approve/result', headers={**headers, 'X-Collab-Approval-Token': 'x' * 43}, json=body).status_code == 403
            assert client.post(target + '/approve/result', headers=headers,
                json={'ceremony_id': 'other', 'credential': {}}).status_code == 409
            assert client.post(target + '/approve/result', headers={**headers, 'Content-Type': 'application/json'}, content=b'{broken').status_code == 400
            response = client.post(target + '/approve/result', headers=headers, json=body)
            assert response.status_code == 200 and response.json()['status'] == 'proof-ready'
            evidence_sent.append(valid)
        return True
    monkeypatch.setattr(page.webbrowser, 'open_new', _in_a_browser(opener))
    result = page.approve_with_passkey(prepared, lambda **kwargs: challenge(prepared, kwargs['origin']))
    _browser_finished()
    assert result['choice'] == 'approve' and len(evidence_sent) == 1
    assert result['proof']['client_data_json'] == evidence_sent[0]['response']['clientDataJSON']


def test_an_expired_exact_browser_ceremony_finishes_without_waiting_for_review_expiry(prepared, monkeypatch):
    """The shorter ceremony deadline cannot leave a current review waiting600seconds."""
    # 5 s, not 0.75: the browser's four requests must fit inside this window,
    # and on a loaded machine 0.75 s did not. What the test proves is that the
    # expired ceremony ENDS the wait — asserted below as finishing well before
    # this ceiling — not how fast the machine is (gate review of #190).
    monkeypatch.setattr(seam, 'MAX_WAIT_SECONDS', 5.0)
    clock = [time.time()]
    monkeypatch.setattr(seam.time, 'time', lambda: clock[0])
    adapters, responses = [], []
    original = seam._ProofAdapter
    def adapter_factory(*args):
        instance = original(*args)
        adapters.append(instance)
        return instance
    monkeypatch.setattr(seam, '_ProofAdapter', adapter_factory)
    def opener(url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        headers = {'Origin': origin, 'X-Collab-Approval-Token': parsed.fragment}
        target = origin + parsed.path
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(target + '/review', headers=headers).status_code == 200
            issued = client.post(target + '/approve/options', headers=headers, json={}).json()
            clock[0] = issued['expires'] + 1
            # A wrong expired ID is still unrelated and must not consume the
            # actual collector or stop the current review.
            response = client.post(target + '/approve/result', headers=headers,
                json={'ceremony_id': 'unrelated-expired-id', 'credential': {}})
            assert response.status_code == 409 and not adapters[0].finished.is_set()
            response = client.post(target + '/approve/result', headers=headers,
                json={'ceremony_id': issued['ceremony_id'], 'credential': {}})
            responses.append((response.status_code, response.json()))
        return True
    monkeypatch.setattr(page.webbrowser, 'open_new', _in_a_browser(opener))
    started = time.monotonic()
    result = page.approve_with_passkey(prepared, lambda **kwargs: challenge(prepared, kwargs['origin']))
    elapsed = time.monotonic() - started
    _browser_finished()
    assert result == {'choice': 'expired'}
    assert elapsed < 4.5, "the expired ceremony must end the wait, not the 5 s ceiling"
    assert adapters[0].finished.is_set() and adapters[0].ceremony is None
    assert responses == [(200, {'status': 'refused',
        'message': 'The passkey ceremony expired locally. No proof was handed to the dispatcher.'})]


@pytest.mark.asyncio
async def test_direct_expired_proof_refusal_is_owned_one_shot_and_never_revives_authority(prepared, monkeypatch):
    origin = 'http://localhost:12345'
    digest = bytes.fromhex(prepared['digest'])
    clock = [time.time()]
    monkeypatch.setattr(seam.time, 'time', lambda: clock[0])
    adapter = seam._ProofAdapter(seam._Prepared.parse(prepared), lambda **kwargs: challenge(prepared, kwargs['origin']))
    issued = await adapter.begin(prepared['request_id'], 'approve', origin, digest)
    clock[0] = issued.expires + 1
    with pytest.raises(ValueError):
        await adapter.reject_proof(prepared['request_id'], 'approve', 'unrelated', origin, digest)
    assert not adapter.finished.is_set() and adapter.result is None
    evidence = assertion(prepared, origin, {'publicKey': issued.public_key})
    outcome = await adapter.complete(prepared['request_id'], 'approve', issued.ceremony_id, evidence, origin, digest)
    assert outcome.status == 'refused' and adapter.result == {'choice': 'expired'}
    assert adapter.finished.is_set() and adapter.ceremony is None
    with pytest.raises(ValueError):
        await adapter.cancel(prepared['request_id'], origin, digest)
    assert adapter.result == {'choice': 'expired'}
