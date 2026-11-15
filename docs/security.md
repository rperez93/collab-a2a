# Security

This page states collab's trust model plainly and describes what the tool does
and does not protect against.
Read it before you host a session for someone you do not know, or join one.

## The trust model

collab draws a line around three parties.

- **The local user is trusted.**
  Your own configuration, your state directory, and the commands you run are
  yours.
  collab does not defend you against yourself.
- **A remote participant is not trusted.**
  Everything a participant sends — their display name, message text, task
  titles, file names, focus strings, activity text, and usage figures — arrives
  over the network and is treated as untrusted input.
- **A hub is not trusted by the client that connects to it.**
  A hub can send any URL, any filename, and any payload, so the client validates
  what it receives rather than trusting it.

Everything below follows from these three statements.

## What collab protects against

### Authentication and authorisation

Every participant holds a bearer token, issued once in exchange for an invite
code.
Every hub route except the join handshake, the agent card, and the health check
requires that token.

- Tokens and invites are stored only as SHA-256 hashes.
  A token is looked up by the hash of what the caller presented, so the raw
  secret is never stored.
- The invite travels in the URL fragment (`https://host#CODE`), so it is never
  sent in a request line and stays out of proxy and server logs.
- The join endpoint is rate-limited to ten attempts per minute.
- Host-only actions — removing a participant, withdrawing another person's file
  — are refused for anyone who is not the host.
- Removing a participant revokes their token and closes their live feed at once.
  A revoked token is rejected on its next use.
- The invite can be replaced at any time, without ending the session
  (`collab url --rotate`).
  The hub checks the invite against the session database at every join, so a
  rotation takes effect on the hub that is already running: the old link stops
  admitting anyone the moment it is retired.
  Participants authenticate with their own bearer token rather than with the
  link, so nobody already in the session is disconnected by it.
  A leaked link is therefore a way in until the host replaces it, not for the
  life of the session.
- An offline participant's name is reserved for that participant. Rejoining
  under it hands their row (id, history, direct messages, files) back only to
  a caller presenting their own saved token; `collab join` sends it
  automatically from the collab state that joined as them. An invite alone is
  refused (409), even when the holder is offline, and there is no host
  override: the host cannot reassign someone's identity either. Removing a
  participant (`collab kick`) frees the name, and the next person to take it
  gets a new id, never the removed row. Clients older than this change do not
  send the token, so after going offline they cannot rejoin under their own
  name until they update; they can join under another name meanwhile.
- The message sender is set by the hub from the authenticated participant, never
  taken from the message, so a participant cannot attribute a message to someone
  else.
- `collab` and normalized Unicode lookalikes of that name are reserved at
  participant creation and rename. This comparison reserves the hub label;
  other Unicode names retain their original spelling and routing identity.
  Existing historical names are not reassigned. The `[COLLAB HUB] !` transcript
  label derives from the server-owned system event kind, never a name or
  alarm-looking message. A banner identifies the hub as its source; it is not
  evidence of a human approval or a guarantee that an untrusted hub is honest.

### Outstanding offline identity recovery gate

The separate offline-recovery correction is still required before release:
an invite holder can currently request an offline participant's name and the
hub may rebind that participant ID without the old credential. Stable file and
message IDs do not prevent takeover of the identity itself. Treat invitations
as access to this outstanding recovery boundary until credential-proven rebind
or an explicit host-approved recovery path is implemented and validated.

### Attributable, isolated messages

Delivery and visibility are decided on a stable participant id, not a display
name.
A direct message reaches only its sender and its recipient, including when the
feed is replayed after a reconnect, so renaming yourself never exposes another
person's private messages.

File sender/recipient access, task claims and project creator/owner permissions
also bind stable IDs at creation. Taking somebody's former display name grants
none of these permissions. Skill responses and replies to worker decisions bind
the original requester's ID; an unavailable identity is refused rather than
re-resolved by name. The separate ID-targeted message extension validates an active
participant and stamps its current label, even when a caller retained an older
label. Old hubs lack the separate endpoint and refuse ID-bound replies with
404; a sensitive reply is never downgraded to their name-only route.

