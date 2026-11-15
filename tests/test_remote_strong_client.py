"""Real HTTP strong remote acceptance; synthetic key, never native prompts."""
import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from collab import cli, wake, remote_delivery
from collab.client import approval as strong
from collab.client.hub_client import HubClient
from collab.protocol import Envelope, KIND_CHAT
from collab.remote_requests import Inbox, make_offer, wire_text, receive_envelope
from test_strong_approval_client import world, no_real_prompt


@pytest.fixture
def remote(world, tmp_path, monkeypatch):
    profile = SimpleNamespace(dir=tmp_path, session_id='s_test', participant_id=world.bob.id,
                              name='bob', working_path=str(tmp_path))
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: profile)
    monkeypatch.setattr(cli, '_client', lambda profile: HubClient(world.base, 'bob-token'))
    command = [shutil.which('collab'), 'wake', 'deliver', '--to', 'codex', '--target', 'thread-remote']
    wake.write_config(tmp_path, wake.WakeConfig(command=command))
    sender = world.store.participant_for_token(world.host_token)
    offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=world.bob.id,
                       target='thread-remote', kind='prompt', text='REMOTE_EXACT_PAYLOAD')
    env = world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
        to='bob', to_id=world.bob.id, room=None, text=wire_text(offer)))
    receive_envelope(profile, env)
    inbox = Inbox(tmp_path)
    inbox.bind('thread-remote', command, tmp_path)
    # The host's real setting, not a patched capability: the hub then both
    # advertises remote delivery and prepares and consumes the review.
    from collab.config import set_remote_delivery
    set_remote_delivery(True)
    return SimpleNamespace(world=world, profile=profile, inbox=inbox, offer=offer, env=env, command=command)


def accept(r):
    return cli.main(['remote', 'accept', r.offer['id']])


def test_strong_remote_acceptance_delivers_exactly_once(remote, monkeypatch):
    assert accept(remote) == 0
    assert remote.inbox.list()[0]['state'] == 'accepted'
    window = remote.world.hello.windows[-1]
    canonical = json.loads(window['review_text'])
    assert all(window[k] == canonical[k] for k in ('subject', 'destination', 'authority'))
    attempts = []
    async def exchange(command, prompt, cwd, env, timeout, callback):
        attempts.append((command, prompt, cwd))
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared is not None
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is True
    assert len(attempts) == 1 and b'REMOTE_EXACT_PAYLOAD' in attempts[0][1]
    assert attempts[0][0] == remote.command
    assert remote_delivery.prepare(remote.profile) is None
    assert remote.inbox.list()[0]['state'] == 'delivered'


def test_lost_consumption_answer_never_dispatches_or_retries(remote, monkeypatch):
    original = strong.ApprovalClient._call
    consumed = []
    def call(self, method, path, body=None):
        result = original(self, method, path, body)
        if path.endswith('/consume'):
            consumed.append(result)
            raise strong.Uncertain('lost response after commit')
        return result
    monkeypatch.setattr(strong.ApprovalClient, '_call', call)
    assert accept(remote) == 1
    assert len(consumed) == 1
    assert remote.inbox.list()[0]['state'] == 'approving'
    assert remote_delivery.prepare(remote.profile) is None
    assert accept(remote) == 1
    assert len(consumed) == 1


@pytest.mark.parametrize('prior_accepted', [False, True])
def test_changed_configuration_after_human_gesture_refuses(remote, monkeypatch, prior_accepted):
    first_id = remote.offer['id']
    if prior_accepted:
        assert accept(remote) == 0
        sender = remote.world.store.participant_for_token(remote.world.host_token)
        remote.offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=remote.world.bob.id,
                                  target='thread-remote', kind='prompt', text='second offer')
        env = remote.world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
            to='bob', to_id=remote.world.bob.id, room=None, text=wire_text(remote.offer)))
        receive_envelope(remote.profile, env)
    original = remote.world.hello.open_window
    def window(request, fetch):
        result = original(request, fetch)
        wake.write_config(remote.profile.dir, wake.WakeConfig(command=[]))
        return result
    monkeypatch.setattr(remote.world.hello, 'open_window', window)
    assert accept(remote) == 1
    states = {r['id']: r['state'] for r in remote.inbox.list()}
    assert states[remote.offer['id']] == 'pending'
    if prior_accepted:
        assert states[first_id] == 'failed'
    assert remote_delivery.prepare(remote.profile) is None


