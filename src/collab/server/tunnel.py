"""Optional public exposure via ngrok.

ngrok is detected and used when present, and recommended when not — it is never
installed automatically.  Without a tunnel the hub is still fully usable; it is
just reachable on this machine and LAN only.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

#: ngrok's local API is on 4040 only if that port was free. A second agent —
#: and users often already have one running — moves to 4041, 4042, ... so
#: polling 4040 alone reads someone else's agent and concludes we have no
#: tunnel while ours is up and serving.
NGROK_API_PORTS = range(4040, 4046)
START_TIMEOUT = 25.0

#: ngrok prints this once the tunnel is live; our own log is the authoritative
#: answer for our own agent, whichever API port it ended up on.
_LOG_URL_RE = re.compile(r'url=(https://[^\s"]+)')

#: ngrok's own diagnosis when it refuses to start. It prints the sentence once
#: as `failed to start tunnel: ...` and the code as `ERR_NGROK_<n>`. Both are
#: read back so the host can say WHY there is no tunnel: on 2026-11-13 the
#: account's one endpoint was held by another ngrok process (ERR_NGROK_334),
#: and `collab host` answered with «ngrok was not found» — true of nothing.
_LOG_ERR_CODE_RE = re.compile(r"ERR_NGROK_\d+")
#: The sentence ends at a full stop FOLLOWED BY SPACE — the message quotes a
#: domain, and a bare «.» stopped it at «https://willing-settled-iguana».
_LOG_ERR_TEXT_RE = re.compile(r"failed to start tunnel: (.+?)(?:\.(?=\s|$)|\\n|\n|\")")

#: What to do about the failures people actually meet. Keyed by ngrok's code;
#: anything else gets ngrok's own words and the generic «tunnel it yourself».
TUNNEL_REMEDIES = {
    "ERR_NGROK_334": (
        "this ngrok account's endpoint is already online in another ngrok "
        "process. Use a reserved domain of your own — `collab host --resume "
        "--domain <domain>`, or once for every session: `collab config "
        "ngrok_domain <domain>` — or stop that other process yourself."),
    "ERR_NGROK_4018": (
        "ngrok has no authtoken: `ngrok config add-authtoken <token>`, then "
        "re-run `collab host --resume`."),
    "ERR_NGROK_108": (
        "this ngrok account allows one agent session at a time, and another "
        "is running. Reuse it (`ngrok http <port>` in that agent) or stop it."),
}


def ngrok_path() -> str | None:
    return shutil.which("ngrok")


def ngrok_version() -> str | None:
    exe = ngrok_path()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True,
                             timeout=5, check=False).stdout.strip()
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def local_ip() -> str:
    """Best-effort LAN address, for sharing without a tunnel."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Tunnel:
    public_url: str
    process: subprocess.Popen | None = None

    def alive(self) -> bool:
        """Is this tunnel still actually forwarding?

        The agent process can outlive the tunnel (a free ngrok session ends on
        its own), so process liveness alone is not enough — the agent's API has
        to still be listing a tunnel for our port.
        """
        if self.process is not None and self.process.poll() is not None:
            return False
        return self.public_url in _tunnel_urls()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)


def _all_tunnels() -> list[dict]:
    """Every tunnel from every ngrok agent running on this machine."""
    # Imported where it is used: cli.py imports this module for `free_port`
    # and `local_ip`, and the only thing here that speaks HTTP is this probe
    # of ngrok's local API. See update.check for the cost being avoided.
    import httpx

    found: list[dict] = []
    for api_port in NGROK_API_PORTS:
        try:
            r = httpx.get(f"http://127.0.0.1:{api_port}/api/tunnels", timeout=1.0)
            if r.status_code == 200:
                found.extend(r.json().get("tunnels", []))
        except (httpx.HTTPError, ValueError):
            continue
    return found


def _tunnel_urls() -> set[str]:
    return {t.get("public_url", "") for t in _all_tunnels()}


def _existing_tunnel(port: int) -> str | None:
    """Reuse a tunnel any local ngrok agent already has for this port."""
    for t in _all_tunnels():
        addr = t.get("config", {}).get("addr", "")
        if addr.endswith(f":{port}") and t.get("public_url", "").startswith("https://"):
            return str(t["public_url"])
    return None


def _url_from_log(path: str | None, offset: int) -> str | None:
    """Read the URL out of our own agent's log, from where it started."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            body = fh.read()
    except OSError:
        return None
    matches = _LOG_URL_RE.findall(body)
    return matches[-1] if matches else None


def failure_from_log(path: str | None, offset: int) -> str:
    """ngrok's reason for refusing to start, from our own agent's log.

    Empty when the log says nothing recognisable — a caller then says «no
    URL» rather than inventing a cause.
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            body = fh.read()
    except OSError:
        return ""
    codes = _LOG_ERR_CODE_RE.findall(body)
    text = _LOG_ERR_TEXT_RE.search(body)
    reason = text.group(1).strip() if text else ""
    if codes and reason:
        return f"{codes[-1]}: {reason}"
    return codes[-1] if codes else reason


def remedy_for(error: str) -> str:
    """The advice for a failure `failure_from_log` reported, or empty."""
    code = _LOG_ERR_CODE_RE.search(error or "")
    return TUNNEL_REMEDIES.get(code.group(0), "") if code else ""


