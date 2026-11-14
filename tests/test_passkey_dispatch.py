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
    monkeypatch.setattr(page.webbrowser, 'open_new', opener)
    result = page.approve_with_passkey(prepared, fetch_challenge)
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
    monkeypatch.setattr(page.webbrowser, 'open_new', opener)
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
