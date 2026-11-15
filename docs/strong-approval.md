# Local passkey review frontend

`collab.client.passkey` provides a localhost review page. The bounded
`PasskeyHubAdapter` forwards the draft v1 hub protocol using an authenticated
profile, an immutable operation binding and an explicit credential ID.
Request windows cannot enrol or replace a key: neither the page nor its HTTP
capability exposes registration. The separate human-started service is
`collab.client.passkey_enrol.EnrolService`; CLI and dispatcher adoption remain
core integration gates. This module does not provide a hub
approval API, register a real credential by itself, or turn a terminal receipt
into cryptographic approval. With no adapter it reports «unsupported» and
submits no decision. The authenticated adapter checks capability discovery: the current
draft core reports «operation_integration_pending» and refuses production
availability. No local flag overrides it. Real human enrolment and approval
remain integration gates.

The asynchronous adapter entrypoint awaits `passkey.open(request_id, adapter=adapter)` on its existing
daemon event loop, then calls `passkey.close()` during shutdown. The adapter
must stay on that loop. The module neither launches Windows Hello nor chooses
a fallback. The dispatch contract sends Windows and WSL to native Hello;
this page must not become an automatic fallback there. Dispatcher wiring is
still pending. A trusted browser opener runs in a thread; its await has a three
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
Rendering never interprets embedded request markup. The requester is an explicit
`Requester(participant_id, display_name)` with its hub ID displayed first and its name marked «Calls itself: … (self-chosen)». The
adapter obtains the participant ID from the canonical hub review. The dispatcher
may supply a display name from the authenticated source event; it remains an
untrusted label and does not affect authority or receipt binding. Missing labels
stay «Name unavailable», rather than being guessed. Approval requires a button click and user
verification. No request-window code calls `navigator.credentials.create`.
The separate enrolment flow uses the participant ID's exact UTF-8 bytes as the
WebAuthn user ID, refusing identities larger than 64 bytes. It requests
attestation «none» and only ES256 or RS256. Creation exports the credential ID,
algorithm and bounded SPKI. A subsequent get proves possession using that same
credential and the original hub challenge. Creation attestation is not
forwarded or treated as proof. The adapter forwards original clientDataJSON,
authenticator data and signature bytes without reconstruction. The hub must independently verify
the challenge, origin, RP hash, UV flags, signature, counters, nonce and review
binding before consuming a decision.

The core must refuse approvals using a key enrolled less than ten minutes ago,
allowing time for the loud enrolment announcement and human intervention. The
draft core implements this cooldown; combined integration remains a gate. The
page displays the hub's usable-at metadata. Client readiness and maximum future
challenge expiry allow thirty seconds of clock skew; expired challenges remain
refused and authenticator runtime remains capped at 120 seconds. The hub alone
enforces credential cooldown and authoritative expiry. Acceptance must include attempted virtual-authenticator enrolment
using a stolen request capability and a real human-approved path.

First enrolment is trust on first use. Software and virtual authenticators can
claim user verification; the page does not equate this with hardware, human
presence or verified attestation. Existing-key replacement requires the core's
prior-key or host-authorized strong recovery path and enrolment alarms.

After forwarding begins, cancellation, timeout or an unknown outcome leaves
the window uncertain. It cannot mint another decision until an explicit review
refresh confirms the current hub state. Local expiry is checked again after
body collection, and ceremonies cannot outlive the review or local capability.
The hub remains responsible for authoritative expiry and single consumption.

The request adapter needs a trusted dispatcher binding from an authenticated
operation preparation: session, target and requester IDs, request ID, digest,
expiry, destination, authority and source sequence where applicable. A name or
browser-supplied request ID cannot create that binding. The review endpoint
does not admit bindings itself. The dispatcher uses `register_binding(binding)`
on its existing adapter for later operations: at most sixteen states are
retained, expired unlocked states may be pruned, and a conflicting existing
request binding is refused. The review endpoint
does not supply expiry. If a challenge outlives its request, the adapter caps
only the local ceremony deadline and preserves the hub's original challenge
bytes. A changed route, token or certificate invalidates transport work before
another request is sent. A successful proof releases an opaque receipt only
through a private one-use dispatcher handoff, never to the browser; taking it
is not execution of the operation.

