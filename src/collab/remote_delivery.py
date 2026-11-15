"""Single-use delivery through the receiver's frozen local wake command."""
from __future__ import annotations

import asyncio
import hashlib
import json
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
    if adapter == 'listen' and len(command) == 7 and target == wake.LISTEN_TARGET:
        return {'adapter': adapter, 'session_id': target, 'scope': 'listen-stream'}
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
        if destination(command)['adapter'] == 'listen':
            reached = await _to_stream(profile.dir, offer, prompt, timeout)
            if reached is None:          # withdrawn unprinted: nothing reached it
                launched = False
                return False
            delivered = reached
            return delivered
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
    # REPORTS BEFORE REPLIES: the hub takes an answer only after the hand-off
    # was reported, and a pass that cannot send a report stops before any
    # reply (each `return done` below).
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


# --- the listen stream ---------------------------------------------------------------
# For an agent that holds `collab listen --follow` in a monitor (Claude Code):
# the approved prompt is printed ONCE on that stream, never typed anywhere.
# One file per request moves through atomic renames, so exactly one of the
# monitor (claim) and the daemon (withdraw) wins:
#   <id>.json  offered  -> <id>.claimed  a monitor took it -> <id>.printed  written
#              offered  -> <id>.withdrawn  nobody took it in time: never printed

STREAM_GRACE = 5.0


def stream_dir(root):
    return Inbox(root).root / 'listen'


def _stream_id(request_id):
    if not isinstance(request_id, str) or not request_id.startswith('rq_') or not request_id[3:].isalnum():
        raise ValueError('invalid remote request id')
    return request_id


async def _to_stream(root, offer, prompt, timeout):
    """True: printed on the stream. None: withdrawn, never printed. False: a
    monitor took it but did not confirm writing it (uncertain)."""
    folder = stream_dir(root)
    request_id = _stream_id(offer['id'])
    record = {'request_id': request_id, 'sender_id': offer['sender_id'], 'kind': offer['kind'],
              'text': prompt, 'reply': f'collab remote reply {request_id} "<your answer>"'}

    def offer_it():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.offer-', dir=folder)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False)
        os.replace(tmp, folder / f'{request_id}.json')
    await asyncio.to_thread(offer_it)
    printed = folder / f'{request_id}.printed'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if printed.exists():
            return True
        await asyncio.sleep(0.25)
    try:
        os.rename(folder / f'{request_id}.json', folder / f'{request_id}.withdrawn')
        return None
    except FileNotFoundError:
        pass                              # a monitor claimed it: give it a moment
    end = time.monotonic() + STREAM_GRACE
    while time.monotonic() < end:
        if printed.exists():
            return True
        await asyncio.sleep(0.25)
    return False


def claim_for_stream(root):
    """The monitor's side: take every offered prompt, at most once each."""
    folder = stream_dir(root)
    if not folder.is_dir():
        return []
    taken = []
    for path in sorted(folder.glob('rq_*.json')):
        claimed = path.with_suffix('.claimed')
        try:
            os.rename(path, claimed)
        except FileNotFoundError:
            continue                      # withdrawn, or another monitor took it
        try:
            record = json.loads(claimed.read_text(encoding='utf-8'))
            _stream_id(record.get('request_id'))
            if record['request_id'] != path.stem or not isinstance(record.get('text'), str):
                raise ValueError('mismatched stream record')
        except (OSError, ValueError, KeyError, TypeError):
            continue                      # left claimed: the daemon reports uncertain
        taken.append(record)
    return taken


def mark_printed(root, request_id):
    folder = stream_dir(root)
    os.rename(folder / f'{_stream_id(request_id)}.claimed', folder / f'{request_id}.printed')


def stream_line(record, as_json):
    """One line: the approved prompt as data, labelled with what to do."""
    from .terminal_text import display_text
    if as_json:
        return json.dumps({'kind': 'remote-prompt', 'local': True, **record}, ensure_ascii=False)
    return display_text(
        f"[remote prompt {record['request_id']}] approved by your human for this session, "
        f"from participant {record['sender_id']}. Do what it asks, then answer with: "
        f"{record['reply']}. Prompt (JSON string): {json.dumps(record['text'], ensure_ascii=True)}")
