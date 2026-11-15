"""Receiver-side strong remote approval, with no terminal-receipt fallback."""
import hashlib
import json
import re
import time

from . import approval as strong
from ..consent_hello import unb64url
from ..remote_requests import FIELDS, validate, digest, binding_digest, REMOTE_AUTHORITY
AUTHORITY = REMOTE_AUTHORITY


def verify_review(answer, prepared, row, binding, profile):
    """Compare every locally known operation field before opening a window."""
    source_seq = row.get('source_seq')
    if type(source_seq) is not int or source_seq <= 0:
        raise strong.Mismatch('this offer has no authenticated source sequence')
    try:
        offer = validate({k: row[k] for k in FIELDS})
        raw = unb64url(answer.get('canonical_review'), limit=strong.MAX_REVIEW)
        parsed = json.loads(raw.decode('utf-8'))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise strong.Mismatch('the remote review is not readable') from None
    computed = hashlib.sha256(raw).hexdigest()
    expected = {'api_version': 1, 'session_id': profile.session_id,
                'requester_id': offer['sender_id'], 'target_id': profile.participant_id,
                'operation': 'remote-deliver', 'source_seq': source_seq,
                'offer': offer, 'offer_digest': digest(offer), 'binding': binding,
                'binding_digest': binding_digest(binding['target'], binding['command'], binding['cwd']),
                'authority': AUTHORITY}
    if (type(parsed) is not dict or set(parsed) != set(expected) | {'source_hash'}
            or any(parsed[k] != v for k, v in expected.items())
            or type(parsed['source_hash']) is not str or not re.fullmatch('[0-9a-f]{64}', parsed['source_hash'])
            or parsed != answer.get('review') or computed != answer.get('digest')
            or computed != prepared.get('digest') or prepared.get('source_seq') != source_seq
            or answer.get('request_id') != prepared.get('request_id')
            or answer.get('required_strength') != 'strong' or answer.get('state') != 'pending'):
        raise strong.Mismatch('the canonical review is not this remote offer and local destination')
    expires = prepared.get('expires_at')
    if type(expires) is not int or not time.time() < expires <= min(offer['expires_at'], time.time() + 630):
        raise strong.Mismatch('the remote review has an invalid expiry')
    return strong.Reviewed(strong._path(answer['request_id']), computed, raw, parsed, expires)


def accept(hub, profile, inbox, row, binding, ensure_current):
    api = strong.ApprovalClient(hub)
    source_seq = row.get('source_seq')
    if type(source_seq) is not int or source_seq <= 0:
        raise strong.Mismatch('receive this offer again from authenticated hub history before review')
    caps = api.capabilities()
    if caps.get('available') is not True or caps.get('remote_delivery') is not True:
        raise strong.Mismatch('this hub has not enabled strong remote delivery')
    key, why = strong.usable_key(api.keys())
    if key is None:
        raise strong.Mismatch(why)
    if key['method'] not in (caps.get('supported_methods') or []):
        raise strong.Mismatch('the enrolled approval method is unavailable')
    if key['method'] == 'hello':
        state, observed = strong.local_key_state(strong._hello(), key)
        if state in ('missing', 'changed'):
            api.report_key(key['credential_id'], observed)
            raise strong.Mismatch('the local approval key is missing or changed; the hub has been notified')
        if state != 'ok':
            raise strong.Mismatch('the local approval key could not be verified')
    prepared = api._call('POST', '/remote/prepare', {'source_seq': source_seq, 'binding': binding})
    reviewed = verify_review(api.review(prepared.get('request_id')), prepared, row, binding, profile)
    request = {'request_id': reviewed.request_id, 'requester_id': row['sender_id'],
        'requester_name': row['sender_id'], 'subject': 'Remote ' + row['kind'],
        'destination': binding['target'], 'authority': AUTHORITY,
        'digest': reviewed.digest, 'credential_id': key['credential_id'],
        'review_text': json.dumps(reviewed.review, indent=2, ensure_ascii=False, sort_keys=True),
        'canonical_review': strong._b64url(reviewed.canonical), 'expires_at': reviewed.expires_at}
    outcome = strong.open_window(request, lambda **kw: api.challenge(reviewed.request_id,
        key['credential_id'], origin=kw.get('origin')), method=key['method'])
    if outcome.get('choice') == 'decline':
        api.decline(reviewed.request_id)
        inbox.decline(row['id'])
        return False
    if outcome.get('choice') != 'approve':
        raise strong.Mismatch('the remote operation was not approved')
    if outcome.get('credential_id') != key['credential_id'] or outcome.get('method') != key['method']:
        raise strong.Mismatch('the approval window returned another credential or method')
    ensure_current()
    approved = api.approve(reviewed.request_id, challenge_id=outcome['challenge_id'],
                           credential_id=key['credential_id'], proof=outcome['proof'])
    receipt = approved.get('receipt')
    if type(receipt) is not str or not receipt or len(receipt) > 1024:
        raise strong.Uncertain('the approval returned no bounded receipt; no remote dispatch was authorized')
    ensure_current()
    args = dict(reviewed_digest=row['sha256'], session_id=profile.session_id,
                recipient_id=profile.participant_id, target=binding['target'],
                command=binding['command'], cwd=binding['cwd'])
    # Commit the attempt BEFORE consuming. Any exception, lost response or
    # process death from here leaves it non-dispatchable, never auto-retried.
    attempt = inbox.begin_strong_attempt(row['id'], **args)
    used = api._call('POST', '/remote/' + strong._path(reviewed.request_id) + '/consume', {'receipt': receipt})
    try:
        returned = unb64url(used.get('canonical_review'), limit=strong.MAX_REVIEW)
    except (ValueError, TypeError):
        raise strong.Uncertain('the consumption answer cannot authorize delivery') from None
    identity = used.get('consumption_id')
    if (used.get('state') != 'consumed' or used.get('execution') != 'not_performed'
            or used.get('request_id') != reviewed.request_id or used.get('digest') != reviewed.digest
            or returned != reviewed.canonical or type(identity) is not str
            or not re.fullmatch(r'rc_[A-Za-z0-9_-]{1,100}', identity)):
        raise strong.Uncertain('the consumption answer did not match this reviewed operation')
    ensure_current()
    inbox.finish_strong_attempt(row['id'], attempt, identity, **args)
    return True
