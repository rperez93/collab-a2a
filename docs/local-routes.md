# Local and public routes for one session

The host's HTTP listener and public tunnel continue serving the same session.
The host itself uses loopback. Another agent on the same machine can select
loopback from an unambiguous host record in the current user's private peer
registry. Machine/user labels alone do not authorize that choice: ownership,
directory privacy and record write permissions are checked too.

For automatic LAN selection, hosting also starts a TLS entrance to the
existing listener. OpenSSL generates a private per-session certificate/key
when needed. The entrance forwards bytes to the same hub; it creates no new
room, store or membership. If TLS setup is unavailable, the public route
remains usable and the host log explains the failure. An untrusted plaintext
LAN address is never silently substituted.

Public join lines carry nonsecret query hints for the session, local HTTPS
address and public certificate. The invite stays in the `#fragment`, outside
HTTP request lines. The whole link is shell-quoted. Older clients discard the
query and still join through the original public address. Legacy
`http://host:port#invite` and per-session ngrok links remain accepted; local-only
join lines retain their existing spelling.

New clients probe local routes with a 750 ms total deadline. Every connection
trusts only the certificate supplied in the user-provided link or learned
through the current trusted hub connection. No system CA roots are added to
that local context. A wrong certificate fails before any invite or bearer is
sent; relaying opaque TLS does not reveal credentials. A discovered HTTP URL
never inherits the original user's authority. Hinted links check the expected
session before the join POST, and an uncertain POST is never retried elsewhere.

Bounded `routes.json` metadata preserves the original user URL independently
of the selected URL. Reconnect prefers private same-machine discovery, then
pinned TLS, then a reachable public route, retaining participant id, token and
SSE cursor. Certificate changes close the old pool and restart the feed with
that cursor. Late responses from old URL/token/certificate generations cannot
overwrite current routes or snapshots.

A surviving route can advertise a new tunnel URL; private local discovery can
find a moved local port. If every known address changes while a guest is
disconnected, recovery needs a stable public URL or rendezvous. Unknown
addresses cannot be inferred. Reserved domains provide that stable route;
central proxy routing is a separate change.

The TLS entrance admits at most 256 connections before handshaking, with a
three-second handshake and one-second upstream connection deadline. It reads
64 KiB chunks with write-side backpressure. A shared 60-second idle timer
allows SSE keepalives without limiting total stream lifetime. The resource
workload supports eight participants with up to eight pooled connections each;
remaining slots accommodate CLI traffic. This finite cap does not promise
unlimited room capacity. Excess sockets close so clients can fall back
publicly; shutdown reaps only this listener's connections and tasks.

In one active-session run, five reads each had median round trips of 4.033 ms
over loopback, 5.316 ms over pinned TLS, and 225.075 ms through the tunnel.
Closing only the temporary TLS entrance selected the original public route in
0.541 seconds with zero join POSTs and unchanged membership. Response body
bytes were 92,016 / 92,016 / 92,019; TLS changes the route, not the payload.
The test process peaked at 43,172 KiB RSS. A separate 400-silent-socket test
held the 256-connection cap, used 0.170 CPU seconds and peaked at 108,776 KiB
RSS. Unsafe certificate-file probes finished in about 2 ms and 22 MiB RSS.
These are measured scenarios, not latency or unlimited-capacity guarantees.
