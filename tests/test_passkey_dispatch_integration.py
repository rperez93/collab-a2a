"""Default dispatcher routing uses fake page/Hello functions, never a ceremony."""
import builtins
from types import SimpleNamespace

import pytest

from collab.client import approval


@pytest.fixture
def supported(monkeypatch):
    monkeypatch.setattr(approval.sys, 'platform', 'linux')
    # Model the module's host boundary only; mutating global os.name breaks
    # pathlib/pytest on Windows and real WSL markers must not leak into tests.
    monkeypatch.setattr(approval, 'os', SimpleNamespace(name='posix', environ={},
        uname=lambda: SimpleNamespace(release='6.8.0-owned-linux')))
    assert approval.PASSKEY is approval._default_passkey


def forbid_page_import(monkeypatch):
    real = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name.rsplit('.', 1)[-1] in ('passkey', 'passkey_dispatch'):
            pytest.fail('refused route imported the browser page')
        return real(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', guarded)


def test_explicit_nonwindows_passkey_uses_the_default_lazy_page_and_preserves_exact_objects(supported, monkeypatch):
    """The production default reaches the page without replacing PASSKEY itself."""
    from collab.client import passkey
    request = {'canonical_review': 'original-hub-encoded-bytes', 'requester_id': 'p_peer',
               'request_id': 'ar_owned', 'digest': 'd' * 64}
    original = dict(request)
    proof = {'type': 'webauthn.get', 'client_data_json': 'eCBcbiAg',
             'authenticator_data': 'AQID', 'signature': 'BAUG'}
    calls, challenges = [], []
    def fetch_challenge(*, origin):
        challenges.append(origin)
        return {'challenge': 'unchanged callback value'}
    def fake_page(actual, callback):
        assert actual is request and callback is fetch_challenge
        assert callback(origin='http://localhost:12345') == {'challenge': 'unchanged callback value'}
        calls.append(True)
        return {'choice': 'approve', 'challenge_id': 'ac_owned', 'credential_id': 'credential', 'proof': proof}
    monkeypatch.setattr(passkey, 'approve_with_passkey', fake_page)
    monkeypatch.setattr(approval, '_hello', lambda: pytest.fail('passkey selection probed Hello'))
    result = approval.open_window(request, fetch_challenge, method='passkey')
    assert approval.PASSKEY is approval._default_passkey
    assert result == {'choice': 'approve', 'method': 'passkey', 'challenge_id': 'ac_owned',
                      'credential_id': 'credential', 'proof': proof}
    assert result['proof'] is proof and request == original
    assert calls == [True] and challenges == ['http://localhost:12345']


@pytest.mark.parametrize('platform,release,marker', [
    ('win32', 'Windows', None), ('cygwin', 'owned', None), ('msys', 'owned', None),
    ('linux', '5.15.0-microsoft-standard-WSL2', None), ('linux', '4.4.0-Microsoft', None),
    ('linux', 'owned-WSL-kernel', None), ('linux', 'owned', 'WSL_DISTRO_NAME'),
    ('linux', 'owned', 'WSL_INTEROP'),
])
@pytest.mark.parametrize('entry', ['dispatcher', 'default-direct'])
def test_windows_and_wsl_refuse_before_page_import_or_callback(supported, monkeypatch, platform, release, marker, entry):
    """Neither explicit method selection nor direct default call is browser fallback."""
    monkeypatch.setattr(approval.sys, 'platform', platform)
    monkeypatch.setattr(approval.os, 'uname', lambda: SimpleNamespace(release=release), raising=False)
    if marker:monkeypatch.setitem(approval.os.environ, marker, '')  # Even a stripped value preserves the boundary.
    forbid_page_import(monkeypatch)
    monkeypatch.setattr(approval, '_hello', lambda: pytest.fail('platform refusal launched a Hello probe'))
    callback = lambda **kwargs: pytest.fail('refused platform fetched a challenge')
    result = (approval.open_window({}, callback, method='passkey') if entry == 'dispatcher'
              else approval._default_passkey({}, callback))
    assert result == {'choice': 'unavailable', 'reason': 'NativeHelloRequired'}


def test_even_an_injected_explicit_passkey_leg_cannot_bypass_the_windows_policy(supported, monkeypatch):
    monkeypatch.setattr(approval.sys, 'platform', 'win32')
    monkeypatch.setattr(approval, 'PASSKEY', lambda *args: pytest.fail('Windows invoked injected browser leg'))
    assert approval.open_window({}, lambda: {}, method='passkey') == {
        'choice': 'unavailable', 'reason': 'NativeHelloRequired'}


@pytest.mark.parametrize('outcome', [{'choice': 'passkey'}, {'choice': 'unavailable', 'reason': 'NoKey'}, {'choice': 'decline'}])
def test_hello_outcomes_never_implicitly_invoke_a_passkey_page(supported, monkeypatch, outcome):
    """Hello's alternate choice is not authority to use a different enrolled key."""
    forbid_page_import(monkeypatch)
    seen = []
    def open_hello(request, callback):
        seen.append((request, callback))
        return outcome
    monkeypatch.setattr(approval, '_hello', lambda: SimpleNamespace(available=lambda: True, open_window=open_hello))
    request, callback = {}, lambda: pytest.fail('fake Hello must not fetch a challenge')
    result = approval.open_window(request, callback, method='hello')
    assert seen == [(request, callback)]
    assert result == ({'choice': 'unavailable', 'reason': 'ExplicitPasskeySelectionRequired'}
                      if outcome['choice'] == 'passkey' else outcome)


def test_missing_hello_has_no_browser_fallback_or_challenge(supported, monkeypatch):
    forbid_page_import(monkeypatch)
    monkeypatch.setattr(approval, '_hello', lambda: SimpleNamespace(available=lambda: False,
        open_window=lambda *args: pytest.fail('unavailable Hello opened a prompt')))
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='hello') == {
        'choice': 'unavailable', 'reason': 'NoHello'}


