"""A hub's attachment to the machine proxy; hubs never own its process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import proxy
from .client import exclusive
from .client.daemon_files import open_log


def use_proxy(mode: str, setting: str) -> bool:
    if mode == 'own':
        return False
    if mode == 'proxy':
        return True
    return setting != 'off'


class SharedProxy:
    """TunnelSupervisor interface without ownership of the shared tunnel.

    The proxy's own exclusive lock serializes simultaneous startup attempts.
    A session stopping must leave it alone; it exits after the registry empties.
    """

    shared = True

    def __init__(self, session_id: str, *, domain: str = '', timeout: float = 35):
        self.session_id = session_id
        self.domain = domain
        self.timeout = timeout
        self.public_url = ''
        self.last_error = ''
        self.restarts = 0
        self._child = None

    def own_pid(self) -> int:
        return 0

    def stop(self) -> None:
        # Ownership belongs to proxy_main, never to an individual hub.
        pass

    def _url(self, state) -> str:
        if not state.public_url:
            self.last_error = state.error or 'shared proxy has no public tunnel'
            return ''
        if self.domain and urlsplit(state.public_url).hostname != self.domain:
            self.last_error = ('shared proxy uses another domain; use --tunnel own '
                               'for a separate per-session tunnel')
            return ''
        self.last_error = ''
        return proxy.session_base(state.public_url, self.session_id)

    @staticmethod
    def _alive(state) -> bool:
        stamp = exclusive.decode(state.stamp)
        return bool(stamp and stamp.pid == state.pid and stamp.alive())

    def start(self) -> str:
        state = proxy.ProxyState.load()
        if self._alive(state):
            self.public_url = self._url(state)
            return self.public_url
        command = [sys.executable, '-m', 'collab.proxy_main', '--primary', self.session_id]
        if self.domain:
            command.extend(['--domain', self.domain])
        child = self._child
        if child is None or child.poll() is not None:
            with open_log(proxy.state_path().parent / 'proxy.log') as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=log, start_new_session=True, env=dict(os.environ))
            self._child = child
            threading.Thread(target=child.wait, daemon=True,
                             name='collab-proxy-reaper').start()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = proxy.ProxyState.load()
            if self._alive(state):
                self.public_url = self._url(state)
                return self.public_url
            if child.poll() not in (None, 0):
                break
            # A successful competing startup exits zero; wait for its lock holder.
            time.sleep(.05)
        self.last_error = 'shared proxy did not start; inspect proxy/proxy.log in the peer registry'
        self.public_url = ''
        return ''

    def ensure(self) -> tuple[str, bool]:
        previous = self.public_url
        state = proxy.ProxyState.load()
        if self._alive(state):
            self.public_url = self._url(state)
        else:
            self.restarts += 1
            self.start()
        return self.public_url, self.public_url != previous