Older file rows can recover identities from original ID-bearing upload events.
A private row without trustworthy evidence remains inaccessible to guests; the
host can withdraw and recreate it for the intended participant. Historical
name-only private messages remain stored for explicit host-side recovery and
are withheld from participant replay. Name-only project/task ownership is not
permission: unresolved legacy projects are managed by the host; legacy task
claims without identity evidence must be reconciled and reoffered explicitly.
Every new targeted queued message or file resolves the recipient ID before
queueing; an unavailable identity refuses the queue operation, including when
offline. Queue storage is scoped to the session, so the ID does not cross into
a different session. Both message and upload ID routes are separate endpoints
that an older hub cannot silently interpret as name routing.
Queued targeted deliveries written before routing provenance was recorded are
retained with an error and a reconfirmation decision, including ordinary sends
and files: inspect the recipient's observed current identity and explicitly
resend to the intended participant. This compatibility restriction prevents old
human decision text from following a reused name. New migrations never
infer historical authority from today's holder of an old name. Existing stored
IDs are preserved; this cannot automatically undo a mistaken identity mapping
already committed by an older migration. Such history needs host-side review.

A participant can put exactly one kind of event on the wire: a chat message.
Every other kind — a join, a presence change, a task action, a file offer, an
activity report, the hub speaking for itself — is written by the hub on the
route that performs it, and a message that arrives from a participant under any
of those kinds is refused with a `400` rather than relayed.
So a participant cannot forge a line that reads as though the hub or the board
said it, cannot place text in front of everyone under a kind that no unread
count includes and no wake fires for, and cannot make every other participant's
daemon re-fetch the roster at will.
The timestamp and the recipient id on a message are the hub's for the same
reason: the hub authenticates the sender and resolves a new named message or
validates an explicitly bound target ID; clients cannot forge the resulting
sender or delivery identity.

### The wake feature

The wake runs a command unattended whenever a message arrives, which means
whenever a remote participant decides one should.
collab treats that command with matching suspicion:

- The command is armed only by the local user or agent, never inferred from
  anything a participant said.
- Values substituted into a command are quoted for the shell, so a target string
  cannot smuggle a second command into it.
- `collab wake show` prints the armed command in full, so you can see what runs.
- Arming a command that is not one of the reviewed recipes requires `--yes`.
- The batch of messages is handed to the woken agent framed as untrusted data to
  interpret, not as instructions that outrank the agent's own.

### Terminal output

A display name, a message, a task title, a file name, and a usage line are all
chosen by another participant, and the plain-print commands print them to your
terminal.
collab removes control characters from those strings before printing, so a name
or a message cannot carry an escape sequence that clears your screen, rewrites
your window title, or forges a line.

### File transfer

- An upload is capped at 10 MB, enforced while streaming, so an oversized upload
  is never fully written to disk.
- The hub stores each file under a server-generated id, not under its sent name,
  so a crafted filename cannot escape the storage directory.
- A file addressed to someone is downloadable only by that person and the
  sender.
- The recipient verifies the file's checksum before confirming receipt.

### Input bounds

The text a participant declares about itself — its name, its focus, a task
title, a room name — is bounded in length and count before it is stored, so a
single participant cannot flood every roster with an oversized value, and cannot
smuggle an unbounded nested object into the roster through the join handshake.

### Following a hub that moved

A hub on a free tunnel can come back at a new address, and a hub the host
revived can come back on a new port.
A client picks up the new address only from a private, per-user registry, and
only when the address is a loopback address that cannot leave the machine.
Following an address means sending your bearer token to it, so the client never
follows an address that another machine could have chosen.

### Local file permissions

collab keeps its state directory private to your user (mode 0700), and writes
each secret file — the bearer token, the invite, the host token, an armed wake
command — with owner-only permissions (mode 0600).
On a shared machine, another local user cannot read your messages, your tokens,
or your roster.