The synchronous dispatcher entrypoint is
`passkey.approve_with_passkey(request, fetch_challenge)`. Its trusted caller
owns authenticated readiness, preparation, credential selection and route
policy before opening the window. The request requires the hub's original
`canonical_review` bytes encoded as base64url and their lowercase hex digest,
`expires_at` from the authenticated preparation, request and credential IDs,
requester ID/name, subject, destination and authority. The page verifies the
original bytes against the digest and displayed fields; it never reconstructs
canonical bytes from the pretty review text. Optional caller-supplied session,
target and source-sequence bindings must also match the canonical operation.

Only an explicit Approve click calls `fetch_challenge(origin=page_origin)`.
The origin is the already listening localhost page; the callback owns bounded
authenticated challenge I/O. The page checks the challenge's credential,
request, digest, origin, relying party, 32-byte size and expiry. It returns the
original tagged assertion privately as `{choice: "approve", challenge_id,
credential_id, proof}`. The browser receives «proof-ready – signed, waiting for
the hub's confirmation», never a receipt or an approved result. This mode has
no hub approval transport: the dispatcher alone posts `/approve`, confirms its
result and consumes any receipt through the adopting execution path. Decline
returns a choice for the dispatcher; local Cancel and unavailable/expired
flows submit no hub decision. Actual human enrolment and final dispatcher
adoption remain separate gates.

If the synchronous collector rejects an assertion submitted through its
authorized page for the current ceremony, it ends the local flow immediately
with an unavailable choice and a bounded reason for its
trusted caller. It clears the ceremony and accepts no replacement proof. This
local refusal does not wait for the review deadline, send a hub decision or
claim that a previously forwarded hub operation failed. The authenticated
asynchronous adapter keeps its separate uncertain-forwarding rules.
An exact current ceremony that has expired ends with an expired choice,
rather than leaving the caller waiting for the longer review deadline.
Unrelated requests with a wrong capability, origin or ceremony cannot end
another local review.

The synchronous mode admits one active foreground flow per process. Its
callback/opening threads have a separate two-slot limit; a stuck callback
retains its slot until it exits. Timeouts revoke local authority and stop the
owned listener, while a late callback cannot return a proof. An opted-in
Cancel can also stop a pending challenge callback without changing ordinary
asynchronous adapter cancellation semantics. These are bounded local resource
controls, not a claim to kill a hung Python callback thread.

`EnrolService(page, transport).human_start()` is the local front-door API. It
mounts separate enrolment routes under the existing guarded listener, with one
independent capability and flow alongside at most sixteen request windows.
The eight-connection cap is shared. No HTTP endpoint or request event mints an
enrolment capability. Same origin is not isolation between pages: distinct
capabilities, text-only rendering and the restrictive content policy remain
essential. Stopping the page revokes the capability and retains a bounded
marker for an uncertain finish. Refresh confirms only the exact credential ID,
algorithm and SPKI fingerprint in the authenticated registry; empty or
mismatching keys never authorize repeating an uncertain finish or replacing a
key. A cancelled create/get step can leave a credential on the authenticator
without hub enrolment; the UI promises neither deletion nor rollback.

The transport caps request bodies at 128 KiB and raw responses at 256 KiB,
rejects encoded responses and bounds JSON depth, node count and field sizes.
Signed client data is limited to 8 KiB, authenticator data to 8 KiB, signatures
and SPKI to 4 KiB each, and credential IDs to 1 KiB. A five-second budget covers
each multi-request adapter operation. Enrolment handling has a ten-second
whole-operation deadline including its three-second body budget. These are
component limits, not a final process-tree CPU/RSS guarantee.

Component tests use synthetic adapters and browser API stubs. Socket resource
tests measure owned flooding, silent and trickling peers in fresh processes;
they do not certify final connected-host R17 budgets or real WebAuthn approval.
