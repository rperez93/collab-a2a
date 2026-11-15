# Strong approval API v1 — contract proposal

Status: **Windows Hello approval is live from 2.2.0; passkey approval after 2.2.0.**
Capability discovery returns `available:true` with `methods:["hello","passkey"]`
(2.2.0 itself advertised `["hello"]` only). Off Windows, `collab consent enrol`
enrols a passkey through the local enrolment page; on Windows and WSL the client
always uses Hello. A hub older than 2.2.0 returns `available:false` with
`reason:"operation_integration_pending"`, or 404; a client treats both as
unavailable. Never fall back to terminal approval when strong approval was
requested.

### Implementation checkpoint (not production readiness)

`server/approval_crypto.py` now verifies Hello and passkey assertion signatures,
and `server/approval_challenge.py` builds bounded canonical review bytes and
fresh contextual challenges. `server/approval_store.py` now adds an internal
SQLite lifecycle sharing the hub participant database and write lock. It
revalidates the bearer in each operation transaction, reserves first-key
enrollment, verifies possession, freezes the initial key and applies a 600-second
cooldown. It persists immutable operation reviews, single-use challenges,
approvals and opaque hashed receipts. Receipt consumption claims dispatch once;
it does not execute an operation or promise exactly-once external effects.

`server/approval_keys.py` adds internal signed key addition, rotation and
host-authorized recovery. New keys prove possession over a dedicated
`key-possession` challenge, then a separate immutable request needs approval
from a mature existing key. Addition and rotation use the owner's key;
recovery requires every owner key to be inactive and a different, unique active
host with a mature key. Losing the only host key fails closed. Bearer access
cannot install or unfreeze a replacement. Recovery retires previous keys;
rotation retires the selected key. New keys retain the 600-second cooldown.

The HOST has no other host to recover through, so a key an agent enrolled for
the host first could otherwise never be undone. `reset_host_credentials()` is
the hub machine's own reset, internal API only (there is no `collab` command
for it): it takes no bearer token and is on no HTTP route;
only code holding the session database on the hub machine can call it. It
retires every host key, declines the host's pending approvals, drops its
challenges and re-opens first enrolment — under the same cooldown — and raises
an `approval-security` alarm (`action: reset`) to the session. A same-UID agent
on the hub machine could run it too; the alarm is what surfaces that, as with
any TOFU enrolment.

Key changes bind the owner generation, new fingerprint, credential identifier,
algorithm, replacement identifier and authorizer to the canonical review.
Their receipts cannot enter generic operation dispatch. Applying a change
revalidates current host authority where needed, consumes the receipt and
advances the owner generation atomically, invalidating older challenges and
receipts. Two SQLite connections cannot apply the same change twice. Registry
bounds are four active keys and 64 retained keys per owner (retired keys count);
pending change storage is capped at 128 globally and eight per owner, with
600-second expiry. Capacity exhaustion fails closed rather than dropping old
fingerprints and permitting silent reuse.

Bounded HTTP routes, durable security alarms and a server-side peer-skill
acceptance adapter are wired into the hub. Frontend adoption, local execution
policy, other operation-specific integrations, adapter wiring and real human
ceremonies remain outstanding. The capability endpoint does not advertise
availability based on these components alone.

The internal store refreshes the clock after SQLite writer admission and after
signature verification. It rejects expired receipts, replays, name substitution,
revoked participants and changed/missing key reports. Reports can freeze a key,
never overwrite it or prove a human gesture. Re-enrollment cannot silently
replace an existing or frozen key. Returning local public-key material to its
old value does not unfreeze it.

Current storage policy: 1024 outstanding request rows globally, 64 matching
requester/target rows, 1024 challenges, and 30 admitted mutations per participant
per 60-second window. Expired requests and expired/used challenges are pruned
when issuing new work. Failed proofs consume quota. Read failures and rejected
over-quota calls do not append audit records. Audit retains the latest 1000
events globally, filtering reads to the authenticated owner; it is bounded
rolling history, not append-only archival or guaranteed per-owner retention.
Audit metadata contains no proof, bearer, receipt or review payload. HTTP bodies
are streamed with a 128 KiB bound and five-second read deadline. Eight requests
may be in flight per serving app, with immediate busy rejection beyond that.
Cancellation retains its admission slot until its worker thread has actually
finished; a disconnected caller must treat a mutation outcome as uncertain.
An additional per-app ingress limit of 120 requests per participant per minute
includes malformed bodies and reads; the durable mutation quota remains
separate. JSON rejects duplicate fields, nonfinite literals, more than 2048
nodes and depth above 16; write schemas reject unknown fields and wrong types.
Responses use `Cache-Control: no-store`. There is no public generic request
creation or receipt consumption route: adopting operations must authorize and
dispatch through their own execution boundary.