def test_changed_review_is_refused_before_window(remote, monkeypatch):
    original = strong.ApprovalClient.review
    def review(self, identity):
        result = original(self, identity)
        result['review']['binding']['cwd'] = '/different'
        return result
    monkeypatch.setattr(strong.ApprovalClient, 'review', review)
    assert accept(remote) == 1
    assert remote.world.hello.windows == []
    assert remote.inbox.list()[0]['state'] == 'pending'


def test_remote_capability_is_separate_and_has_no_terminal_fallback(remote, monkeypatch):
    monkeypatch.setattr(strong.ApprovalClient, 'capabilities', lambda self: {'available': True, 'supported_methods': ['hello']})
    from collab import consent
    monkeypatch.setattr(consent, 'request', lambda *a, **k: pytest.fail('terminal fallback'))
    assert accept(remote) == 1
    assert remote.world.hello.windows == []


def test_decline_is_local_and_never_consumes(remote):
    remote.world.hello.choice = 'decline'
    assert accept(remote) == 0
    assert remote.inbox.list()[0]['state'] == 'declined'
    assert remote_delivery.prepare(remote.profile) is None


@pytest.mark.parametrize('change', ['command', 'expiry'])
def test_change_after_prepare_never_launches_or_replays(remote, monkeypatch, change):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared is not None
    if change == 'command':
        wake.write_config(remote.profile.dir, wake.WakeConfig(command=[]))
    else:
        monkeypatch.setattr(remote_delivery.time, 'time', lambda: remote.offer['expires_at'] + 1)
    async def forbidden(*a, **k):
        pytest.fail('stale prepared attempt launched')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    # Refused before the adapter started: nothing reached the agent, so the
    # requester is told `failed`, not `uncertain` — and it is never relaunched.
    assert remote.inbox.list()[0]['state'] == 'failed'
    assert remote_delivery.prepare(remote.profile) is None


def test_receiver_selected_goal_uses_the_reviewed_local_target(remote):
    sender = remote.world.store.participant_for_token(remote.world.host_token)
    remote.offer = make_offer(session_id='s_test', sender_id=sender.id, recipient_id=remote.world.bob.id,
                              target='receiver-selected', kind='goal', text='reviewed connection goal')
    env = remote.world.store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
        to='bob', to_id=remote.world.bob.id, room=None, text=wire_text(remote.offer)))
    receive_envelope(remote.profile, env)
    assert accept(remote) == 0
    reviewed = json.loads(remote.world.hello.windows[-1]['review_text'])
    assert reviewed['offer']['kind'] == 'goal'
    assert reviewed['binding']['target'] == 'thread-remote'
    prepared = remote_delivery.prepare(remote.profile)
    assert prepared[0]['id'] == remote.offer['id']
    assert prepared[0]['delivery_target'] == 'thread-remote'


