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

The recipient must advertise `remote_consent_version: 2`. Version1 advertised terminal receipts and is refused. The sender checks
before queueing and the hub checks again before persisting or forwarding, so
an old or unknown receiver cannot receive an offer through this feature. An
older client rejoining clears its previous capability. The server does not
offer connection goals to clients without this capability.
Every current client request also carries `Collab-Remote-Consent: 2`.
Without that header, remote offers are excluded from history, replay,
snapshot and both replayed and live SSE events. This protects a saved session
resumed by older software even if its stored capability is stale; replay
cursors still advance past excluded events.

Configure a session-addressed wake adapter first (`collab wake set --agent codex`
or `collab wake set --agent tmux`), then bind its exact command and local working
directory. The target must equal the actual thread or pane in that command;
an arbitrary label cannot redirect an offer to a different session. Tmux also
requires the recorded PID and process identity, which its delivery adapter
checks before typing. **Tmux approval is scoped to the pane, not a provider's
conversation ID:** another conversation of the same agent in that pane can
receive the approved turn. The approval review explicitly displays this scope.
Use the Codex thread adapter when the grant must name one exact Codex thread.
Remote delivery requires the installed Collab executable
at an absolute path and the shipped adapter arguments. Custom commands and
recipes that start a fresh process are ordinary wake options, not supported
destinations for this session-addressed remote feature.

```sh
collab remote bind --target CODING_SESSION_ID
collab remote pending
collab remote show REQUEST_ID
collab remote accept REQUEST_ID
collab remote decline REQUEST_ID
```

Binding does not approve an offer. Acceptance opens the enrolled-key approval
window and shows the exact hub-canonical offer and receiver-selected command,
working directory and authority. The challenge is fetched only after Approve;
the hub verifies the original proof and issues a single-use receipt. There is
no terminal-code, stdin, hash or approval-flag fallback. The existing coding
session retains its permissions, and no additional sandbox is applied.

Before consuming the receipt, the receiver durably records an `approving`
attempt. A lost response or crash leaves that attempt non-dispatchable; it is
never automatically retried. Only a matching consumption answer containing the
exact reviewed canonical bytes can make it accepted. The hub never sends the
receiver's private argv or cwd back to the requester.

**The requester is told nothing about the outcome.** There is no delivery
status yet: neither the hub nor the receiver reports back whether an offer was
reviewed, approved, declined, delivered or left uncertain. A sender that needs
to know must ask in an ordinary message, and the receiver's answer is only its
own word.

**Remote delivery is off until the hub's host turns it on:**
`collab config remote_delivery on`, on the machine that hosts the session.
Off — the default — the hub advertises no remote delivery and refuses to
prepare or consume a remote review, and `collab remote accept` refuses («this
hub has not enabled strong remote delivery»). Offers are still received, held
and listed. The setting is an availability switch, not consent: each delivery
still needs the receiver's own gesture on the reviewed operation. It stays off
by default because its release gate is a native human ceremony on the
installed adapter, which automated tests do not perform. See the
[strong-approval contract](remote-strong-adoption.md).

Old accepted flag/hash/terminal-receipt grants are invalidated when the queue
opens. Pending offers need their authenticated hub sequence before review;
legacy offers can acquire it from matching authenticated replay. Failed legacy
grants require a fresh offer. A changed command invalidates earlier grants
even if the command is subsequently restored. An expired unconsumed hub review
can be explicitly reviewed again; a consumed or ambiguous local attempt cannot.

Acceptance is for one delivery only. The daemon atomically changes an accepted
request to dispatching before launching the frozen local command. Changing the
binding invalidates outstanding approvals; command, cwd, adapter identity and
expiry are checked again immediately before launch. An observed command change
requires binding and review again. Decline can revoke a request until it is claimed.
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
At most 16 retained records may belong to one sender, including completed or
declined records until compaction. Duplicate receipt does not consume another
slot. The hub permits 16 explicit offers per authenticated sender and 32 per
recipient in a rolling 60-second window. Automatic connection goals have a
separate 16-per-minute host budget, so guest joins cannot spend the host's
explicit-send allowance. Hub counters restart with the hub; receiver record
limits persist. Ordinary chat is unaffected. REST refusal returns HTTP 429
with `Retry-After: 60`; the worker keeps the offer queued, reports the throttle,
and waits at least a minute. A throttled or full-queue connection goal is
reported without aborting the connection.
Expired records are compacted after another hour, except ambiguous in-flight
records, which remain non-replayable. Pending/show expose current states and
hashes without terminal control sequences from peer content.

The private shared consent audit records offer receipt and quota refusal,
local decline, strong attempt and consumption, requested acceptance and
dispatch, observed adapter results, and revocation reasons (expiry, changed
configuration, legacy approval or local decline). It stores IDs and digests,
not raw prompt text, challenge codes or delivery tokens. Audit records survive
queue compaction and reject application-level updates and deletion.

Audit and queue storage use separate transactions. Events ending in
`_requested` record an attempted transition, not proof that the queue commit
completed. `delivery_result` records the observed adapter result; an interrupted
queue commit may still leave the queue dispatching. Audit failure prevents
new dispatch. Revocation is committed even if its audit write fails, so an
unavailable audit cannot restore permission; that failure is reported to the
caller. A crash after dispatch reservation remains non-replayable.

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

## Release validation gate

The strong hub and receiver paths are implemented and tested with isolated HTTP
hubs and synthetic signing keys. The host's `remote_delivery` setting stays off
by default until actual native human ceremonies, passkey adoption and the
shared installed-version validation are complete. Automated tests do not prove human presence or
end-to-end readiness of a deployed session.