Enrollment, freezing and applied key changes queue a bounded security alarm
inside the authority transaction. Publication transfers the alarm atomically
to the durable system-event log before ordered fan-out. Startup and bounded
periodic retries drain the backlog; a delivery failure closes existing feeds
so they replay before receiving later sequences. Alarms identify stable
participant IDs, action, fingerprint and cooldown, never proofs or payloads.
They remain explicit about TOFU and reports that are not verified gestures.
The outbox holds at most 256 pending alarms, publishes batches of at most 16,
and retries every second. A transition that cannot enqueue its alarm returns
429 and does not commit; the caller must retry after delivery recovers.

The current verifier profile requires RSA-2048/e=65537 for RS256 or P-256 for
ES256. V1 requests no authenticator extensions and rejects assertions carrying
AT/ED data rather than accepting unparsed CBOR. It requires a top-level local
browser origin, UP and UV, and preserves the original signed client bytes.
The origin has an explicit non-default port; browser default-port aliases are
not accepted. Counterless credentials (0 to 0) are supported; otherwise counters
must increase. Stored backup eligibility must not change. Counter comparison
and persistence occur atomically with challenge consumption in the internal
state machine. A failed counter check is not proof of cloning.

Review JSON is limited to 64 KiB of canonical bytes, depth 16 and 2048 nodes.
Strings cannot contain unpaired surrogates; integers must fit the exact JSON
integer range ±(2^53−1), and floats must be finite. Challenges preserve opaque
credential IDs: native Hello key names need not be base64url; binary passkey
IDs are validated by their enrollment path. This does not change the binary
wire fields' canonical unpadded base64url requirement.