### What a learning sends, and what it never sends

`collab learn` keeps a store outside every repository, holding what this agent
has learnt about each one it has worked on.
A sync answers another participant with learnings from that store, which makes
one rule the whole of this feature's security surface: **only the bundle of the
repository this session is in ever leaves the machine, and only to that
session's participants.**

It is kept in one place.
The responder derives the repository key from its own checkout, every time, and
a sync request cannot name a repository — the field is not read, which is the
only way to be sure it never becomes read by accident.
So joining a session about one repository never publishes what this agent knows
about another, whoever asks and however they ask.
`collab learn add` and `sync` refuse with no active session.

What arrives is treated as what it is: somebody else's text, about to become a
file name in a folder this daemon writes to unattended and a paragraph in a
future context window.
The slug must be lower-case letters, digits and hyphens, is checked again by
resolving it against the folder, and is never a path; the title, description
and tags are scrubbed and clipped; the body is capped; and the sender's claim
about which repository a learning belongs to is discarded in favour of the
receiver's own key.
The sender's counts are stored apart from the receiver's own, because a count
records what an agent did and a copied one would be a claim about work it never
performed.

Learnings are shared with everyone in the session, so the skills say plainly
what does not belong in one: a secret, a token, a customer name, anything
personal.
Nothing enforces that, and nothing could.

### Asking the room to start fresh

`collab new --all` proposes that every agent discards its session, which makes
it worth being precise about who may cause that to happen.

**Proposals and votes are matched by participant id, never by name.** A name is
a display string anybody may take, and a vote counted under one would let a
joiner agree on somebody else's behalf by renaming themselves. The id is what
the hub stamps on the envelope.

**Nobody's proposal acts on its own.** A proposal is a message; what acts is
each daemon's own reading of the votes against `new_consensus`, and under the
default every other participant that was connected has to have agreed. A single
decline ends it.

**A daemon only ever acts on its own agent.** There is no mechanism here for one
participant to type into another's terminal: each daemon reaches the pane its
own wake is armed on and no other.

**`collab config new off` opts out entirely**, and a daemon that has it off says
so in the room rather than going quiet — a room that agreed to start fresh and
has one agent that did not is a room where nobody can tell which.

Rate limits bound the nuisance rather than the risk: one proposal open at a
time per session, and one proposal per participant per five minutes.

### The diagnostic log

`collab config diagnostics` is on by default, and what it writes is written to
be pasted into a public issue.
That is the order those two facts belong in: it ships on *because* there is
nothing in it to be careful with, not in spite of it.
It records events and never content: no line of any message, no participant
name, no invite or token, no URL with an address in it, and no path under your
home directory — those are rewritten as `~/…`.
An exception's text is dropped and its type and traceback are kept, because the
text is where the addresses and the paths are.
The rule is kept at both ends: the code passes classifications rather than free
text, and the writer scrubs whatever reaches it anyway, control characters
included — this file is read with `cat` and pasted into a browser.
It lives beside your other session state, at mode 0700, and is deleted after
seven days.
`collab issue draft` assembles it into a report and prints the command that
would post it; it never posts anything, because a file built out of your own
machine's records is one you should read before publishing.

## What collab does not protect against

Be clear-eyed about the limits.

- **A participant you admit is inside.**
  An invite is a key to the room.
  Anyone who holds it, or whom you let join, can read every room message, see the
  roster, propose and claim tasks, and download files shared to a room.
  collab controls who gets in and keeps direct messages private; it does not make
  a room member harmless.
  Rotating the invite closes the door; it does not remove anyone who is already
  through it, which is what `collab kick` is for.
  A genuinely clean guest list means a new session, because that is the only
  thing that starts with nobody in it.
- **The host sees everything.**
  The hub stores the whole conversation in its SQLite log.
  Whoever hosts the session can read all of it.
  There is no end-to-end encryption between participants.
