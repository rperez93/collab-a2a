"""Single-use delivery through the receiver's frozen local wake command."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import time

from . import wake
from .remote_requests import Inbox
from .worker_runtime import WorkerRuntimeError, _exchange


PROMPT_TTL = 7200
MAX_PROMPTS = 128


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
    except ValueError:
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
    try:
        # Both the stdin adapter and existing wake adapters receive precisely
        # the same approved content. Neither argv nor environment comes from
        # the remote envelope. An exit status confirms delivery, not success
        # of the requested task inside the coding session.
        prompt = ('Human-approved remote ' + offer['kind'] + ' request.\n'
                  'Request: ' + offer['id'] + '\n'
                  'Target: ' + offer['delivery_target'] + '\n\n' + offer['text'])
        fd, filename = tempfile.mkstemp(prefix='approved-', suffix='.txt', dir=inbox.root)
        prompt_path = Path(filename)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(prompt)
        env = {**os.environ, 'COLLAB_WAKE_PROMPT': str(prompt_path),
               'COLLAB_WAKE_BATCH': '', 'COLLAB_WAKE_KIND': 'remote',
               'COLLAB_SESSION': profile.session_id}
        await _exchange(command, prompt.encode('utf-8'), cwd, env, timeout, None)
        delivered = True
        return True
    except (OSError, ValueError, WorkerRuntimeError):
        return False
    finally:
        # Cancellation or daemon interruption must never recreate an accepted
        # grant. If finishing itself fails, dispatching remains non-replayable.
        await asyncio.shield(asyncio.to_thread(
            inbox.finish, offer['id'], offer['receipt'], delivered=delivered))
        # Keep the private prompt file: notice-based adapters hand its path to
        # an existing session, which may open it only after the adapter exits.
