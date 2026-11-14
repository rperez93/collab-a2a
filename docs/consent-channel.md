# Operator-terminal consent

`collab.consent` is a shared internal primitive for operations requiring local
operator review. Integration into remote prompts and skill operations is a
separate requirement: adding this module alone does not secure existing callers.

The supported channel is a standalone foreground controlling terminal, opened
outside the coding agent, daemon, or terminal multiplexer. The primitive checks
the complete visible process ancestry, rejects recognized agents and automation
wrappers, and refuses when ancestry cannot be inspected. It displays a bounded,
JSON-escaped review and a fresh code exclusively through `/dev/tty`. Approval
cannot be supplied by a command-line flag, stdin, or the content digest.

This is protection for supported CLI execution paths, not proof of human
identity. Ancestry recognition cannot identify every custom automation program.
Programs with the same operating-system privileges can tamper with local state;
this mechanism does not isolate them. Stronger guarantees need a separate
authenticated authority outside that privilege boundary.

## Caller contract

1. Construct an immutable operation and its content digest. Bind it to the exact
   local destination, command, working directory and authority to be granted.
2. Call `request(root, subject=..., request_id=..., digest=..., binding=...,
   review=...)`. The review must show the exact operation, destination and
   authority; the caller is responsible for correspondence with the digest.
3. Revalidate the operation and current local configuration. Call
   `consume(root, receipt, subject=..., request_id=..., digest=..., binding=...)`
   immediately before committing the authorized transition. Receipt consumption
   is serialized, bound to all four fields, expires after 120 seconds, and is
   single-use. Do not revive a consumed receipt after ambiguous delivery.
4. Append lifecycle metadata with `audit(root, event=..., subject=...,
   request_id=..., digest=..., binding=..., details=...)`, including offer,
   refusal, dispatch and delivery outcomes as applicable. Do not store raw
   prompt text, challenge codes or receipt tokens in metadata.

Receipts must stay inside the local process; never expose minting or consumption
as a remote API. `ConsentRefused` is a visible refusal, not permission to fall
back to another approval channel. Approval and decline records identify the
terminal channel, reviewer UID and PID. The private SQLite audit rejects updates
and deletion through application-level triggers; it is not tamper-proof storage.

This primitive does not supply sender throttling, transport authentication,
recipient capability negotiation or prompt isolation. Callers must provide those
controls and demonstrate their end-to-end behavior before enabling delivery.
