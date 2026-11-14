# One tunnel for multiple sessions

`collab host` uses a shared machine proxy by default. The public link is
`https://domain/s/<session_id>#<invite>`. The proxy preserves authentication,
streams events and file bodies, and routes only to local live host records.

The first primary session retains the bare-domain route used by older links.
The primary remains fixed when that session ends: the bare route returns 404,
never another session. Existing per-hub tunnels remain available:

```bash
collab host --tunnel own
collab host --tunnel proxy
collab config proxy off
collab host --no-tunnel
```

`auto` and `on` use the shared proxy; `off` uses a separate tunnel per hub.
An explicit host mode is saved for resume. Local-only hosting starts neither
kind of tunnel. A requested domain differing from a running proxy produces a
visible error; choose a separate tunnel if that different domain is intended.

The hub never owns the shared proxy PID or its tunnel PID. Stopping a hub
withdraws only its route; other sessions keep serving. The proxy exits after
60 seconds without live host records. If it dies, a hub watcher restarts it
on its next tunnel check. Local clients remain able to use their direct hub URL.

For migration from a per-hub tunnel, stop/restart that owning hub through the
normal coordinated session procedure so its recorded tunnel releases the domain.
The new shared proxy does not seize or kill an existing tunnel. A domain already
in use produces the provider error and leaves the hub available locally.
A reserved domain preserves remote URLs across restarts; an unreserved address
may change and must be re-shared. Use `collab url` to inspect the current link.

Proxy state and process logs live in `proxy/` under the machine peer registry;
the tunnel log is `proxy-ngrok.log` in the registry root. Startup
errors are also stored in the host session for its normal tunnel diagnostic.
The proxy owns its tunnel supervision; a hub merely observes the shared address.
