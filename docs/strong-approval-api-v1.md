# Strong approval API v1 — contract proposal

Status: **proposed, not implemented or accepted**. This document is an interface
for review by the core, Hello and passkey owners. It is not evidence that an
endpoint exists. Clients must remain visibly unavailable until the capability
endpoint and the corresponding implementation tests exist. Do not fall back to
terminal approval when strong approval was requested.

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

API routes, event publication, operation-specific authorization/adoption, adapter wiring and real
human ceremonies remain outstanding. The capability endpoint must not advertise
availability based on these internal components alone.

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
Audit metadata contains no proof, bearer, receipt or review payload. Network
request limits and loud alarm delivery still belong to the unimplemented API.

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

Proposed prefix: `/ext/collab/v1/approval`. Existing participant authentication
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

IDs are opaque, case-sensitive hub-issued strings; adapters must not derive or
interchange `request_id`, `challenge_id`, `credential_id`, or `nonce`. Expiry is
an integer Unix-second instant. The hub clock is authoritative. Challenges
expire at `now >= expires_at`, 120 seconds after issuance, and are single-use.

Proposed limits: 64 KiB canonical review, 128 KiB request body, 4 KiB DER SPKI,
4 KiB decoded signature, 8 KiB decoded client data, 8 KiB decoded authenticator
data, 1 KiB decoded credential ID. Attestation is not accepted in this first
wire proposal; a client boolean must never establish attestation. A later
attestation implementation needs its own bounded format and trust policy.

## Routes and schemas

All route names below are proposals to freeze before implementation.

* `GET /capabilities` returns `{api_version:1, methods:["hello","passkey"],
  algorithms:{hello:[-257],passkey:[-7,-257]}, attestation_verification:false}`
  only when the implementation is available. An old hub's 404 means unavailable.
* `GET /keys` returns the authenticated participant's credential metadata:
  `credential_id`, `method`, `algorithm`, `fingerprint`, `created_at`,
  `trust` (`tofu-unverified` initially), `state` and alarms. It never exposes a
  private key or silently substitutes a credential for a missing one.
* `POST /enrol/begin` accepts `{method, origin?}`. `origin` is mandatory for
  passkeys. It returns `{api_version, challenge_id, challenge, expires_at,
  method, rp_id?, origin?, algorithms, bootstrap:"tofu-unverified"}`.
  A first-key ceremony reserves the enrolment generation. Concurrent first-key
  finishes cannot both succeed. Existing-key enrolment is refused here pending
  the separately reviewed signed rotation protocol; no overwrite path exists.
* `POST /enrol/finish` accepts `{challenge_id, credential_id, algorithm,
  public_key_spki, proof}`. `public_key_spki` contains DER SPKI in base64url;
  `algorithm` is the COSE integer, not a caller-selected verifier name. The hub
  checks key type/parameters, algorithm consistency and proof of possession.
  The response is `{api_version, credential:{...}, alarm_id}`. TOFU enrolment
  is announced loudly and never labelled attested.
* `GET /requests/{request_id}/review` returns `{api_version, request_id,
  review, canonical_review, digest, state, required_strength}`. The hub builds
  `review` from its stored operation: subject, stable requester/target IDs,
  exact destination, authority and payload. `canonical_review` is base64url of
  the UTF-8 bytes the hub hashes. The UI displays the full decoded review as
  untrusted text. Reading it grants no dispatch authority; payload secrecy
  from the reviewing same-UID agent is not claimed.
* `POST /requests/{request_id}/challenge` accepts `{credential_id, origin?}`
  and returns `{api_version, request_id, challenge_id, credential_id, method,
  challenge, expires_at, digest, rp_id?, origin?}`. The request is frozen to
  the reviewed operation; a changed operation requires a new request/challenge.
* `POST /requests/{request_id}/approve` accepts `{challenge_id,
  credential_id, proof}`. The hub verifies and atomically consumes the challenge
  before issuing `{api_version, request_id, state:"approved", receipt}`.
  The opaque receipt is bound to the exact operation and consumed by the
  adopting execution path; it is not a general approval token.
* `POST /requests/{request_id}/decline` accepts `{reason_code?}` and returns
  `{api_version, request_id, state:"declined"}`. It invalidates outstanding
  challenges atomically. An authenticated decline is a refusal, **not proof
  of a human gesture**; audit must not label it a verified-human decision.

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
413 oversized input, 429 bounded rate limit. No error triggers weak fallback.
Invalid proofs issue no receipt. Successful replay cannot execute twice.

Hub-owned receipt/event/audit metadata separates `strength` (`strong` or
`weak`), `method`, `credential_trust`, fingerprint and reviewed digest.
Strong means verified proof under the accepted enrolled-key policy; it does
not turn TOFU into attestation or prove software keys impossible. Terminal
codes are weak and cannot satisfy `required_strength:"strong"`.

Before declaring v1 ready, specify and test signed key addition/replacement,
host-strong recovery, missing-key alarm reporting, request creation/adoption,
receipt consumption/idempotence, bounded audit retention, rate limits and
counter rules (including authenticators with no counter). These are core
acceptance requirements, not implicit behavior supplied by this proposal.
Then publish the exact source SHA and contract tests; only afterward connect
the production adapters. Final real enrolment/signing remains a human gate.