@pytest.mark.parametrize('bad_release', [None, b'linux', '', '   ', 'x' * 1025])
def test_unreadable_or_invalid_kernel_metadata_refuses_the_default_page(supported, monkeypatch, bad_release):
    monkeypatch.setattr(approval.os, 'uname', lambda: SimpleNamespace(release=bad_release), raising=False)
    forbid_page_import(monkeypatch)
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='passkey')['choice'] == 'unavailable'


def test_failed_kernel_lookup_is_not_a_reason_to_launch_a_probe(supported, monkeypatch):
    def unavailable():raise OSError('owned synthetic failure')
    monkeypatch.setattr(approval.os, 'uname', unavailable, raising=False)
    forbid_page_import(monkeypatch)
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='passkey') == {
        'choice': 'unavailable', 'reason': 'NativeHelloRequired'}


def test_unknown_method_does_not_import_any_page(supported, monkeypatch):
    forbid_page_import(monkeypatch)
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='other') == {
        'choice': 'unavailable', 'reason': 'UnknownMethod'}


def test_native_windows_os_name_cannot_be_hidden_by_a_posix_platform_string(supported, monkeypatch):
    monkeypatch.setattr(approval.os, 'name', 'nt')
    forbid_page_import(monkeypatch)
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='passkey') == {
        'choice': 'unavailable', 'reason': 'NativeHelloRequired'}


@pytest.mark.parametrize('platform', ['win32', 'wsl'])
@pytest.mark.parametrize('leg', ['default', 'disabled', 'injected'])
def test_native_hello_policy_holds_for_every_explicit_passkey_leg(supported, monkeypatch, platform, leg):
    if platform == 'win32':monkeypatch.setattr(approval.sys, 'platform', 'win32')
    else:monkeypatch.setitem(approval.os.environ, 'WSL_DISTRO_NAME', 'owned-fixture')
    if leg == 'disabled':monkeypatch.setattr(approval, 'PASSKEY', None)
    elif leg == 'injected':monkeypatch.setattr(approval, 'PASSKEY', lambda *args: pytest.fail('browser fallback'))
    forbid_page_import(monkeypatch)
    assert approval.open_window({}, lambda: pytest.fail('challenge'), method='passkey') == {
        'choice': 'unavailable', 'reason': 'NativeHelloRequired'}
