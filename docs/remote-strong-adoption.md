# R7 adoption of R22b strong approval

Status: implementation contract, not an implemented authorization path. PR118
at 045605b uses local terminal receipts. Those receipts do not satisfy R22b.
The reviewed reference is PR172 c4f810d (core) and PR173 d90bf86 (frontend).
The remote-prompt and connection-goal operations remain release gates.

## Preserve the R7 transport

Keep authenticated sender and recipient IDs, immutable offer bytes and digest,
expiry, per-sender queue/rate bounds, receiver-selected local destination,
content-free audits, and exclusion from ordinary worker/wake history and replay.
A connection goal must retain its separately authenticated issuer; joining never
accepts it. Do not convert an offer into an ordinary chat or skill request to
reuse an acceptance implementation.

Current main 2fa3a3a adds worker files/comments, recipient-ID routing, main-sent
context, and security-alarm attention. A mechanical replay of the original R7
commit conflicts with those paths. In particular, adding `recipient_id` must not
replace `to_id`, reset an ordinary message route inside acknowledge(), lose
file/comment delivery, or restore an acknowledgement after permanent rejection.
The worker-off manual flush must use the same remote-only route, without an
ordinary-send fallback. Preserve security-alarm relevance when filtering offers.

## Prepare the reviewed operation

Add an operation-specific remote approval adapter beside approval_skills; do
not expose the generic internal create_request/consume methods as arbitrary
bearer-accessible HTTP endpoints. Preparation authenticates the receiver and
loads the exact private source from the hub's event store using its sequence.
The adapter validates the R7 offer shape, sender/recipient/session IDs, original
expiry, source fingerprint and supported prompt/goal kind before preparing it.
For connection goals, persist an equivalent immutable recipient-specific source
with its authenticated issuer before it can be prepared.

The receiver supplies its selected delivery descriptor: adapter, target session
or disclosed tmux pane scope, exact argv and resolved cwd. The hub cannot attest
to local configuration; the receiver must independently compare it before review
and again before dispatch. A peer never supplies executable argv/env/cwd.

Canonical review binds all of:

- Collab session, immutable requester and receiver IDs;
- source sequence, offer ID, source fingerprint, full prompt/goal and expiry;
- exact local target, adapter/scope, argv, cwd and binding digest;
- authority text explaining existing coding-session permissions and absence of
  an additional sandbox;
- a distinct operation kind, `remote-deliver`, never `skill-accept`.

Use R22b canonical serialization and lowercase SHA256 hex. Reject over-bound
canonical reviews rather than truncating the prompt or destination. The local
6000-byte prompt and 32768-byte argv bounds do not by themselves guarantee the
combined review fits the core's 64KiB bound. Retain the narrower original expiry
when applying the core's request lifetime. Preparation must not execute or issue
a challenge. The target fetches the challenge only after the human clicks in the
existing Hello/passkey window, forwarding the original bounded proof object.

## Consume and dispatch

The target-only consume endpoint reauthenticates, reloads the source, checks its
fingerprint/expiry and consumes the receipt against the exact review digest in
one transaction. It records an immutable remote-consumption identity and returns
canonical reviewed bytes. It never executes the command or broadcasts the local
argv/cwd. The requester may receive a bounded delivery status, not the receiver's
private local configuration. Do not copy the skill adapter's canonical-review
broadcast: remote reviews contain receiver-local details the sender did not own.

The receiver verifies returned canonical bytes against the locally reviewed
operation before transitioning its inbox. Local authorization version must advance
past version2: old accepted terminal-receipt rows become non-dispatchable and need
fresh review. Merely editing a state label or receiving an accepted chat cannot
create a grant. Preserve recorded dispatching/uncertain tombstones.

There is no atomic transaction spanning the hub, the local inbox and the coding
agent. The supported claim is at-most-once dispatch attempt, not exactly-once
execution. Persist a local non-replayable attempt before consuming/dispatching.
Timeout, cancellation, process death or lost consume response must never trigger
a second automatic consume or execution. A status query can report durable state;
it cannot prove a previous execution did not happen or authorize replay. A new
human-reviewed request is needed for an ambiguous attempt.

Recheck the selected adapter identity and local binding immediately before the
attempt. If changed, refuse and require review again. Failure writing the audit or
inbox must not launch the coding adapter. A successful adapter exit reports only
submission, not successful completion of the requested task.

## Acceptance evidence required

Exercise the real HTTP adapter and SQLite state with synthetic signing keys and
a stub addressed coding adapter, separately from actual native human validation:

- wrong source, issuer, recipient, digest, local binding or operation kind refused;
- receipt reuse, expiry, retired key and changed credential generation refused;
- unapproved offers absent from models, ordinary wake, history/backfill and old clients;
- old terminal receipts and accepted version2 rows never dispatch after migration;
- one successful reviewed prompt and one authenticated connection goal reach only
  their exact bound target, with no model invocation in automated tests;
- interruption at every hub/local/adapter boundary produces no automatic replay;
- private receiver argv/cwd never appear in requester events or audit details;
- normal worker messages/files/comments, manual outbox flush and alarm attention
  retain current-main behavior.

Then run the lead's serialized landing suite and actual same-version hub/client
human ceremonies. Keep capabilities unavailable until the concrete operation and
installed adapter are verified. This document is not evidence of those gates.