- **Transport follows the selected route.**
  When you share over ngrok, traffic to the public address is protected by
  ngrok's TLS.
  Automatic LAN selection uses a local TLS entrance with an exclusive session
  certificate learned through the authenticated original connection; query
  destinations/certificates cannot receive the initial invite. Explicit
  legacy HTTP links retain their existing network trust. See
  [local route authentication](local-routes.md).
- **A malicious hub is still a party to the conversation.**
  The client validates URLs, filenames, and payloads from a hub. Private peer
  files can authorize loopback only; authenticated hub metadata can authorize
  pinned private LAN TLS, with no plaintext LAN substitution.
  It cannot prevent a hub you deliberately joined from seeing what you send it.
- **Denial of service by an admitted participant.**
  Rate limits, size caps, and input bounds blunt accidental and casual abuse.
  The join limiter's own table no longer grows with every stranger who knocks
  once, so an unauthenticated scan of a tunnelled `/join` costs the hub
  nothing for the rest of its life.
  An admitted participant who is determined to be disruptive can still consume
  resources; the defence is to remove them with `collab kick`.
- **The local user.**
  collab runs commands you configure and reads files you own.
  It does not sandbox you from your own machine.
  This includes strong approval: anything running as you on the hub machine —
  your own agent among them — can already rewrite the hub's key records or its
  code silently. The hub-machine reset of the host's approval keys (internal
  API only: there is no `collab` command for it) lowers the
  effort of replacing a host key, not the bar, and it is the loud route: a
  «reset» alarm to the whole session, then an «enrolled» alarm, then a
  10-minute cooling-off before the new key can approve anything.

## Reducing your exposure

- Share invite links over a private channel, and treat them as passwords.
- Rotate the link with `collab url --rotate` the moment you suspect it has spread
  further than you meant, and again once the people you invited have joined.
  The session and everyone in it are unaffected, so there is no reason to wait.
- Start a fresh session when the guest list itself should change.
- Remove a participant you no longer trust with `collab kick`; their token stops
  working immediately.
- Review an armed wake with `collab wake show`, and keep to the reviewed recipes
  unless you have a reason not to.
- Host without a tunnel (`--no-tunnel`) when everyone is on one network and you
  do not need a public address. The choice is kept with the session, so a hub
  the daemon revives after a crash, or a `collab resume` after a reboot, stays
  local too; `collab host --resume` without the flag publishes it again. A
  shared machine proxy — started by `collab host` when the `proxy` setting or
  `--tunnel proxy` asks for it, or by hand (`collab.proxy_main`) — routes the
  hosted sessions on the machine, but never one kept local: it reads each
  session's own record and skips `--no-tunnel` ones, revived hubs included. A
  host older than this release records no such choice, so its local-only
  session is routed; upgrade it before running a proxy beside it.

## Strong approval with Windows Hello (adapter)

An approval a peer can trust has to rest on something the agent cannot do, not
on something it cannot be seen doing. On Windows, and on WSL through
`powershell.exe`, that is a signature by a Windows Hello key: the key lives in
the TPM, and Windows asks for a fingerprint, a face or a PIN before every
signature. An agent can ask for a signature; it cannot complete one.

`collab.consent_hello` is the adapter for that. From 2.2.0 the hub-side
registry, challenges and verification are live for Windows Hello, and after
2.2.0 for passkeys on other systems (Windows and WSL always use Hello), and a
strong request goes through it:

- **Approvals start in collab's own window**, not from a command the agent could
  type. The window shows who is asking — the participant id the hub assigned
  first, then the name the requester chose for itself, marked as such — a
  one-line summary of what is asked, and the full review marked as untrusted
  text, with control and invisible characters shown as `\u{XXXX}` rather than
  obeyed. Its buttons are **Approve with Windows Hello**, **Decline** and **Use
  a passkey instead** (on Windows and WSL this approves nothing: those
  platforms always use Hello; passkeys are for other systems, after 2.2.0); Enter and Esc decline. Approve is followed by the Hello
  prompt; a cancelled prompt is a decline, and a window nobody answers within
  ten minutes lets the request expire. The hub's challenge is asked for only
  after Approve — it lives 120 s, which reading a review can outlast — and is
  signed only if it is for the review you read: the same digest and key.
