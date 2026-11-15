"""A hub's attachment to the machine proxy; hubs never own its process."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import proxy
from .client import exclusive
from .client.daemon_files import open_log

logger = logging.getLogger("collab.proxy_host")


def proxy_env(base) -> dict:
    """The environment the MACHINE-WIDE proxy starts with: this hub's, minus
    everything that names one session or one agent. It was `os.environ`
    whole, so the proxy carried the starting hub's COLLAB_HOME, agent kind and
    owner — and an owner is what makes a collab process follow an agent and
    stop when it leaves, which the proxy serving every session must never do.
    """
    from . import owner

    env = owner.without_owner(dict(base))
    for name in ("COLLAB_HOME", "COLLAB_AGENT_KIND"):
        env.pop(name, None)
    return env


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

    def __init__(self, session_id: str, *, domain: str = '', primary: bool = False,
                 timeout: float = 35):
        self.session_id = session_id
        self.domain = domain
        #: Whether this session's host asked, explicitly, to be the session a
        #: bare-domain link reaches (`collab host --primary`, kept in hub.json
        #: so a revived hub asks again). Never implied by starting the proxy:
        #: that made the primary whichever hub noticed a dead proxy first.
        self.primary = primary
        #: The session holding the primary when ours was refused, else ''.
        self.primary_holder = ''
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

    def _claim(self) -> None:
        """Claimed on disk rather than passed to the proxy we may spawn: the
        proxy is usually running already, and an argument to a process that
        is never started reaches nobody. The running proxy reads the primary
        from the state file on each request (briefly cached)."""
        if not self.primary:
            return
        holder = proxy.claim_primary(self.session_id)
        self.primary_holder = '' if holder == self.session_id else holder
        if self.primary_holder:
            logger.warning("the shared proxy's primary is %s; bare-domain links "
                           "reach that session, not this one", holder)
            # Refused once, asked no more: `ensure()` re-runs `start()` and so
            # this claim whenever it finds the proxy dead, and a refused hub
            # kept asking — it took the slot silently after the holder was
            # purged and the proxy restarted (adversarial review of #163). The
            # CLI drops the recorded claim too; asking again is explicit.
            self.primary = False

    def start(self) -> str:
        state = proxy.ProxyState.load()
        # The domain first: a session on another domain must not claim this
        # proxy's root (its public address is refused below anyway).
        if not (self.domain and state.public_url
                and urlsplit(state.public_url).hostname != self.domain):
            self._claim()
        if self._alive(state):
            self.public_url = self._url(state)
            return self.public_url
        command = [sys.executable, '-m', 'collab.proxy_main']
        if self.domain:
            command.extend(['--domain', self.domain])
        child = self._child
        if child is None or child.poll() is not None:
            with open_log(proxy.log_path()) as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=log, start_new_session=True,
                                         env=proxy_env(os.environ))
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
