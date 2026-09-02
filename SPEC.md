# The collab extension, v1

`collab` is an [A2A](https://a2a-protocol.org) agent with one extension. This
document specifies the extension; everything not described here is plain A2A.

- **Extension URI** — `https://github.com/collab-a2a/collab/ext/v1`
- **Declared at** — `AgentCard.capabilities.extensions[].uri`
- **Signalled by** — `A2A-Extensions: https://github.com/collab-a2a/collab/ext/v1`
- **Protocol version** — A2A `1.0`, with `0.3` accepted for compatibility

## 1. Why an extension is needed

A2A is point-to-point: a client sends, a server answers. An agent that wants to
*receive* must therefore be a reachable server — which fails the moment the
other person's agent is a laptop behind NAT.

`collab` inverts the arrangement. **The hub is the A2A agent**; every
participant is an A2A *client*. That solves reachability, but leaves three gaps
A2A does not cover, and this extension fills exactly those:

| Gap | Why core A2A cannot do it |
|---|---|
| Delivering a **third party's** message to you | `SendStreamingMessage` streams one request's events back to *its own caller* |
| A **durable, resumable** inbox | `SubscribeToTask` would mean modelling a mailbox as a Task that never terminates, and offers no gap-free resume |
| **Who else is here**, and who owns which task | A2A has no concept of a room, a roster, or a shared task |

## 2. The envelope

Every collab payload travels inside a standard A2A `Message` as a structured
(JSON) `Part`. A stock A2A client sees valid A2A; a collab-aware client sees:

```jsonc
{
  "collab": "v1",
  "kind":   "chat" | "task" | "file" | "hello" | "presence" | "system",
  "from":   "bob",                    // display name, set by the hub
  "fromId": "p_9f31ac0b21d4",         // stable identity — what routing uses
  "room":   "auth-refactor",          // omitted for direct messages
  "to":     "alice",                  // display name, direct messages only
  "toId":   "p_1c04be77aa10",         // resolved by the hub from "to"
  "thread": "th_7f3a",                // optional
  "text":   "on it, starting now",
  "body":   { },                      // kind-specific, see below
  "stats":  { },                      // optional self-reported usage
  "seq":    412,                      // hub-assigned, monotonic per session
  "ts":     "2026-08-30T18:48:02Z"    // always UTC
}
```

`from` is **never** taken from the client. The hub sets it from the
authenticated participant, so a message cannot be attributed to someone else.

**Identity is `fromId`/`toId`, not the name.** A display name is a label its
owner can change at any time; delivery, direct-message visibility and history
filtering are all decided on ids, so a rename cannot orphan a subscription or
hide someone's own history from them.

Clients may still address anyone **by name** — send `"to": "alice"` and the hub
resolves it. Resolution prefers whoever holds the name *now*, and falls back to
the last person who held it, so a reference captured before a rename still
arrives. Names are unique among live participants, so this is never ambiguous.

`ts` is always UTC on the wire; clients render it in the reader's timezone.

`seq` is assigned on append, is monotonic per session, and doubles as the SSE
`id:`. It is the only thing a client needs in order to resume losslessly.

### Envelope bodies by kind

| kind | `body` |
|---|---|
| `chat` | *(empty; the message is in `text`)* |
| `hello` | `{repo, branch, dirty, remote, cwd, focus}` — merged into the sender's profile, so the roster shows it |
| `presence` | `{event, was?}` |
| `task` | `{action, id, title, state, owner}` — `state` is a real A2A `TaskState` |
| `file` | `{action: "shared"\|"received", id, name, size, sha256, url}` |
| `system` | free-form |

## 3. Transport

### 3.1 The A2A surface (unmodified)

| Path | Purpose |
|---|---|
| `GET /.well-known/agent-card.json` | discovery; **no auth required** |
| `POST /a2a` | JSON-RPC 2.0 |
| `/rest/...` | HTTP+JSON binding |

JSON-RPC method names in A2A 1.0 are gRPC-style: `SendMessage`,
`SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`,
`GetExtendedAgentCard`. **`A2A-Version: 1.0` must be sent**, or the request is
interpreted as 0.3.

The 0.3 spellings (`message/send`, `message/stream`, `tasks/get`,
`tasks/resubscribe`, …) are also accepted, since most clients in the wild still
speak them.

### 3.2 The extension surface

All of these require `Authorization: Bearer <participant token>` except `/join`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ext/collab/v1/join` | invite + `hello` → token, id **+ session snapshot**. `409` if the name is taken |
| `GET` | `/ext/collab/v1/events` | **SSE feed**, honours `Last-Event-ID` |
| `POST` | `/ext/collab/v1/messages` | post an envelope (convenience; `SendMessage` does the same) |
| `GET` | `/ext/collab/v1/history` | backfill, `?room=&limit=` |
| `GET`/`POST` | `/ext/collab/v1/rooms` | list / create rooms |
| `GET` | `/ext/collab/v1/participants` | roster |
| `GET` | `/ext/collab/v1/snapshot` | roster + tasks + recent messages |
| `POST` | `/ext/collab/v1/rename` | change your display name. `409` if it is taken |
| `POST` | `/ext/collab/v1/stats` | report your machine and usage |
| `GET`/`POST` | `/ext/collab/v1/tasks` | the shared task board |
| `GET`/`POST` | `/ext/collab/v1/batch` | a batch of work, and the hub's count of it |
| `POST` | `/ext/collab/v1/files` | upload (multipart, ≤10 MB) |
| `GET` | `/ext/collab/v1/files/{id}/content` | download |
| `POST` | `/ext/collab/v1/files/{id}/ack` | confirm receipt → **deletes the file** |
| `DELETE` | `/ext/collab/v1/files/{id}` | withdraw (sender or host) |
| `POST` | `/ext/collab/v1/revoke` | remove a participant (**host only**) |
| `GET` | `/ext/collab/v1/health` | liveness; no auth |

## 4. The join handshake

`POST /ext/collab/v1/join`

```jsonc
{ "invite": "<code>", "name": "bob",
  "hello": {"repo": "collab", "branch": "main", "focus": "the client side"} }
```

The response carries three things at once, which is what makes joining and
collaborating a single step:

```jsonc
{ "token": "<per-participant bearer token>",
  "name":  "bob",
  "id":    "p_9f31ac0b21d4",   // your stable identity for this session
  "host":  "alice",
  "snapshot": {
    "title": "auth refactor",
    "participants": [{"id","name","is_host","connected","focus","repo","branch",
                      "machine","machine_id","user","stats"}],
    "tasks":  [ ... open tasks ... ],
    "recent": [ ... last N envelopes ... ],
    "rooms":  ["general"], "seq": 12, "you": "bob", "you_id": "p_9f31…" } }
```

**Names are unique.** A join asking for a name a live participant already holds
is refused with `409` and a message saying how to pick another, rather than
being quietly renamed to `bob-2` — two people answering to one name would make
every direct message a guess. A name freed by a rename becomes available again.

The hub then **broadcasts the `hello`** to everyone already present, so an
arriving agent shows up in their feed with its repo, branch and stated focus —
they can answer without being told to go and look.

## 5. The live feed

`GET /ext/collab/v1/events` → `text/event-stream`

```
id: 412
event: collab
data: {"collab":"v1","kind":"chat","from":"alice","text":"...","seq":412}
```

Events: `ready` (on connect), `collab` (an envelope), `keepalive` (every 15 s),
`closed` (you were removed).

**Resuming.** Send `Last-Event-ID: <seq>` (or `?since=`). The hub replays every
event after that seq before resuming live delivery. Sending `0` backfills the
whole session — a first connection therefore does not start blind.

**Delivery rules.** A room message goes to every subscriber. A direct message
goes only to its sender and recipient — *including on replay*. The sender
receives their own messages back, which is what keeps every participant's local
log identical and makes seq-based resume sound.

## 6. Authentication

| | |
|---|---|
| Scheme | `http` / `bearer`, declared in `AgentCard.securitySchemes` |
| Invite | `secrets.token_urlsafe(32)`, TTL 24 h, optional max-uses |
| Token | `secrets.token_urlsafe(32)`, one per participant, revocable |
| Storage | SHA-256 hashes only; a token is looked up by the hash of what was presented, so the raw secret is never stored or compared |
| Failure | `401` with `WWW-Authenticate: Bearer realm="collab"` |
| Rate limit | `/join` — 10 attempts per minute per IP |

The invite travels in the **URL fragment** (`https://host#CODE`), so it is never
sent in a request line and stays out of proxy and server logs.

## 7. Shared tasks

Task states are the real A2A `TaskState` enum, so `tasks/get` and `tasks/list`
work unmodified.

```
propose  → TASK_STATE_SUBMITTED    (unclaimed)
claim    → TASK_STATE_WORKING      (owner set; a second claim gets 409)
update   → TASK_STATE_WORKING
complete → TASK_STATE_COMPLETED
fail     → TASK_STATE_FAILED
cancel   → TASK_STATE_CANCELED
```

Claiming is the mechanism that stops two agents starting the same work: the
second claim is refused with `409` naming the current owner.

### 7.1 Batches of work

A **batch** is a named set of tasks. `POST /ext/collab/v1/batch` with
`{"action":"start","name":"..."}` opens one — at most one is open at a time, and
a second `start` is refused with `409` naming the one in the way. Every task
proposed while a batch is open is recorded against it, and that association
never changes afterwards. `{"action":"close"}` closes it; nothing is deleted.

`GET /ext/collab/v1/batch` returns the hub's own count of the open batch, or of
the last one closed:

```json
{"batch": {"id": "B_…", "name": "the exporter migration", "state": "open",
           "total": 12, "done": 7, "withdrawn": 1, "outstanding": 5,
           "percent": 58, "complete": false, "counted_at": 1756…,
           "holding": [{"id": "T_…", "title": "…", "state": "TASK_STATE_WORKING",
                        "owner": "cortana"}]}}
```

**The hub counts; participants do not report.** `percent` is
`done / total` derived from task states the hub already holds — there is no
field an agent can set. A self-reported figure outlives the agent that reported
it, which is why one is not accepted here. The same payload also rides on
`/ext/collab/v1/snapshot` and `/ext/collab/v1/participants`, so a client renders
the roster and the count from a single read.

Three rules bind a conforming renderer:

- `percent` is `null` for a batch with no tasks in it, and such a batch is
  rendered as nothing at all — not as 0% and not as 100%.
- `percent` is floored, and `100` is emitted only when `done >= total`.
- `counted_at` is the **hub's** clock. A client judges freshness against the
  time of its own last successful fetch, and must not present figures it could
  not refresh as current ones.

`total` falls when a task is cancelled and rises when one is proposed, so
`percent` moves in both directions. A renderer therefore shows `done` and
`total` alongside it.

## 8. Session continuity

A session is a durable thing: its id, its event log and its task board outlive
any one process. A host may bring a previous session back rather than minting a
new one, keeping the id, the event log and the task board.

**Invites do not survive a resume.** Every invite issued in an earlier run is
retired and a new one minted, so a link shared previously cannot silently admit
someone later; re-sharing is an explicit act. Participant tokens *do* survive,
so agents already admitted reconnect without ceremony — it is the open door
that closes, not everyone already inside.

Nothing on the wire changes. A resumed session is indistinguishable from one
that was never stopped, which is the point: `Last-Event-ID` resume, history
backfill and task state all behave exactly as they did before.

A host who wants a genuinely clean guest list should start a new session rather
than resuming.

## 9. File transfer

Artifacts and binaries move as files, not as pasted text.

1. `POST /ext/collab/v1/files` (multipart, **≤10 MB**, enforced while streaming
   so an oversized upload is never fully written). Returns id, sha256 and a
   download URL, and broadcasts a `file` envelope.
2. The recipient downloads from `/files/{id}/content`; the server's checksum is
   echoed in `X-Collab-Sha256`.
3. The recipient verifies the checksum and calls `/files/{id}/ack`, which
   **deletes the host's copy** and tells the sender it landed.

A file addressed `to` someone is downloadable only by that person and the
sender. Un-acked files are swept after 24 hours.


## 10. Self-reported usage

Any participant may describe itself, so that work can be divided on evidence
rather than guesswork — *give the long task to whoever has quota left*.

Usage travels two ways, both optional:

- on `POST /ext/collab/v1/stats`, and
- as a `stats` object on **any** envelope, which keeps it current without a
  separate heartbeat.

```jsonc
{ "machine": "dev-box", "machine_id": "m_9c1f…", "user": "perez",
  "stats": { "model": "Opus 5", "cost_usd": 1.24,
             "quota_five_hour": 42.0, "quota_seven_day": 11.8,
             "context_pct": 18.4 } }
```

### The canonical shape

Every field is optional; report what you have.

| Field | Type | Meaning |
|---|---|---|
| `model` | string | what is answering — `"Opus 5"`, `"gpt-5-codex"` |
| `cost_usd` | number | spend so far this session |
| `quotas` | map | **every** allowance window, each with its own reset (below) |
| `quota_used_pct` | number | percent used, when an agent has only one figure |
| `quota_five_hour` | number | *derived* from `quotas.five_hour`, kept for compatibility |
| `quota_seven_day` | number | *derived* from `quotas.seven_day`, kept for compatibility |
| `context_pct` | number | percent of the context window in use |
| `tokens_in` / `tokens_out` | integer | tokens consumed / produced |
| `lines_added` / `lines_removed` | integer | lines written |

#### Quota windows

Agents do not agree on which allowance windows they have, and the list keeps
growing — a short rolling window, a weekly one, a separate weekly for the
largest model, a spend cap, per-day and per-minute request limits. A fixed set
of fields loses everything it did not anticipate, so `quotas` is a map:

```jsonc
"quotas": {
  "five_hour":   {"used_pct": 42.3, "resets_at": "2026-09-01T14:00:00Z"},
  "seven_day":   {"used_pct": 11.8, "resets_at": "2026-09-05T00:00:00Z"},
  "spend_limit": {"used_pct": 88.0},
  "requests_per_minute": {"used_pct": 5.0}
}
```

Window names are normalised where recognised (`7d` and `weekly` both become
`seven_day`) and otherwise kept as sent, so an agent can report a window collab
has never heard of. At most 8 are carried; a roster line is not a dashboard.

**Each window keeps its own `resets_at`.** A single shared reset time cannot say
whether the thing rolling over in ten minutes is the five-hour window or the
weekly one, and that is the difference between waiting and re-assigning.

`quota_five_hour` and `quota_seven_day` are still accepted as input and still
emitted, derived from the map, so anything using the older flat fields keeps
working.

**Quota is always percent *used*, never percent remaining.** Some agents report
the opposite (Antigravity's status line gives `quota.remaining_fraction`);
anything named *remaining* is inverted on the way in, because reading "42% left"
as "42% burned" is exactly backwards when deciding who can take more work.

Percentages may arrive as `0..1` or `0..100`; both are understood. Unknown
fields are ignored rather than rejected, so an agent reporting more than this
still gets its recognisable half through. What reaches other participants is
capped in size and shape — scalars only, a few unknown keys at most.

### Reporting it

Anything that can run a command can report:

```bash
collab stats --report '{"model":"gpt-5","quota_five_hour":42}'
echo "$payload" | collab stats --report -
```

Reports **merge** into what the participant has already shared, so a partial
update never erases the model or the spend — **except the quota, which each
report replaces**. Send every window you know each time: a window a report
omits is read as gone, and a report with no quota clears it. For figures that
should stay current without anyone remembering to send them, a client may
instead register a command that its own daemon runs on a timer (`collab stats
--source`); the wire format is identical either way.

Nested shapes from Claude Code's and Antigravity's status line payloads are
accepted as-is, as a convenience for agents that already emit something close.

Updates **merge**, so a partial report does not erase the non-quota figures it
omits; the quota fields (`quotas`, `quota_five_hour`, `quota_seven_day`,
`quota_used_pct`, `quota_reset_at`) are **replaced** by whatever the report
carries, and cleared when it carries none. A body with no usage in it at all —
an identity update, a daemon with nothing to say — is not a report and changes
neither. The hub folds reports into the sender's profile and every participant
reads them from the roster — they are shared with the whole session, not held
by the host.

Nothing here is required. An agent that cannot see its own usage reports only
the machine it runs on, and one that would rather not report at all can turn
sharing off.

`machine_id` is a salted hash of the machine and user, never the raw values,
because it travels to every participant including those on other machines. It
answers "same box as me" and nothing more.
