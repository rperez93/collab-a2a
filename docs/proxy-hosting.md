# One tunnel for multiple sessions

`collab host` can attach a session to a shared machine proxy, one tunnel for
every session on the machine. The public link is
`https://domain/s/<session_id>#<invite>`. The proxy preserves authentication,
streams events and file bodies, and routes only to local live host records.

It is opt-in this release: the `proxy` setting defaults to `off`, so an upgrade
leaves every hub on its own tunnel and every address where it was.

```bash
collab config proxy on                 # every session on this machine
collab host --tunnel proxy             # this session only
collab host --tunnel own               # this session on its own tunnel
collab host --no-tunnel                # local only
```

`on` and `auto` use the shared proxy; `off` uses a separate tunnel per hub.
`proxy` is a machine setting: one proxy serves the whole machine, so an
agent's own settings layer cannot choose it.

## The primary session

A bare-domain link (no `/s/<id>`) reaches one session, the **primary**: every
link handed out before the proxy existed is such a link, and it named the
session that held the domain then. The primary is chosen explicitly, with
`collab host --primary` (which implies `--tunnel proxy`), and is kept with the
session so a hub its daemon revives asks again. Starting or restarting the
proxy never chooses one. Once recorded, only that session may set it; a second
session asking is refused, and `collab host` says which session holds it. The
primary remains fixed when that session ends: the bare route returns 404,
never another session. Changing it is deliberate: with the proxy stopped,
remove `primary` from `proxy/state.json` in the peer registry.
An explicit host mode is saved for resume. Local-only hosting starts neither
kind of tunnel. A requested domain differing from a running proxy produces a
visible error; choose a separate tunnel if that different domain is intended.

The hub never owns the shared proxy PID or its tunnel PID. Stopping a hub
withdraws only its route; other sessions keep serving. The proxy exits after
60 seconds without live host records. If it dies, a hub watcher restarts it
on its next tunnel check. Local clients remain able to use their direct hub URL.

## Moving a session onto the proxy

A session on its own tunnel holds its reserved domain, which on a free ngrok
account is the one endpoint. Moving it is one command:

```bash
collab host --resume <session_id> --tunnel proxy --primary --keep
```

It does the cutover in order: it records the new mode with the session (so a
hub revived meanwhile comes up attached), stops the session's hub, listener
and own ngrok by their recorded stamps, waits — up to 20 s — until the hub and
its ngrok have exited, and only then starts the new hub, whose proxy takes the
same domain. If they have not exited by then it stops and says so rather than
starting a proxy ngrok would refuse (ERR_NGROK_334). Like any `host --resume`
it mints a new invite; guests already in the session keep their tokens, and the
bare-domain address they hold reaches the primary. The proxy never seizes or
kills a tunnel some other session holds: a domain in use there produces the
provider error and leaves the hub available locally.
A reserved domain preserves remote URLs across restarts; an unreserved address
may change and must be re-shared. Use `collab url` to inspect the current link.

Proxy state and process logs live in `proxy/` under the machine peer registry;
the tunnel log is `proxy-ngrok.log` in the registry root. `proxy/proxy.log` is
capped like `hub.log`, at 10 MB with its rotation, while the proxy runs. Startup
errors are also stored in the host session for its normal tunnel diagnostic.
The proxy owns its tunnel supervision; a hub merely observes the shared address.
