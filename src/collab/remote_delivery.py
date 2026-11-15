"""Single-use delivery through the receiver's frozen local wake command."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
import tempfile
import time

from . import wake
from .remote_requests import Inbox, delivery_text
from .worker_runtime import WorkerRuntimeError, _exchange


PROMPT_TTL = 7200
#: Refusals that will never change: the row is dropped, and said refused.
FINAL = frozenset({'unknown_remote_approval', 'remote_reply_window_closed',
                   'remote_already_reported', 'remote_already_answered'})
MAX_PROMPTS = 128


def destination(command):
    """Decode only the shipped adapters that address an existing session."""
    if not isinstance(command, list) or len(command) not in (7, 11):
        raise ValueError('remote delivery requires a session-addressed codex or tmux wake adapter')
    if not Path(command[0]).is_absolute():
        raise ValueError('re-arm the session adapter with collab wake set --agent codex or tmux')
    installed = shutil.which('collab')
    executable = shutil.which(command[0])
    try:
        trusted = installed and executable and os.path.samefile(installed, executable)
    except OSError:
        trusted = False
    if not trusted or command[1:4] != ['wake', 'deliver', '--to'] or command[5] != '--target':
        raise ValueError('remote delivery requires the installed collab session adapter')
    adapter, target = command[4], command[6]
    if adapter == 'codex' and len(command) == 7 and target:
        return {'adapter': adapter, 'session_id': target, 'scope': 'codex-thread'}
    if (adapter == 'tmux' and len(command) == 11 and target
            and command[7] == '--expect-pid' and command[8].isdigit()
            and int(command[8]) > 0 and command[9] == '--expect-command' and command[10]):
        return {'adapter': adapter, 'session_id': target,
                'expected_pid': command[8], 'expected_command': command[10],
                'scope': 'tmux-pane',
                'scope_notice': 'Approval applies to this pane, including replacement conversations of the same agent. The provider conversation identity is not verified.'}
    raise ValueError('remote adapter must address an existing session with required identity pins')


def working_directory(profile):
    return Path(getattr(profile, 'working_path', '') or Path.cwd()).resolve()


def cleanup_prompts(root, now=None):
    now = time.time() if now is None else now
    retained = 0
    for path in root.glob('approved-*.txt'):
        if now - path.stat().st_mtime >= PROMPT_TTL:
            path.unlink()
        else:
            retained += 1
    return retained


def prepare(profile):
    """Blocking local IO; run off the daemon loop. Receipt never calls this."""
    inbox = Inbox(profile.dir)
    if not inbox.path.exists():
        return None
    retained = cleanup_prompts(inbox.root)
    cwd = working_directory(profile)
    config = wake.read_config(profile.dir)
    command = list(config.command)
    try:
        target = inbox.bound_target(command, cwd)
        actual = destination(command)
        if actual['session_id'] != target:
            raise ValueError('bound target differs from the actual adapter session')
    except ValueError:
        inbox.invalidate('unsupported_or_changed_destination')
        return None
    if retained >= MAX_PROMPTS:
        return None
    offer = inbox.claim(session_id=profile.session_id,
                        recipient_id=profile.participant_id,
                        target=target, command=command, cwd=cwd)
    if not offer:
        return None
    return offer, command, max(1.0, min(float(config.timeout), 300.0)), cwd


async def deliver(profile, prepared):
    offer, command, timeout, cwd = prepared
    inbox = Inbox(profile.dir)
    delivered = False
    prompt_path = None
    started = False
    launched = False
    try:
        recorded = await asyncio.to_thread(inbox.start_delivery, offer['id'], offer['receipt'],
                                           target=offer['delivery_target'], command=command, cwd=cwd)
        if recorded is None:
            return False
        started = True
        offer = recorded
        # Both the stdin adapter and existing wake adapters receive precisely
        # the same approved content. Neither argv nor environment comes from
        # the remote envelope. An exit status confirms delivery, not success
        # of the requested task inside the coding session.
        # EXACTLY THE REVIEWED BYTES: the review bound their SHA-256, and
        # start_delivery refused a row whose recorded digest differs.
        prompt = delivery_text(offer, offer['delivery_target'])
        if hashlib.sha256(prompt.encode('utf-8')).hexdigest() != offer['delivery_sha256']:
            raise ValueError('delivery text differs from the reviewed text')
        fd, filename = tempfile.mkstemp(prefix='approved-', suffix='.txt', dir=inbox.root)
        prompt_path = Path(filename)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(prompt)
        env = {**os.environ, 'COLLAB_WAKE_PROMPT': str(prompt_path),
               'COLLAB_WAKE_BATCH': '', 'COLLAB_WAKE_KIND': 'remote',
               'COLLAB_SESSION': profile.session_id}
        # A prepared tuple is not authority after local configuration changes.
        # Recheck immediately before launch; never re-claim on this failure.
        current = list(wake.read_config(profile.dir).command)
        current_cwd = working_directory(profile)
        if (current != command or current_cwd != cwd
                or inbox.bound_target(current, current_cwd) != offer['delivery_target']
                or destination(current)['session_id'] != offer['delivery_target']
                or time.time() >= offer['expires_at']):
            raise ValueError('remote delivery binding changed or offer expired before launch')
        launched = True   # from here on, a failure may have reached the agent
        await _exchange(command, prompt.encode('utf-8'), cwd, env, timeout, None)
        delivered = True
        return True
    except (OSError, ValueError, WorkerRuntimeError):
        return False
    finally:
        # Cancellation or daemon interruption must never recreate an accepted
        # grant. If finishing itself fails, dispatching remains non-replayable.
        if started:
            await asyncio.shield(asyncio.to_thread(
                inbox.finish, offer['id'], offer['receipt'], delivered=delivered, launched=launched))
        # Keep the private prompt file: notice-based adapters hand its path to
        # an existing session, which may open it only after the adapter exits.


def flush(profile, client):
    """Send the hub every owed delivery report and reply; keep what fails.

    Safe to repeat: the hub answers an identical report or reply (same key,
    same content) with its existing success, so a lost acknowledgement costs a
    resend, never a second outcome. A refusal that will never change (the
    request is unknown, expired, or already has another answer) is dropped as
    sent, so one bad row cannot block the rest; anything else stops the pass
    and is tried again on the next one.
    """
    from .client import approval as strong
    inbox = Inbox(profile.dir)
    if not inbox.path.exists():
        return 0
    api = strong.ApprovalClient(client)
    reports, replies = inbox.owed()
    done = 0
    for table, rows, route in (('outcomes', reports, 'report'), ('replies', replies, 'reply')):
        for row in rows:
            body = ({'state': row['state'], 'key': row['key']} if table == 'outcomes'
                    else {'answer': row['answer'], 'key': row['key']})
            try:
                api._call('POST', f"/remote/offers/{strong._path(row['id'])}/{route}", body)
            except strong.ApprovalRefused as exc:
                # Only the hub's own final codes drop a row; an older hub's
                # 404 for a route it lacks keeps it for after an upgrade.
                if exc.code not in FINAL:
                    return done
                inbox.sent(table, row['id'], row['key'], refused=exc.code)
                done += 1
                continue
            except strong.Uncertain:
                return done
            inbox.sent(table, row['id'], row['key'])
            done += 1
    return done


def flush_owed(profile):
    """Blocking; run off the daemon loop. A pass only when something is owed."""
    inbox = Inbox(profile.dir)
    if not inbox.path.exists() or inbox.owed() == ([], []):
        return 0
    from .client.hub_client import HubClient
    from .routes import options
    with HubClient(profile.url, profile.token, **options(profile)) as client:
        return flush(profile, client)
