# Local passkey review frontend

`collab.client.passkey` provides a localhost review page and an injectable
adapter boundary for the strong-approval dispatcher. It does not provide a hub
approval API, register a real credential by itself, or turn a terminal receipt
into cryptographic approval. With no adapter it reports «unsupported» and
submits no decision. Wiring the final versioned hub contract and testing real
human enrolment and approval remain integration gates.

The dispatcher awaits `passkey.open(request_id, adapter=adapter)` on its existing
daemon event loop, then calls `passkey.close()` during shutdown. The adapter
must stay on that loop. The module neither launches Windows Hello nor chooses
a fallback. A trusted browser opener runs in a thread; its await has a three
second timeout, but Python cannot stop a stuck opener thread. A late launch can
show a refused page after its capability has been invalidated. This is a
best-effort desktop integration, not a subprocess cleanup guarantee.

The listener binds an ephemeral IPv4 loopback port and advertises
`http://localhost:<port>`. The hub must bind every ceremony to that exact
origin, including scheme and port, and to the exact canonical review digest.
The relying-party ID is `localhost`, independently of any tunnel hostname.
[WebAuthn](https://www.w3.org/TR/webauthn-3/) defines the signed origin and
relying-party checks; [Secure Contexts](https://www.w3.org/TR/secure-contexts/)
defines the potentially trustworthy localhost exception. Browser support must
still be checked on each supported connected host.

A random 256-bit request capability travels in the URL fragment, is removed
from browser history on load, and becomes a request header. Exact Host and
Origin checks, loopback peers, no-store responses, no referrer, a restrictive
content security policy, and bounded connection/body deadlines constrain the
local transport. No participant bearer is sent to the browser. These controls
do not isolate the page or its payload from other software running as the same
OS user; such software can redraw the page or read the request.

The review displays the complete bounded UTF-8 canonical bytes as text alongside
their SHA-256 digest, subject, requester, destination, authority and expiry.
Rendering never interprets embedded request markup. Each credential operation
requires a button click and requests user verification. Enrolment requires a
supported exported SPKI and preserves the browser's raw base64url evidence.
The adapter forwards original clientDataJSON, authenticator data, attestation
and signature bytes without reconstruction. The hub must independently verify
the challenge, origin, RP hash, UV flags, signature, counters, nonce and review
binding before consuming a decision.

First enrolment is trust on first use. Software and virtual authenticators can
claim user verification; the page does not equate this with hardware, human
presence or verified attestation. Existing-key replacement requires the core's
prior-key or host-authorized strong recovery path and enrolment alarms.

After forwarding begins, cancellation, timeout or an unknown outcome leaves
the window uncertain. It cannot mint another decision until an explicit review
refresh confirms the current hub state. Local expiry is checked again after
body collection, and ceremonies cannot outlive the review or local capability.
The hub remains responsible for authoritative expiry and single consumption.

Component tests use synthetic adapters and browser API stubs. Socket resource
tests measure owned flooding, silent and trickling peers in fresh processes;
they do not certify final connected-host R17 budgets or real WebAuthn approval.
