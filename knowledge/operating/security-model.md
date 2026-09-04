---
type: Trust Model
title: The trust model
description: The three parties collab draws a line around, what follows from that, and the things it plainly does not defend against.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/docs/security.md
tags: [security, trust, auth, limits]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
  - { by: process:pytest, at: 2026-09-02T00:25:00Z }
sources:
  - id: security-doc
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/docs/security.md
    title: docs/security.md — the same model, written for a person
  - id: auth-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/auth.py
    title: collab.server.auth — bearer tokens and the join limiter
    last_modified: 2026-08-31T19:42:12Z
  - id: store-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/store.py
    title: collab.server.store — tokens and invites stored only as hashes
    last_modified: 2026-09-01T23:18:43Z
  - id: daemon-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/client/daemon.py
    title: collab.client.daemon — following a hub that moved
    last_modified: 2026-09-02T00:20:53Z
---

# Three parties

| Party | Trusted? |
|---|---|
| The local user | Yes. Your configuration, your state directory, your commands. collab does not defend you against yourself. |
| A remote participant | No. Their display name, message text, task titles, file names, focus strings, activity text and usage figures all arrive over the network. |
| A hub, as seen by a client | No. A hub can send any URL, any filename and any payload, so the client validates rather than trusts. |

Everything below follows from those three lines.[^security-doc]

# Authentication

An invite is exchanged **once** for a per-participant bearer token, so every
message is attributable to a named participant and any single participant can
be revoked without disturbing the others.[^auth-src]

- Tokens and invites are stored **only as SHA-256 hashes**. A token is looked
  up by the hash of what the caller presented, so the raw secret is never
  written down.[^store-src]
- Each secret is 32 bytes from `secrets`, URL-safe — roughly 256 bits. The
  session URL is public once tunnelled, so this is the only thing standing
  between a stranger and the room.[^auth-src]
- The invite travels in the **URL fragment** (`https://host#CODE`), so it never
  reaches a request line, a proxy log or a server log.
- `/join` is rate-limited to 10 attempts per minute. Since commit `3ea97f1`
  (*The join limiter forgets a caller once it has stopped limiting them*) the
  limiter also forgets a caller once every attempt of theirs has aged past the
  window. At the pin it trimmed a caller's timestamps and never the caller's
  key, and a tunnelled `/join` is reachable from the whole internet, so every
  scanner that knocked once held an entry for the life of the hub — 15.9 MiB
  after 20,000 distinct addresses, measured. The sweep runs once per window
  over the whole table, and it is deliberately not a fixed-size cache: evicting
  the least-recently-seen key would let an attacker reset their own count by
  having enough other addresses knock in between. That claim post-dates the pin
  and carries no `verified` stamp until the bundle is re-pinned.
- Revoked and never-valid look identical from outside, on purpose.
- An unauthenticated connection is left unauthenticated rather than rejected at
  the middleware, because the routes decide what needs a token — which is what
  keeps the agent card, `/join` and `/health` reachable without one.

# Authorisation

- The message sender is set by the hub from the authenticated participant,
  never taken from the message body, so nobody can attribute a message to
  somebody else.
- Removing a participant, and withdrawing somebody else's file, are host-only.
- Removing a participant revokes their token and closes their live feed at
  once: the stream emits `{"reason": "revoked"}` and ends, and the token is
  rejected on its next use.
- Delivery and visibility are decided on the stable participant id, including
  on replay after a reconnect, so renaming yourself never widens what anybody
  can see. See
  [rooms and direct messages](/collaboration/rooms-and-direct-messages.md).

# The wake

The wake runs a command unattended whenever a message arrives — which means
whenever a remote participant decides one should. It is treated with matching
suspicion:[^security-doc]

- Armed only by the local user or agent, never inferred from anything a
  participant said.
- Values substituted into a command are shell-quoted, so a target string cannot
  smuggle a second command in. The substitution runs in a **single pass**,
  because a second pass would re-quote already-quoted text and leave a
  repository genuinely called `{target}` with a corrupted path and a wake that
  fails for ever.
- `collab wake show` prints the armed command in full.
- Arming a command that is not one of the reviewed recipes requires `--yes`.
- The batch is handed to the woken agent framed as untrusted data to interpret,
  not as instructions that outrank the agent's own.

# Following a hub that moved

A hub on a free tunnel can come back at a new address, and a hub the host
revived can come back on a new port. A client picks up a new address only from
a private, per-user registry, and **only when it is a loopback
address**.[^daemon-src] Following an address means sending your bearer token to
it, so the client never follows one another machine could have chosen.

`_is_loopback` parses the URL rather than matching on the string.
`http://127.0.0.1.evil.example/` and `http://user@127.0.0.1@evil/` both contain
`127.0.0.1` and neither is loopback; anything that does not parse is refused.

# What it does not protect against

Stated plainly, because the limits are the useful half of a trust
model.[^security-doc]

- **A participant you admit is inside.** An invite is a key to the room. Anyone
  holding it can read every room message, see the roster, propose and claim
  tasks, and download files shared to a room. For a genuinely clean guest list,
  start a new session rather than resuming one — see
  [sessions](/architecture/session.md).
- **The host sees everything.** The whole conversation is in the hub's SQLite
  log. There is no end-to-end encryption between participants.
- **Transport privacy depends on the tunnel.** Over ngrok, traffic to the
  public address is protected by ngrok's TLS. A hub reachable only on a local
  network, with no tunnel, is as private as that network.
- **A malicious hub is still a party to the conversation.** The client
  validates URLs, filenames and payloads, and refuses to send its token to a
  non-loopback address it found in a file. It cannot stop a hub you
  deliberately joined from seeing what you send it.
- **Denial of service by an admitted participant.** Rate limits, size caps and
  input bounds blunt the accidental and the casual. A determined admitted
  participant can still consume resources; the answer is `collab kick`.
- **The local user.** collab runs commands you configure and reads files you
  own. It does not sandbox you from your own machine.

# On disk

The state directory is `0700`, and the individually sensitive files are `0600`.
The message log is not, and is protected by the directory above it. That
distinction is measured rather than asserted in
[the state directory](/architecture/state-directory.md), and it matters,
because a file copied out of that tree carries no protection at all.

# Related

- [Input from somebody else](/operating/hostile-input.md) — the mechanics of
  the second row of the table at the top.
- [docs/security.md](../../docs/security.md) — the same model in prose, for a
  person.

[^security-doc]: docs/security.md — the same model, written for a person
[^auth-src]: collab.server.auth — bearer tokens and the join limiter
[^store-src]: collab.server.store — tokens and invites stored only as hashes
[^daemon-src]: collab.client.daemon — following a hub that moved
