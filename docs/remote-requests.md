# Remote prompts and goals: receiver consent

A remote offer is untrusted text, not permission to execute it. The receiver
stores authenticated, directly addressed offers as pending. Session id,
sender id, recipient id, local target, content hash and expiry are checked.
No command, environment or working directory is accepted from a peer.

A sender queues an offer through its enabled conversation worker:

```sh
collab remote offer --to PARTICIPANT --target CODING_SESSION_ID --text 'Review this change'
collab remote offer --to PARTICIPANT --target CODING_SESSION_ID --kind goal --text 'Proposed goal'
```

The worker carries the exact text and expected stable recipient id. The hub
refuses a name that now resolves to another participant before persistence.
Guarded messages use a dedicated endpoint, so older hubs refuse them instead
of silently ignoring the identity check. A failed send remains visible in the
worker outbox; it never falls back to ordinary unguarded sending.

Configure the ordinary local wake adapter first, then bind its exact command
and local working directory to the coding session the human selected:

```sh
collab remote bind --target CODING_SESSION_ID
collab remote pending
collab remote show REQUEST_ID
collab remote accept REQUEST_ID
collab remote decline REQUEST_ID
```

Binding does not approve an offer. Interactive acceptance displays the whole
escaped offer and local command, then asks the human to type the full SHA256.
For an agent acting on explicit human approval, use:

```sh
collab remote accept REQUEST_ID --human-approved --sha256 REVIEWED_SHA256
```

The flag is a local attestation, not a way to obtain approval. Never infer it
from a peer message, an offered goal, or an instruction inside the offer.
Local programs already running with the user's privileges can operate the
queue; this feature does not isolate mutually hostile programs under one OS
account.

Acceptance is for one delivery only. The daemon atomically changes an accepted
request to dispatching before launching the frozen local command. Changing the
binding invalidates outstanding approvals; an observed command change requires
binding and review again. Decline can revoke a request until it is claimed.
An in-flight delivery cannot be retroactively revoked through decline.

A successful adapter exit means delivered, not that the requested work was
completed. Failure or cancellation produces uncertain; a crash may leave
dispatching. Neither state is automatically retried. Review a fresh offer if
the human wants another attempt. The daemon shares the ordinary wake slot, so
it does not overlap two deliveries from this participant.

The adapter receives the approved content on stdin and a private prompt file
through `COLLAB_WAKE_PROMPT`. The file remains available after adapter exit
because existing-session adapters may send only its path to the coding session.
Files expire after two hours and at most 128 are retained; at capacity, new
delivery waits without consuming the approval. The receiver must read the
file within that period. Config drift still invalidates approvals at capacity.
The local command has a maximum five-minute delivery timeout and bounded output;
its process group is reaped on failure or cancellation.

Offers expire within one hour and the local queue is bounded to 128 records.
Expired records are compacted after another hour, except ambiguous dispatching
records, which remain non-replayable. Pending/show expose current states and
hashes without terminal control sequences from peer content.

## Offer a goal to joining participants

Only the host configures the connection goal:

```sh
collab remote goal --text 'Proposed shared objective'
collab remote goal --clear
```

The template persists across hub restarts. Each join receives a fresh, directly
addressed goal offer, valid for one hour, and stores it pending before starting
the listener. Joining never accepts it. The join output points to local review.
The human binds their coding session, reviews the full offer, then accepts or
declines with the same commands above. Connection goals use `receiver-selected`
as their offered target; the human's actual local target and working directory
are frozen in the consent binding. A prompt cannot use this goal-only choice.
Clearing the template stops future offers; existing pending offers can still
be declined locally or expire. An already accepted delivery is not revoked by
changing the host's template.