def test_a_prepared_delivery_tuple_cannot_be_executed_twice(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    attempts = []
    async def exchange(*args):
        attempts.append(args)
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is True
    try:
        asyncio.run(remote_delivery.deliver(remote.profile, prepared))
    except ValueError:
        pass
    assert len(attempts) == 1


def test_concurrent_prepared_delivery_reuse_has_one_launch_owner(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    attempts = []
    async def exchange(*args):
        attempts.append(args)
        await asyncio.sleep(.02)
    monkeypatch.setattr(remote_delivery, '_exchange', exchange)
    async def race():
        return await asyncio.gather(remote_delivery.deliver(remote.profile, prepared),
                                    remote_delivery.deliver(remote.profile, prepared))
    assert sorted(asyncio.run(race())) == [False, True]
    assert len(attempts) == 1
    assert remote.inbox.list()[0]['state'] == 'delivered'


def test_old_inflight_record_is_not_relaunched_after_marker_migration(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    import sqlite3
    with sqlite3.connect(remote.inbox.path) as db:
        db.execute('ALTER TABLE offers DROP COLUMN delivery_started')
    async def forbidden(*a, **k):
        pytest.fail('old in-flight record relaunched')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    assert remote.inbox.list()[0]['state'] == 'dispatching'
    assert remote_delivery.prepare(remote.profile) is None


def test_rebinding_away_and_back_cannot_revive_prepared_delivery(remote, monkeypatch):
    assert accept(remote) == 0
    prepared = remote_delivery.prepare(remote.profile)
    other = remote.command[:-1] + ['another-thread']
    remote.inbox.bind('another-thread', other, remote.profile.working_path)
    remote.inbox.bind('thread-remote', remote.command, remote.profile.working_path)
    async def forbidden(*a, **k):
        pytest.fail('revoked prepared delivery revived')
    monkeypatch.setattr(remote_delivery, '_exchange', forbidden)
    assert asyncio.run(remote_delivery.deliver(remote.profile, prepared)) is False
    assert remote.inbox.list()[0]['state'] == 'failed'


def test_failed_launch_binding_cannot_be_retried_with_restored_arguments(remote):
    assert accept(remote) == 0
    offer = remote_delivery.prepare(remote.profile)[0]
    assert remote.inbox.start_delivery(offer['id'], offer['receipt'], target='thread-remote',
        command=remote.command, cwd='/wrong') is None
    assert remote.inbox.start_delivery(offer['id'], offer['receipt'], target='thread-remote',
        command=remote.command, cwd=remote.profile.working_path) is None
    assert remote.inbox.list()[0]['state'] == 'failed'


def _forge_hub_review(monkeypatch, change):
    """A faulty or hostile hub: it builds the canonical review from ANOTHER
    operation than the one the receiver holds, and keeps it self-consistent —
    digests recomputed, the served review equal to its bytes, the prepared
    digest equal to the review's — so only a field-by-field comparison with
    the receiver's own offer and binding can tell."""
    from collab.remote_requests import digest
    from collab.server import approval_remote
    served = []
    original = approval_remote.RemoteApprovals._review

    def forged(value):
        value = json.loads(json.dumps(value))
        if change == 'offer_text':
            value['offer']['text'] = 'HUB_SUBSTITUTED_PROMPT'
            value['offer_digest'] = digest(value['offer'])
        elif change == 'binding_argv':
            value['binding']['command'] = ['/usr/bin/hub-chosen-program', '--run']
            value['binding_digest'] = approval_remote.delivery_binding(value['binding'])
        elif change == 'binding_cwd':
            value['binding']['cwd'] = '/hub/chosen/directory'
            value['binding_digest'] = approval_remote.delivery_binding(value['binding'])
        else:
            value['requester_id'] = 'p_somebody_else'
        served.append(value)
        return original(value)

    monkeypatch.setattr(approval_remote.RemoteApprovals, '_review', staticmethod(forged))
    return served


@pytest.mark.parametrize('change', ['offer_text', 'binding_argv', 'binding_cwd', 'requester'])
def test_a_hub_review_of_another_operation_opens_no_window_and_approves_nothing(remote, monkeypatch, capsys, change):
    """R5 of the #118 review. The receiver delivers ITS local offer to ITS
    local binding, so the human must be shown exactly those. A hub that shows
    prompt X, another program, another directory or another requester while
    the receiver would deliver Y has to be caught before the window opens:
    the human's gesture would otherwise approve what they never saw."""
    served = _forge_hub_review(monkeypatch, change)
    assert accept(remote) == 1
    assert served, 'the forged review was never built'
    assert 'the canonical review is not this remote offer and local destination' in capsys.readouterr().err
    assert remote.world.hello.windows == []
    db = remote.world.store._db
    assert db.execute('SELECT COUNT(*) FROM approval_challenges').fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE state IN ('approved','consumed')").fetchone()[0] == 0
    assert remote.inbox.list()[0]['state'] == 'pending'
    assert remote_delivery.prepare(remote.profile) is None


def test_a_consumption_answer_for_other_bytes_authorizes_no_delivery(remote, monkeypatch, capsys):
    """R13 of the #118 review. The hub consumed the receipt, but its answer
    carries canonical bytes that are not the review the human approved. The
    attempt stays uncertain and is never dispatched: what was consumed is not
    known to be what was shown."""
    from collab.consent_hello import b64url, unb64url
    original = strong.ApprovalClient._call

    def call(self, method, path, body=None):
        result = original(self, method, path, body)
        if path.endswith('/consume'):
            raw = json.loads(unb64url(result['canonical_review'], limit=strong.MAX_REVIEW))
            raw['offer']['text'] = 'OTHER_BYTES'
            result['canonical_review'] = b64url(json.dumps(raw).encode())
        return result

    monkeypatch.setattr(strong.ApprovalClient, '_call', call)
    assert accept(remote) == 1
    assert 'the consumption answer did not match this reviewed operation' in capsys.readouterr().err
    assert remote.inbox.list()[0]['state'] == 'approving'
    assert remote_delivery.prepare(remote.profile) is None


def test_remote_delivery_is_on_by_default_and_the_host_setting_turns_it_off(remote, capsys):
    """Nothing patched. With the host's `remote_delivery` never set the hub
    advertises remote delivery; `collab config remote_delivery off` is the
    host's off switch, after which the receiver opens no window and the hub
    prepares no review. On again, delivery still waits on the gesture."""
    from collab.config import load_config, save_config, set_remote_delivery, settings, REMOTE_DELIVERY_DEFAULT
    cfg = load_config()
    cfg.pop("remote_delivery", None)  # never set: the default decides
    save_config(cfg)
    assert REMOTE_DELIVERY_DEFAULT is True
    assert next(s for s in settings() if s.name == 'remote_delivery').default is True
    assert strong.ApprovalClient(remote.world.bob_hub).capabilities()['remote_delivery'] is True
    set_remote_delivery(False)
    assert strong.ApprovalClient(remote.world.bob_hub).capabilities()['remote_delivery'] is False
    assert accept(remote) == 1
    assert 'not enabled strong remote delivery' in capsys.readouterr().err
    assert remote.world.hello.windows == []
    db = remote.world.store._db
    assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE kind='remote-deliver'").fetchone()[0] == 0
    set_remote_delivery(True)
    assert accept(remote) == 0 and len(remote.world.hello.windows) == 1


def test_on_by_default_a_remote_offer_still_needs_the_receivers_gesture(remote):
    """Default on is not consent. Without an approving gesture — the human
    declines in the window — nothing is consumed and nothing is delivered."""
    from collab.config import load_config, save_config
    cfg = load_config()
    cfg.pop("remote_delivery", None)
    save_config(cfg)
    remote.world.hello.choice = 'decline'
    assert accept(remote) == 0
    assert remote.inbox.list()[0]['state'] == 'declined'
    assert remote_delivery.prepare(remote.profile) is None
    db = remote.world.store._db
    assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE state IN ('approved','consumed')").fetchone()[0] == 0

def test_a_passkey_receiver_is_shown_this_remote_operation_on_the_passkey_page(remote, monkeypatch):
    """Main now advertises passkeys, and remote acceptance dispatches on the
    enrolled key's method. The passkey page binds the hub-canonical bytes it
    is handed, so those must be THIS remote review — offer, binding and the
    summary fields — verified against the digest. A decline there consumes
    nothing. (The page's WebAuthn signing itself is exercised only through
    main's own passkey tests; remote signatures here use the Hello fake.)"""
    import hashlib
    from collab.consent_hello import unb64url
    shown = []
    def passkey_page(request, fetch_challenge):
        shown.append(request)
        return {"choice": "decline"}
    monkeypatch.setattr(strong, "_native_hello_platform", lambda: False)
    monkeypatch.setattr(strong, "PASSKEY", passkey_page)
    keys = strong.ApprovalClient.keys
    monkeypatch.setattr(strong.ApprovalClient, "keys",
                        lambda self: [k | {"method": "passkey"} for k in keys(self)])
    assert accept(remote) == 0
    [request] = shown
    canonical = unb64url(request["canonical_review"], limit=strong.MAX_REVIEW)
    assert hashlib.sha256(canonical).hexdigest() == request["digest"]
    review = json.loads(canonical)
    assert review["operation"] == "remote-deliver" and review["offer"] == remote.offer
    assert review["binding"]["command"] == remote.command
    assert all(request[k] == review[k] for k in ("subject", "destination", "authority"))
    assert remote.world.hello.windows == []  # never the Hello window for a passkey key
    assert remote.inbox.list()[0]["state"] == "declined"
    db = remote.world.store._db
    assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE state='consumed'").fetchone()[0] == 0


def test_an_expired_window_is_said_as_expired_and_the_request_stays_pending(remote, capsys):
    """Live Hello test: every outcome that was not approve or decline read 'the
    remote operation was not approved'. Nobody choosing in time is now said as
    such, and the request stays pending to be accepted again."""
    remote.world.hello.choice = 'expired'
    assert accept(remote) == 1
    said = capsys.readouterr().err
    assert 'expired' in said and 'still pending' in said and 'not approved' not in said
    assert remote.inbox.list()[0]['state'] == 'pending'


def test_a_window_that_could_not_be_shown_is_said_with_its_scrubbed_reason(remote, monkeypatch, capsys):
    monkeypatch.setattr(remote.world.hello, 'open_window',
                        lambda request, fetch: {'choice': 'unavailable', 'reason': 'NoHello\x1b[31m'})
    assert accept(remote) == 1
    said = capsys.readouterr().err
    assert 'unavailable' in said and 'NoHello' in said and 'still pending' in said
    assert '\x1b[31m' not in said, 'the reason is scrubbed before it is shown'
    assert remote.inbox.list()[0]['state'] == 'pending'


def test_choosing_a_passkey_in_the_hello_window_is_said_as_that_choice(remote, monkeypatch, capsys):
    """Lead gate on #215: 'use a passkey instead' is the human's choice in a
    window that WAS shown, not a window that could not be shown."""
    monkeypatch.setattr(remote.world.hello, 'open_window',
                        lambda request, fetch: {'choice': 'unavailable', 'reason': 'ExplicitPasskeySelectionRequired'})
    assert accept(remote) == 1
    said = capsys.readouterr().err
    assert 'you chose a passkey' in said and 'Windows Hello' in said and 'could not be shown' not in said
    assert remote.inbox.list()[0]['state'] == 'pending'


@pytest.mark.parametrize('reason', ['NoHello', 'NativeHelloRequired', strong.PASSKEY_SEAM])
def test_a_window_that_really_could_not_be_shown_still_says_so(remote, monkeypatch, capsys, reason):
    monkeypatch.setattr(remote.world.hello, 'open_window',
                        lambda request, fetch: {'choice': 'unavailable', 'reason': reason})
    assert accept(remote) == 1
    said = capsys.readouterr().err
    assert 'could not be shown' in said and 'you chose a passkey' not in said


def test_an_offer_that_expired_during_the_window_says_to_ask_again(remote, monkeypatch, capsys):
    """Lead LOW on #215: if the offer itself has expired, accepting again would
    only be refused, so the message says to ask the sender for a new one."""
    import time as real_time
    from collab.client import remote_approval
    later = {'now': None}
    def window(request, fetch):
        later['now'] = real_time.time() + 24 * 3600     # the window outlived the offer
        return {'choice': 'expired'}
    monkeypatch.setattr(remote.world.hello, 'open_window', window)
    monkeypatch.setattr(remote_approval, 'time', type('T', (), {
        'time': staticmethod(lambda: later['now'] or real_time.time()),
        '__getattr__': lambda self, name: getattr(real_time, name)})())
    assert accept(remote) == 1
    said = capsys.readouterr().err
    assert 'offer itself has expired' in said and 'ask the sender' in said and 'accept it again' not in said