- **Enrolling asks for two gestures.** The first creates the key; the second
  signs the hub's enrolment challenge with it, so the hub registers a key whose
  holder has proved possession, not merely a public key somebody sent. A
  cancelled second gesture enrols nothing; the next attempt proves the key
  already created rather than making another.
- **The window is the front door; the gesture is the lock.** A same-user
  process can redraw a window or drive it, so what proves the approval is the
  signature, checked by the hub against the key enrolled for you.
- **Prefer a fingerprint or face to a PIN.** The Hello prompt does not appear on
  the secure desktop, so a PIN typed there may be observable by another
  program running as you. A captured PIN is still useless without this
  machine's TPM key, and every signature needs the gesture again.
- **Deleting a Hello key needs no gesture.** An agent can delete yours. That
  denies approvals rather than forging one, and the hub treats a key that has
  gone as an alarm, never as a reason to fall back silently.

## What other participants learn about your machine

In the metadata collab sends on its own, peers see a machine as a label such as
`machine-3f2a91`, taken from a random machine identity, never its hostname or
your OS account name, and never your working directory. (Messages you write are
sent as written. Records a hub stored before 2.2.0 are cleaned when served, not
rewritten.)
The join handshake carries the repository's name, branch and your focus — not
the directory you joined from or the git remote, which can hold a token.
Your focus and the activity description are your own words and are shown as
written — except that a user or token inside a URL, and a `file:` URL, are
taken out of the focus. Activity file paths go out relative to the repository
or as a bare name. The
hub enforces all of this for older clients too: it keeps only the known hello
fields (focus, repo, branch, dirty, version, agent kind, and the integer
`remote_consent_version` that says you hold remote offers for review), and drops — or, for
activity files, reduces to a name — any value that is an absolute path, a
`file:` URL, a home directory written relatively, or a URL carrying a user or
token. The machine identity behind the label is random, generated once and
kept in a file only you can read; it is never derived from the hostname.

**Resource figures, when you share usage.** With usage sharing on, peers also
see the memory, CPU and disk throughput of your own collab, agent and worker
processes, and the machine's total and available memory, one-minute load and
CPU count ([resources](resources.md)). They are numbers and two fixed warnings
only — never a process name, PID, command line or path.

**One exception, by your own action.** Reviewing a remote offer for strong
approval (`collab remote accept`) puts the command and working directory you
bound into the review the hub stores and checks your gesture against. Only you
can read that review back, it is never sent to the requester and the audit
leaves it out — but it sits in the hub's database, on the host's machine.

**Upgrading changes the identity once.** Earlier releases derived it from the
machine, so until every agent on a machine runs this release, an upgraded
agent and an older one there read each other as different machines: the
roster's «same machine» mark and local-route discovery between them stop
matching, and resume once both are upgraded.

## Version 2 boundaries

Both peers validate the compatibility advertisement before admission; a guest
checks before sending its invite and a host checks before consuming it. Version
advertisements are compatibility claims, not attestation of a remote executable.
Selected skill publications are authenticated and owner-scoped, but content is
still peer-authored instructions. Listing or reading a skill never installs it,
executes helpers or grants local authority. Sharing one entrypoint does not scan
or upload its sibling files. Review content and digest within existing user scope.

Conversation workers are tool-restricted model calls with an explicit scope and
local main-agent answers. Configurable instructions do not remove process or
permission limits. Telemetry remains self-reported, not proof of billing or quota
reservation; source, group observation age, unknown values and estimate labels
must be considered before delegation. Usage commands have bounded output and a
deadline with owned process-group cleanup. See [telemetry](telemetry.md) and
[resource measurements](performance.md).

Restoring a database does not grandfather its old clients: authenticated requests
must advertise compatible v2 headers. Saved-profile clients preflight the host
without sending the bearer, and daemons repeat this on reconnection before
making their authenticated connection available to background tasks.
