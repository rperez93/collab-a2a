# Local and public routes for one session

The host's HTTP listener and public tunnel continue serving the same session.
The host itself uses loopback. Another agent on the same machine can select
loopback from an unambiguous host record in the current user's private peer
registry. Machine/user labels alone do not authorize that choice: ownership,
directory privacy and record write permissions are checked too.
Discovery examines at most 1,024 directory entries before filtering file
names, with at most 16 KiB read from each eligible regular record. Shared or
writable registries remain discovery-only for automatic credential routing.

For automatic LAN selection, hosting also starts a TLS entrance to the
existing listener. OpenSSL generates a private per-session certificate/key
when needed. The entrance forwards bytes to the same hub; it creates no new
room, store or membership. If TLS setup is unavailable, the public route
remains usable and the host log explains the failure. An untrusted plaintext
LAN address is never silently substituted.

Public join lines keep the short legacy form
`collab join https://host#invite`. Routed public paths such as `/s/<id>` may
include a small `collab_session` query hint, with the full link quoted for the
shell. The invite stays in the `#fragment`, outside HTTP request lines.
Older query-bearing links remain accepted, but `collab_local` and `collab_cert`
never choose a destination or establish certificate authority.

The client submits the join once through the original visible address, unless
an unambiguous private same-machine record authorizes a loopback route.
When a session id is known, health must name that session before any invite
is submitted. An uncertain join POST is never retried at another address.
The CLI prints the selected route after joining.

Only the authenticated join response or a later authenticated roster can
supply the local TLS entrance and certificate for subsequent traffic. Secure
candidates use numeric loopback, RFC 1918 LAN or IPv6 ULA addresses; DNS and
arbitrary public destinations are excluded. Every local TLS connection trusts
only the supplied session certificate, without system CA roots. A wrong
certificate fails before bearer submission; relaying opaque TLS does not
reveal credentials. An explicitly supplied legacy HTTP address retains its
existing trust, while discovered plaintext LAN addresses are never adopted.

Local probes share one 750 ms budget. All public fallback probes share one
five-second budget, divided so the original URL still gets a chance after a
silent newly advertised address. Thus reconnect has at most 5.75 seconds of
network probe budgets, rather than a separate full deadline per candidate.
An expected-session health preflight on a hinted join has its own five-second
bound; ordinary short legacy links need no additional session-id health read.

Bounded `routes.json` metadata (at most 16 KiB) preserves the original user URL independently
of the selected URL. Reconnect prefers private same-machine discovery, then
pinned TLS, then a reachable public route, retaining participant id, token and
SSE cursor. Even a legacy profile without a route sidecar checks the expected
health session id before exposing its daemon bearer. Certificate changes close the old pool and restart the feed with
that cursor, including a new secure entrance learned while using the public
route. A host always recovers through its own HTTP listener, rather than
flipping its profile back to the public tunnel. Late responses from old URL/token/certificate generations cannot
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
These latency observations predate the original-first join change and measure
transport only. They do not establish cross-host acceptance of the current
integrated build.

The local WebSocket bridge admits at most 32 viewers and sends to them in
parallel with one-second send and close deadlines. When a route changes after
an event is recorded, its bounded local delivery completes before cancellation
advances to the next feed. The saved SSE cursor therefore cannot skip an event
that was committed but never forwarded to the viewer. A 32-silent-viewer test
used about 0.001 CPU seconds, 0.041 seconds wall time and 26 MiB peak RSS.
