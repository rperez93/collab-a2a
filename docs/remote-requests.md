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

The recipient must advertise `remote_consent_version: 1`. The sender checks
before queueing and the hub checks again before persisting or forwarding, so
an old or unknown receiver cannot receive an offer through this feature. An
older client rejoining clears its previous capability. The server does not
offer connection goals to clients without this capability.
Every current client request also carries `Collab-Remote-Consent: 1`.
Without that header, remote offers are excluded from history, replay,
snapshot and both replayed and live SSE events. This protects a saved session
resumed by older software even if its stored capability is stale; replay
cursors still advance past excluded events.

Configure the ordinary local wake adapter first, then bind its exact command
and local working directory to the coding session the human selected:

```sh
collab remote bind --target CODING_SESSION_ID
collab remote pending
collab remote show REQUEST_ID
collab remote accept REQUEST_ID
collab remote decline REQUEST_ID
```

Binding does not approve an offer. Acceptance must run from a standalone
foreground terminal opened by the human outside the coding agent, daemon and
terminal multiplexer. The review shows the escaped offer, local command,
working directory and adapter authority. A fresh one-time code appears only
on the controlling terminal; type it there to approve. Stdin, a content hash,
and an agent-provided approval flag cannot authorize execution.

The resulting receipt binds this exact operation and local configuration,
expires after 120 seconds and is consumed once before acceptance. See
[the consent channel contract](consent-channel.md) for its audit and threat
boundary: this channel restricts supported CLI paths but does not prove human
identity or isolate hostile programs with the same OS privileges.

Saved approvals created by the former flag/hash channel are invalidated when
the queue is opened. Pending offers can be reviewed through the new channel;
failed legacy approvals require a fresh offer. An observed command change
during review also invalidates earlier approvals, even if that command is
subsequently restored.

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
The authenticated join response identifies the host issuer separately; the
receiver checks the offer against that identity, its session and recipient.
An offer's own sender field cannot establish its origin.
The human binds their coding session, reviews the full offer, then accepts or
declines with the same commands above. Connection goals use `receiver-selected`
as their offered target; the human's actual local target and working directory
are frozen in the consent binding. A prompt cannot use this goal-only choice.
Clearing the template stops future offers; existing pending offers can still
be declined locally or expire. An already accepted delivery is not revoked by
changing the host's template.