def start_tunnel(port: int, *, log_path: str | None = None,
                 domain: str | None = None,
                 errors: list[str] | None = None) -> Tunnel | None:
    """Return a public https URL for ``port``, or None if ngrok is unavailable.

    ``domain`` pins a reserved ngrok domain. Worth doing: without one, a tunnel
    that dies comes back on a *different* random URL, which invalidates every
    join link already handed out.

    ``errors``, when given, receives ngrok's reason for a failed start, so the
    host can print it instead of a guess.
    """
    exe = ngrok_path()
    if not exe:
        return None

    if (url := _existing_tunnel(port)) is not None:
        return Tunnel(public_url=url, process=None)

    argv = [exe, "http", str(port), "--log", "stdout"]
    if domain:
        argv += ["--domain", domain]

    # Note where our agent's output starts, so we read our own URL and not a
    # previous run's.
    offset = 0
    if log_path:
        try:
            offset = os.path.getsize(log_path)
        except OSError:
            offset = 0
    log = open(log_path, "a") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        argv,
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=os.environ.copy(),
    )

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            # ngrok exited. Its log names the cause — a missing authtoken, or
            # the account's endpoint already online elsewhere — so carry it.
            if errors is not None:
                errors.append(failure_from_log(log_path, offset)
                              or f"ngrok exited with status {proc.returncode}")
            return None
        # Our own log first: it is definitive for our agent regardless of which
        # API port it landed on.
        if (url := _url_from_log(log_path, offset)) is not None:
            return Tunnel(public_url=url, process=proc)
        if (url := _existing_tunnel(port)) is not None:
            return Tunnel(public_url=url, process=proc)
        time.sleep(0.4)

    if errors is not None:
        errors.append(failure_from_log(log_path, offset)
                      or f"ngrok gave no public URL within {START_TIMEOUT:.0f} s")
    # TERMINATE, THEN MAKE SURE. One SIGTERM to a process in its own session and
    # an immediate return leaves a slow ngrok running with nobody holding its
    # pid: `own_pid()` reports 0, nothing records it, and `collab kill` never
    # stops it. The same leak class the pid stamps exist for, at the one moment
    # collab knows for certain which process it is.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=5)
    return None


#: For a machine where ngrok IS installed but did not give us a tunnel. The
#: session is local-only until one of these is handed out instead.
TUNNEL_YOURSELF_HELP = """\
This session is reachable on this machine only. To share it, tunnel it yourself
and hand out that URL instead:
  ngrok http {port} --domain <a reserved domain>
  cloudflared tunnel --url http://localhost:{port}
  tailscale funnel {port}
"""


NO_NGROK_HELP = """\
ngrok was not found, so this session is reachable on this machine only.

To share it with someone else, either:
  1. install ngrok      https://ngrok.com/download   then re-run `collab host`
  2. or tunnel it yourself, and hand out that URL instead:
       ngrok http {port}
       cloudflared tunnel --url http://localhost:{port}
       tailscale funnel {port}
"""

NGROK_FAILED_HELP = """\
ngrok is installed but did not start, so this session is reachable on this
machine only. The usual cause is a missing authtoken — a free account needs it
once:
  ngrok config add-authtoken <token>     https://dashboard.ngrok.com/get-started/your-authtoken
then re-run `collab host`. ngrok's own output is in {log}
"""


class TunnelSupervisor:
    """Keeps a tunnel up for the life of the hub.

    A free ngrok tunnel ends on its own schedule, which silently cuts every
    participant off while the hub itself carries on running perfectly. This
    notices and brings it back. The session, its database and every issued
    token are untouched by a restart — only the public URL changes, and only
    when no reserved domain is pinned.
    """

    def __init__(self, port: int, *, log_path: str | None = None,
                 domain: str | None = None) -> None:
        self.port = port
        self.log_path = log_path
        self.domain = domain
        self.tunnel: Tunnel | None = None
        self.restarts = 0
        #: ngrok's reason the last start failed; empty once a start succeeds.
        self.last_error = ""

    @property
    def public_url(self) -> str:
        return self.tunnel.public_url if self.tunnel else ""

    def own_pid(self) -> int:
        """The agent we launched, or 0 if we are reusing someone else's."""
        if self.tunnel is None or self.tunnel.process is None:
            return 0
        return int(self.tunnel.process.pid)

    def start(self) -> str:
        errors: list[str] = []
        self.tunnel = start_tunnel(self.port, log_path=self.log_path,
                                   domain=self.domain, errors=errors)
        self.last_error = "" if self.tunnel is not None else (errors[-1] if errors else "")
        return self.public_url

    def ensure(self) -> tuple[str, bool]:
        """Check the tunnel, restarting it if it has gone.

        Returns ``(url, changed)`` — ``changed`` is True when the public URL is
        different from before, which means previously shared links are dead.
        """
        if self.tunnel is not None and self.tunnel.alive():
            return self.public_url, False

        previous = self.public_url
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
        self.start()
        if self.tunnel is not None:
            self.restarts += 1
        return self.public_url, self.public_url != previous

    def stop(self) -> None:
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
