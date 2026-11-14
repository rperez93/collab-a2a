"""Local review surface for remote offers; receiving text grants no authority."""
from __future__ import annotations

import json

from . import consent, wake
from .remote_requests import Inbox, make_offer, wire_text, digest, binding_digest, supports_remote_consent
from .remote_delivery import working_directory


def run(args, profile):
    inbox = Inbox(profile.dir)
    action = args.action
    cwd = working_directory(profile)
    if action == 'goal':
        from .cli import _client
        from .protocol import EXT_PREFIX
        with _client(profile) as client:
            result = client._request('PUT', f'{EXT_PREFIX}/remote-goal',
                                     json={'text': args.text, 'clear': args.clear})
        print(json.dumps(result, ensure_ascii=True))
        return 0
    if action == 'offer':
        from .cli import _client
        from .worker import Store
        if not args.to or not args.target or not args.text:
            raise ValueError('offer requires --to, --target and --text')
        store = Store(profile.dir)
        if not store.status()['enabled']:
            raise ValueError('start the conversation worker before offering a remote request')
        with _client(profile) as client:
            snapshot = client.snapshot()
        matches = [p for p in snapshot.get('participants', [])
                   if p.get('name') == args.to or p.get('id') == args.to]
        if len(matches) != 1 or not matches[0].get('id'):
            raise ValueError('recipient must identify one authenticated participant')
        person = matches[0]
        if not supports_remote_consent(person):
            raise ValueError('recipient has not advertised supported remote consent; update and reconnect it first')
        offer = make_offer(session_id=profile.session_id, sender_id=profile.participant_id,
                           recipient_id=person['id'], target=args.target,
                           kind=args.kind, text=args.text)
        queued = store.send(wire_text(offer), to=person['name'], recipient_id=person['id'])
        print(json.dumps({'request_id': offer['id'], 'sha256': digest(offer),
                          'recipient_id': person['id'], 'outbox_id': queued,
                          'state': 'offered; human acceptance required'}, ensure_ascii=True))
        return 0
    if action == 'pending':
        print(json.dumps(inbox.list(), ensure_ascii=True, indent=2))
        return 0
    command = list(wake.read_config(profile.dir).command)
    if action == 'bind':
        if not args.target:
            raise ValueError('--target must name the local coding session')
        inbox.bind(args.target, command, cwd)
        print(json.dumps({'target': args.target, 'cwd': str(cwd), 'bound': True,
                          'authorizes_execution': False}, ensure_ascii=True))
        return 0
    if not args.request_id:
        raise ValueError('a request id is required')
    rows = [row for row in inbox.list() if row['id'] == args.request_id]
    if not rows:
        raise ValueError('remote request not found')
    row = rows[0]
    # Escape all controls, including ANSI escapes in peer-provided text.
    print(json.dumps(row, ensure_ascii=True, indent=2))
    if action == 'show':
        return 0
    if action == 'decline':
        inbox.decline(args.request_id)
        print('Declined locally.')
        return 0
    target = inbox.bound_target(command, cwd)
    receipt = consent.request(
        profile.dir, subject='remote', request_id=args.request_id,
        digest=row['sha256'], binding=binding_digest(target, command, cwd),
        review={'offer': row, 'local_target': target, 'local_command': command,
                'local_cwd': str(cwd),
                'authority': 'Runs one turn with the configured local adapter permissions.'})
    current_command = list(wake.read_config(profile.dir).command)
    current_cwd = working_directory(profile)
    current_target = inbox.bound_target(current_command, current_cwd)
    if (current_command != command or current_cwd != cwd or current_target != target):
        raise ValueError('local delivery configuration changed during review; review again')
    inbox.accept(args.request_id, reviewed_digest=row['sha256'],
                 session_id=profile.session_id, recipient_id=profile.participant_id,
                 target=target, command=command, approval=receipt, cwd=cwd)
    print('Accepted for one delivery to the bound local target.')
    return 0