Primary references: [WebAuthn assertion verification](https://www.w3.org/TR/webauthn-3/#sctn-verifying-assertion)
and [cryptography RSA signing/verification](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/).

Core owner: Arma; Hello/front-door owner: Jarvis; passkey adapter owner: Friday.
Source design: shared artifact `f_bcb2c024e2892f9067f1`, SHA-256
`41abc56ea8bda4ea24214ab9ac6c6b57f83e2532a6ba00f7c2a297abc63eb9d9`.

## Transport and representation

Prefix: `/ext/collab/v1/approval`. Existing participant authentication
is required on every route. Session and participant identity come from that
authentication, never from a supplied display name. The hub checks target
ownership for each request, challenge and credential lookup. Bearer possession
alone cannot approve a strong request or replace an enrolled key.

The authorization principal is `(session_id, participant_id)` from authenticated
hub state. Names are labels only. A same-ID rename preserves authority; a
reused name with a new ID or a different session grants none. Key, request,
challenge, receipt, rate-limit and audit ownership use this pair, with atomic
identity revalidation at approval and dispatch. Missing or ambiguous identity
fails closed; no legacy name fallback or automatic credential migration.

JSON responses carry `api_version: 1`. Unknown fields are rejected in writes.
Binary fields use **unpadded canonical base64url**: ASCII A–Z/a–z/0–9/`-`/`_`,
no whitespace, `=`, standard-base64 alphabet or alternate encoding. Decode with
field-specific limits and require encoding the decoded bytes to reproduce the
input exactly. `digest` and key fingerprints are lowercase SHA-256 hex.

Request and challenge IDs are opaque, case-sensitive hub-issued strings;
adapters must not reconstruct them or interchange them with credential IDs or
nonces. `credential_id` is the immutable provider's registered-key handle, not
a fixed session/participant naming formula. Hello key names must match
`[A-Za-z0-9._-]{1,64}`; a valid `collab-<session>-<participant>` name is merely
one possible convention, and adding/rotating keys needs fresh handles. Passkey
IDs encode the authenticator's credential ID as canonical unpadded base64url.
Only the initial enrolment challenge has a null credential ID before the new
key is known. `enrol/finish` requires the ID, SPKI and possession proof for
both methods; Hello has no exemption from proof or ownership. Expiry is
an integer Unix-second instant. The hub clock is authoritative. Challenges
expire at `now >= expires_at`, 120 seconds after issuance, and are single-use.
For an approval, fetch the challenge only after the human explicitly chooses
Approve in the owned review window. Time spent reading the review must not
consume the authenticator's challenge budget. This does not extend the stored
request's expiry: a request that expired while being reviewed must be prepared
and reviewed again. Enrollment and key-possession have their own separate
ceremonies and do not constitute approval of an operation.

Proposed limits: 64 KiB canonical review, 128 KiB request body, 4 KiB DER SPKI,
4 KiB decoded signature, 8 KiB decoded client data, 8 KiB decoded authenticator
data, 1 KiB decoded credential ID. Attestation is not accepted in this first
wire proposal; a client boolean must never establish attestation. A later
attestation implementation needs its own bounded format and trust policy.

## Routes and schemas

The following routes are implemented in the draft; production readiness is
reported separately by capability discovery.

* `GET /capabilities` returns `{api_version:1, available:true,
  methods:["hello","passkey"], supported_methods:["hello","passkey"],
  algorithms:{hello:[-257],passkey:[-7,-257]}, attestation_verification:false}`
  after 2.2.0 (2.2.0 itself returned `methods:["hello"]`). An older hub reports `available:false` with
  `reason:"operation_integration_pending"`; its 404 also means unavailable.
* `GET /keys` returns the authenticated participant's credential metadata:
  `credential_id`, `method`, `algorithm`, `fingerprint`, `created_at`,
  `trust` (`tofu-unverified` initially), `usable_at` and `state`, wrapped in
  `{api_version,credentials:[...]}`. It never exposes a
  private key or silently substitutes a credential for a missing one.
* `POST /enrol/begin` accepts `{method, origin?}`. `origin` is mandatory for
  passkeys. It returns `{api_version, challenge_id, challenge, expires_at,
  method, rp_id?, origin?, algorithms, bootstrap:"tofu-unverified"}`.
  A first-key ceremony reserves the enrolment generation. Concurrent first-key
  finishes cannot both succeed. Existing-key enrolment is refused here; use
  the separate signed key-change protocol, never an overwrite.
* `POST /enrol/finish` accepts `{challenge_id, credential_id, algorithm,
  public_key_spki, proof}`. `public_key_spki` contains DER SPKI in base64url;
  `algorithm` is the COSE integer, not a caller-selected verifier name. The hub
  checks key type/parameters, algorithm consistency and proof of possession.
  The response is `{api_version, credential:{...}}`. TOFU enrolment
  is announced loudly and never labelled attested.
* `GET /requests/{request_id}/review` returns `{api_version, request_id,
  review, canonical_review, digest, state, required_strength}`. The hub builds
  `review` from its stored operation: subject, stable requester/target IDs,
  exact destination, authority and payload. `canonical_review` is base64url of
  the UTF-8 bytes the hub hashes. The UI displays the full decoded review as
  untrusted text. Reading it grants no dispatch authority; payload secrecy
  from the reviewing same-UID agent is not claimed.
  The authenticated target **or original requester**, identified by immutable
  participant ID, may read this exact canonical review while both remain
  active. The requester hashes `canonical_review` bytes returned by the hub;
  it must not reconstruct a subset from skill/args/IDs because the server also
  binds authority, destination, source fingerprint and operation semantics.
  This read permission grants no challenge, signing, decline or receipt right.
* `POST /requests/{request_id}/challenge` accepts `{credential_id, origin?}`
  and returns `{api_version, request_id, challenge_id, credential_id, method,
  challenge, expires_at, digest, rp_id?, origin?}`. The request is frozen to
  the reviewed operation; a changed operation requires a new request/challenge.
  Before requesting a signature, the trusted dispatcher/adapter must compare
  the returned request ID, credential ID, method and digest with the exact
  request and enrolled credential selected for the displayed canonical review.
  For passkeys it must also match the owned localhost origin and `rp_id`.
  It must validate the challenge as canonical base64url of exactly 32 bytes,
  require an integer expiry and a nonempty challenge ID, and bound the gesture
  by the remaining challenge lifetime. A mismatch, expired challenge or failed
  fetch produces no signature, approval POST or weaker fallback. The hub still
  revalidates all authority and expiry; these local checks do not replace it.
* `POST /requests/{request_id}/approve` accepts `{challenge_id,
  credential_id, proof}`. The hub verifies and atomically consumes the challenge
  before issuing `{api_version, request_id, state:"approved", receipt}`.
  The opaque receipt is bound to the exact operation and consumed by the
  adopting execution path; it is not a general approval token.
* `POST /requests/{request_id}/decline` accepts `{}` and returns
  `{api_version, request_id, state:"declined"}`. It invalidates outstanding
  challenges atomically. An authenticated decline is a refusal, **not proof
  of a human gesture**; audit must not label it a verified-human decision.
* `POST /keys/report` accepts `{credential_id,observed_spki?}` and returns
  `{api_version,credential}`. Omitting the observation reports a missing key;
  a changed observation freezes the stored key, and neither installs a key.
* `POST /key-changes/begin` accepts `{kind,credential_id,method,algorithm,
  public_key_spki,replaced_credential_id?,origin?}`. Kinds are `add`, `rotate`
  and `recover`; rotation requires the replacement identifier. The response
  includes the new key's possession challenge, `change_id`, `authorizer_id`,
  full `review` and `canonical_review`.
* `POST /key-changes/{change_id}/prove` accepts `{challenge_id,proof}` and
  returns the authorizer's approval `request_id`, digest, expiry, change ID
  and authorizer ID. The existing request review/challenge/approve routes
  perform the separate authorization using an already enrolled mature key.
* `POST /key-changes/{change_id}/apply` accepts `{receipt}` from that
  authorizer and returns `{api_version,change_id,state:"applied",credential}`.
* `GET /key-changes/{change_id}` lets the owner inspect state, request ID,
  authorizer ID and expiry; it releases no receipt.
* `GET /audit` returns `{api_version,events:[...]}` filtered to the caller.

## Concrete peer-skill acceptance adapter

`POST /skills/prepare` accepts only `{source_seq}`. The authenticated receiver
must own the private source event, whose server-stamped sender must remain
active. It must be a chat with an exact `skill_request` object containing
`id` (`sr_` plus twelve lowercase hex digits), `skill`, full `args` (at most
4000 characters), and `required_strength:"strong"`. Legacy requests are not
silently upgraded. The source expires after 24 hours. SQL selects a bounded
prefix of the stored event before allocating its Python representation; an
event over 32 KiB is refused rather than truncated into a review.

The server constructs the canonical review itself from the stored event:
immutable requester/target IDs, exact source sequence and fingerprint, skill,
full arguments, destination and `skill-request:accept` authority. It does not
accept a caller-built review or an arbitrary destination. The response returns
the ordinary approval request ID/digest/expiry plus `source_seq`; the existing
review/challenge/approve routes then produce the strong receipt.

`POST /skills/{request_id}/accept` accepts `{receipt}`. It rechecks the original
source, participant authority, credential generation and receipt, then consumes
the receipt atomically with a durable private accepted event. It returns the
source sequence and event ID with `execution:"not_performed"`: this operation
records acceptance only. `GET /skills/{request_id}` exposes the owner's durable
acceptance state and event ID so a caller can reconcile a lost response without
executing again. It never releases the receipt. A source can be accepted once;
two concurrent connections cannot create two accepted events. An expired
receipt needs a fresh review/approval, and its old request cannot be reused.

The event is a hub-owned `system` event addressed by immutable IDs to the two
participants. It uses a distinct `strong_skill_response` field, **not** the
legacy `skill_response` field. A frontend must check the server-owned event
kind, both identities, source sequence and reviewed digest. The event carries
`canonical_review` (unpadded base64url of the reviewed bytes, which hold only the
requester's own request data), so the requester recomputes SHA-256 and checks
that the approved review names exactly the skill, arguments and source sequence
it sent; a mismatch is never acceptance. It must never trust
a peer chat that merely copies a `strength` label. The old first-wins request
parser cannot inherit acceptance for another message reusing the same short
request ID. Reusing a request ID under an existing source binding is refused.

Bindings are bounded to 4096 globally and 256 per receiver, retained until the
source expires; acceptance and request-expiry do not immediately erase replay
protection. Accepted events share the bounded durable approval-event outbox
with security alarms and preserve private entitlement in live and replay feeds.
The shared message persistence boundary now records explicit strong-request
policy atomically with each source. Both REST and A2A reject subsequent legacy
`accepted` responses and premature `done` responses for that routing pair and
request ID, and refuse
client attempts to mint `strong_skill_response` or `event:"skill-approval"`.
Declining still needs only authenticated authority; ordinary weak requests
retain their existing behavior. A strong request ID with a prior weak
acceptance or completion is refused, preventing an acceptance sent in advance
from being inherited by the old reader. Concurrent source/response publication
cannot commit both: one transaction must refuse.

After the exact latest source has a durable accepted binding and its strong
receipt has been consumed, its authenticated receiver may send a private chat
`skill_response` with state `done`. This is an execution report, not a human
approval or independently verified execution. It does not replace the separate
server-owned acceptance event. A later source reusing the short request ID
cannot inherit this permission; a report sent before strong acceptance remains
refused. Because legacy clients key completion by short ID, reporting is also
refused if another same-ID source exists among the pair's latest 500 chat
events (including weak requests), or that bounded history cannot be parsed.
Clients must continue checking the exact source's server-owned strong
acceptance before displaying a strong request as completed.

The policy registry is bounded to 4096 entries. It preserves each routing
pair/ID while its newest strong source remains in the event history, even
past approval expiry; pruning the source makes its policy row reclaimable.
This global cap is a storage bound, not per-peer flood protection. Draft-state
compatibility checks inspect at most 500 indexed pair events, matching the old
reader's maximum history window, with bounded SQL payload prefixes. The pair
indexes are installed after identity migration so old databases remain valid.
Frontend consumption, native Windows Hello signing and the human ceremonies are
live from 2.2.0, and passkey enrolment and approval after it; local execution
policy for remote prompts remains with #118's adoption. Collab does not execute a skill
as a side effect of this API.

## Proof bytes and origin binding

`proof` is a tagged union. Hello: `{type:"hello", signature}`. Passkey:
`{type:"webauthn.get", client_data_json, authenticator_data, signature}`.
The adapter forwards original byte arrays encoded once; it must not parse and
reserialize client data before forwarding or assign receipt strength itself.

The hub constructs the challenge as SHA-256 of canonical JSON containing
`api_version`, `purpose` (`enrol`, `key-possession` or `approve`), authenticated `session_id`,
`target_id`, `request_id` (null for enrol), `subject`, `requester_id`, `digest`,
`credential_id` (null for first enrol), fresh 32-byte `nonce`, `origin` and
`expires_at`. Null fields are explicit. Canonical JSON uses sorted keys,
compact separators, ASCII escaping and no nonfinite numbers. Only hub-issued
bytes are signed; clients do not reconstruct this envelope.

Hello signs the decoded 32 challenge bytes using RSA-2048 PKCS#1 v1.5/SHA-256.
Passkey authentication uses those bytes as the WebAuthn challenge, rpId
`localhost`, user verification required, and the selected credential ID.
The hub checks signed client-data type/challenge, exact stored origin,
rpIdHash, UP/UV, algorithm, signature and credential counter policy. The origin
must be an exact canonical `http://localhost:<port>` or
`https://localhost:<port>` origin, without path, credentials, query or fragment;
the local adapter binds its actual port before starting the ceremony. Origin
validation is not authentication of a local process.

**Enrolment possession choice (decision `e_867b714f9cd24a8e96c89320a804b647`):** `create()` plus
`getPublicKey()` alone does not prove possession of that supplied SPKI without
verifying attestation. This proposal uses a subsequent `get()` assertion by
the new credential over the enrolment challenge, verified against the supplied
SPKI. That can require another gesture. The create ceremony is not itself the
hub-accepted proof. No fake or software-key fixture establishes real human
approval; unverified TOFU remains explicitly unverified even after proof of
possession. The follow-up assertion is required as possession proof only, never attestation.
The possible second gesture is accepted for this flow. This settles only that
protocol choice; endpoint implementation and the rest of the contract remain
unverified and must pass their own gates.

## Errors, receipts and remaining protocol work

Errors have `{api_version:1,error:{code,message}}` with fixed, content-free
messages: 400 malformed/unsupported proof, 401 authentication required,
403 wrong participant/origin/verification failed, 404 unknown owned resource,
409 already consumed/changed/frozen key, 410 expired challenge,
408 body deadline, 413 oversized input, 415 unsupported body media/encoding,
429 bounded rate/capacity limit and 503 busy admission. No error triggers weak fallback.
Invalid proofs issue no receipt. Successful replay cannot execute twice.

Hub-owned receipt/event/audit metadata separates `strength` (`strong` or
`weak`), `method`, `credential_trust`, fingerprint and reviewed digest.
Strong means verified proof under the accepted enrolled-key policy; it does
not turn TOFU into attestation or prove software keys impossible. Terminal
codes are weak and cannot satisfy `required_strength:"strong"`.

Before declaring v1 ready, connect an operation-specific authorized request
creator and receipt consumer, integrate and test both production adapters,
and verify actual dispatch behavior, including uncertain outcomes after a
claimed receipt. The internal and transport tests do not prove those gates.
Publish the exact source SHA and contract evidence for adapter review.
Final real enrollment/signing remains a human gate: synthetic software-key
proofs and a live loopback HTTP server are transport evidence, not human or
hardware-authenticator acceptance.
