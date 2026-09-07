# Contributing

Thanks for taking a look. This is a small project with a sharp purpose: let
coding agents on different machines talk to each other over A2A without either
of them needing to be reachable from the internet.

## Getting set up

```bash
git clone https://github.com/rperez93/collab-a2a.git
cd collab-a2a
./install.sh
.venv/bin/python -m pytest -q
```

Everything runs from `.venv`. Nothing is installed globally, and `install.sh`
will never use `sudo` or touch system packages — if it cannot find a Python
≥3.10 it stops and tells you what to install.

## Running it against yourself

You do not need two machines. Three environment variables isolate everything a
profile owns, so one repo can hold several:

| Variable | Isolates |
|---|---|
| `COLLAB_HOME` | session state (normally `<repo>/.collab`) |
| `COLLAB_CONFIG` | global settings (name, stats sharing, viewer layout) |
| `COLLAB_PEERS_DIR` | the machine-wide session registry |

Set all three when running more than one profile, or they will share a name and
a peer registry and confuse each other:

```bash
export COLLAB_CONFIG=/tmp/cfg.json COLLAB_PEERS_DIR=/tmp/peers
COLLAB_HOME=/tmp/A .venv/bin/collab host --no-tunnel --name alice
COLLAB_HOME=/tmp/B .venv/bin/collab join 'http://127.0.0.1:PORT#INVITE' --name bob
COLLAB_HOME=/tmp/A .venv/bin/collab send "does this work?"
COLLAB_HOME=/tmp/B .venv/bin/collab watch --no-follow
```

`--no-tunnel` keeps ngrok out of the loop while you are iterating, and
`COLLAB_NO_UPDATE_CHECK=1` keeps the release check off your test runs.

Stop things by pid file rather than by pattern — `pkill -f collab.hub_main`
matches the shell you are typing it in, and kills that too:

```bash
kill "$(python -c "import json;print(json.load(open('/tmp/A/sessions/<id>/hub.json'))['pid'])")"
kill "$(cat /tmp/A/sessions/<id>/daemon.pid)"
```

## Layout

```
src/collab/
  protocol.py      the envelope and the extension's shared constants
  config.py        per-repo .collab/ resolution, names, session profiles
  cli.py           every command
  server/
    app.py         the FastAPI app: A2A routes + the extension
    hub.py         fan-out — one queue per connected participant
    store.py       SQLite; the append-only event log is the backbone
    events.py      the SSE feed and Last-Event-ID resume
    executor.py    bridges A2A SendMessage into the hub
    auth.py        invites, per-participant tokens, the bearer backend
    card.py        the Agent Card
    tunnel.py      ngrok detection
  peers.py         the machine-wide registry: local discovery, co-location
  update.py        release checks
  learnings.py     a fact one agent found, kept in the agent's own store
                   outside any checkout and grouped by repository
  hosttool.py      which coding tool is running this command, and whether
                   it can hold a watcher between turns
  compaction.py    typing an agent's own /compact into the pane its wake holds
  diagnostics.py   the optional local record of what the daemon and hub did
  atomic.py        replacing a file whole where more than one process writes it
  reboot.py        clearing records left by processes from before a restart
  owner.py         which agent started this, and whether it is still there
  quotas.py        asking an agent for its own allowance figures
  client/
    daemon.py      holds the feed, reconnects, writes the local inbox
    daemon_files.py  the pid, status and readers it writes down, read without it
    tui.py         the full-screen viewer
    onboard.py     the one-step join
    watch.py       the human-readable transcript
    bridge.py      localhost WebSocket bridge for Monitor
  statusline/      render + the additive installers
```

## Things worth knowing before you change something

**Anything we write into somebody else's host is tested by running that host.**
Status lines, hooks, shell snippets, `~/.tmux.conf` — a string we believe a host
will accept is a guess until that host has parsed it. The tmux status line
shipped `${COLLAB_STATUSLINE_TIMEOUT:-8}`, which is right for Claude Code
because that command really is handed to a shell, and which tmux rejects: its
own `${}` knows only `${NAME}`. tmux answered «invalid environment variable»
and **abandoned the rest of the file**, so the segment never drew and everything
below our block in the user's config stopped being read. The test beside it
asserted the block was one line and held the right words. It was, it did, and it
did not work.

So: `test_tmux_itself_accepts_what_we_write` hands the file to a real `tmux
source-file` and requires silence; `test_the_claude_code_hook_is_shell_a_shell_will_run`
pipes the block into `sh` with the JSON Claude Code sends and reads the line
back. Skip on the host being absent (`shutil.which`), never on it being
awkward. An assertion about the TEXT of an installed script is a supplement to
running it, never a substitute — and if a host cannot be run in CI, say so in
the test rather than asserting on a string and calling it covered.


**A file two processes write gets a private temp name.** `path.with_suffix(".tmp")`
is one name per directory, so two writers share it — and collab has two records
with two writers by design: `hub.json` (the tunnel watcher and `collab url
--rotate`) and `agent.lock` (`_take_lock` on every host and join, and the
daemon's heartbeat). Sharing the name gives one of two faults depending on the
size of the record. `hub.json` came back **unreadable in 1 run of 10** at its
real size, one writer having truncated the other's half-written document so the
mixture was renamed into place — and that is unrecoverable, because `load` reads
it as «no such session». `agent.lock` is written in one piece and never tore;
instead the `replace` itself raised, on **26.8%** of `acquire` calls with both
sides looping, and `_take_lock` does not catch, so `collab host` exited with a
traceback. Use `collab.atomic.scratch`, and `discard` on the failure path — a
unique name leaves a *new* file behind on every failure where a shared one left
the same file over and over.


**Identity is an id, never a display name.** Names change; anything that routes
or authorises on one breaks the moment someone renames themselves — which is
precisely the bug `tests/test_rename.py` exists to prevent coming back. Names
stay on the wire for humans and for clients that address by name, and the hub
resolves them, preferring whoever holds the name now.

**The event log is the contract.** `seq` is assigned on append, is monotonic,
and doubles as the SSE `id:`. Persist *before* fan-out — if you deliver an event
that is not yet durable, a reconnecting client can resume past a message that no
longer exists. Most of the resume tests exist to catch exactly that.

**`from` is never client-supplied.** The hub sets it from the authenticated
participant. Anything that lets a client choose its own sender is a security
bug, not a feature.

**Direct messages must be filtered on replay too**, not just on live delivery.
It is easy to add a new read path and forget; `test_replayed_dms_stay_private`
guards it.

**Anything written into someone else's config is additive and marked.** Skills,
instructions files, status line scripts, tmux config — all of it belongs to the
user, not to us. Insert a marker-delimited block, back the file up first, never
remove or reorder what is already there, and make re-running replace our block
rather than add a second. `tests/test_statusline_install.py` and
`tests/test_skills.py` exist to keep that true.

**Nothing writes a log on the caller's thread.** Every log this project keeps —
the plain `daemon.log` and `hub.log` lines and the structured diagnostic record
— goes on a bounded in-memory queue and is written by a thread that does nothing
else. It is not about speed: the write itself is about 10 µs on a healthy disk.
It is about *what the caller is exposed to*. The daemon logs from inside the
event loop that is holding the SSE feed, and on the direct path a filesystem
that stalls stalled the feed with it — a 9p mount over `/mnt/c`, a network
share, a disk waking up. So a new log call must reach `logging` (which
`daemon_files.setup_logging` has already put behind a `QueueHandler`) or
`diagnostics.log`, and never `open(...).write(...)` on a path in a hot loop.

Both queues are **bounded and drop their oldest** rather than growing or
blocking: `diagnostics.BUFFER_MAX` and `daemon_files.LOG_QUEUE_MAX`. A daemon
sits for days, and a queue that grows while the disk is unwritable turns the
fault it was reporting into a larger one. What is dropped is counted and the
count is written out, so a gap in the record says it is a gap. If you need to
read a record back in a test, `diagnostics.flush()` first — writing and reading
are two acts now.

**The diagnostic record holds events, never content.** It is written to be
pasted into a public issue. Pass classifications rather than free text, and note
what the logging bridge does and does not take: `diagnostics.Handler` records
that a warning fired, where, and the type of any exception with it — never the
formatted message, because a message is content. An exception's own text is
dropped for the same reason and its traceback kept, which is the part that
locates the bug.

**Measure the CPU and the memory of anything that reads from something you do
not control.** Not "does it work" — what does it cost when the other end
misbehaves? Both failures found in the quota probe were of that shape and
neither showed up in a passing test. `readline` on a pipe grows its buffer until
a newline arrives, with no ceiling: a server writing megabyte blobs and no
newline took the process to **10.5 GB resident in ten seconds**. Fixing that by
discarding instead then cost **93% of a core** for the whole deadline, on
something the daemon runs every two minutes.

So a reader of a pipe, a socket or a subprocess needs three limits, and they do
three different jobs: a cap on one record (memory), a cap on the whole exchange
(CPU), and a deadline that is enforced even while nothing is arriving — which a
blocking read cannot do on its own. Write the figures into the test, the way
`tests/test_a_quota_read_from_the_agent_itself.py` does: it asserts the wall
clock, the resident growth and the CPU spent, against a fake that floods and a
fake that goes silent. A limit with no measurement beside it is a guess.

And anything spawned must be bounded by the budget of whoever runs it. The
daemon gives a usage command twenty seconds and then SIGKILLs the shell — and
SIGKILL does not unwind, so a probe slower than that leaks the process it
started, every cycle, for ever. Start such a child in its own process group and
end the group, not the handle: what holds the pipe open may be something the
child forked.

**Every state file is read tolerantly, so that upgrades need no migration
step.** Unknown keys are dropped, missing keys take their default, and an
absent field means «cannot tell» rather than «false» — `lockfile.read` has
always filtered this way, `HubConfig.load` was fixed to, and `exclusive.parse`
reads `daemon.pid` in all three of the shapes it has had. That discipline is
what lets a release add a field without shipping code to rewrite anybody's
files, and it works in both directions: a newer collab reads an older file, and
an older collab keeps working when a newer one has written the file first. Two
versions coexist on one machine more often than you would think — a checkout
being tested beside the installed copy is the ordinary case.

`update.refresh_installed` is the only thing that runs after an upgrade, and it
re-runs INSTALLERS — the skills and the status line, which are copies in other
people's directories that `pip install` cannot reach. It is not a migration
hook and should not become one: it runs unattended, it must never fail the
update, and a half-applied state change that cannot fail loudly is worse than
no state change at all. If a genuinely destructive format change ever arrives —
a key that must be renamed rather than added, a file that must move — write a
maintained module in this tree, key it off a `schema` integer stored in the
file rather than off a version comparison, make it idempotent and safe to run
twice, and refuse to run it against state a live daemon or hub is holding open.
Never a script fetched or discovered at update time.

**A pid is not an identity, and a pid from another boot is nobody.** Everything
collab writes down about a process goes through `exclusive.Stamp` — the number,
the start time, and the boot it was recorded on — and is put to `Stamp.alive`
before it is believed and certainly before it is signalled. The kernel reuses
pids, `wsl --shutdown` restarts the counter at 1, and the files in the
repository survive the machine that wrote them. An unstamped record is trusted,
because an upgrade must not make every running process look like an impostor.

**Global settings belong to the person, session state to the repo.** A new
preference goes in `~/.config/collab/config.json` behind a getter and setter in
`config.py`, and gets a CLI flag — never ask anyone to edit that file by hand.

**Name the mechanisms before you build the thing that reaches an agent.** Every
agent reaches collab differently, and a feature built for the mechanism in front
of you is a feature most sessions never see. Claude Code holds its own monitor
and arms no wake — we tell it to. Codex has no status line and reads a thread.
Gemini and most of the rest are driven through a tmux pane. Some run in a
sandbox that cannot signal a process or prove its own ancestry, and some have no
tty at all. So when you add something that reaches an agent — a prompt, a
reminder, a status line, a check — write down which mechanisms it travels by and
which agents each one covers, *before* it is built. If it covers one, it is a
feature for one.

The standing reminder is the worked example. It shipped on the wake, which is
the one mechanism the most common agent here does not use, so the agent most
likely to be in a session was the only agent it never reached — and `collab
check` was quiet about it, because that warning was gated on the wake too. It
now travels by the monitor as well, with the daemon keeping one clock for both
so that an agent holding both routes is reminded once. See
`tests/test_reminder_on_the_monitor.py`.

Writing the list down is not enough on its own; every entry on it needs a test.
`tests/test_the_reminder_reaches_every_agent.py` asks the same question of each
route separately — with nothing unread at all, does the reminder's own text
reach what the agent actually reads? For a tmux pane that is the file the typed
line points at; for a Codex thread it is the queued message; for a fresh run it
is standard input, read directly or spliced into a shell argument. It also
holds the generalisation those last two rest on: the two shapes are asserted to
be every fresh-run recipe there is, so one added in a third shape fails a test
rather than quietly going uncovered.

The documentation version of the same mistake is a hand-written second copy of a
registry: `collab-configure`'s table of settings was copied out of
`config.settings()` once and was a setting short by the next release. If a list
of what something reaches is worth writing down twice, hold the copy to the
original with a test — `tests/test_docs_match_cli.py` and
`tests/test_skill_settings_match_the_registry.py` are the two that do it here.

**A budget belongs to the thing it was created to pace.** `settle` and
`min_gap` were written to pace how often other people's *messages* start a
turn. The standing reminder borrowed the same delivery, and by borrowing it
borrowed the counter behind that gate — so it quietly began spending a budget
that was never its, and a message landing a second after a reminder waited out
the remaining eighty-nine seconds of a gap it had not been given a turn for. So
when a new caller reuses an existing path, ask what state that path owns and on
whose behalf it is spent. If the answer is *somebody else's*, give the new
caller its own.

That is the worked example: the fix is `reminded_at` beside `last_attempt` —
one clock for the reminder's interval, one for the route's last attempt, and a
third, `messaged_at`, for the only turn `min_gap` is entitled to charge for. The
condition lives at the write, in `Waker._gap_spent_by`, so the gates stay
unconditional. See `tests/test_periodic_reminder.py`.

**The diagnostic log records events and never content.** `diagnostics.log` is
written to be pasted into a public issue, so nothing that reaches it may be a
line of a message, a participant's name, an invite, a token, an address, or a
path under the reader's home. The rule is kept at both ends and both ends
matter: callers pass classifications rather than free text, and `_safe` scrubs
whatever arrives anyway. Two places it costs something and is paid regardless —
a dropped feed records the exception's *type* and not its message, because an
httpx error carries the URL it was talking to; a failed wake records the exit
code and not the output, because a woken agent prints what it was woken about.
When you add an event, decide which of those two it is before you decide what
to put in it.

**The status line must never touch the network.** Hosts cancel an in-flight
status line script when the next update fires, so a network call there can stall
someone's whole status bar. It reads one local file and exits 0 — including when
collab is not running at all.

That goes for imports as well as calls. Everything that reads what the daemon
writes down — the pid file, `status.json`, the watchers directory — lives in
`client/daemon_files.py`, which imports nothing that opens a socket, and the
status line reads it from there rather than from `client/daemon.py`, which
carries httpx, websockets and asyncio for the daemon's own use. Reaching those
five helpers through the daemon module cost 89 of a 115 ms cold start, on every
prompt the host rendered, for a file read.
`tests/test_statusline_imports_no_networking.py` holds the renderer's import
graph off the network stack in a fresh interpreter, so it stays that way.

**Importing the CLI imports no networking either.** Only `host`, `join` and
`update` open a connection from the CLI process; `recv`, `send`, `status`,
`watch` and the rest read and write local files and ask the daemon. httpx is
imported where it is called — in `update.check`, `tunnel._all_tunnels` and the
`HubClient` methods that use it — and never at the top of a module `cli.py`
imports, because at the top of one it came to 80 ms of a 180 ms
`import collab.cli`, paid by every command that never went out. The two daemon
signals the CLI needs, `stop` and `stop_orphans`, are imported when `host`,
`join` or `daemon stop` actually reach for them.
`tests/test_cli_imports_no_httpx.py` asserts on the whole graph in a fresh
interpreter, so moving the import to the next module instead of out of the path
fails it too.

**The status line installers are additive, always.** A status line script is
shared ground; a typical one already hosts several other tools' segments. Insert
a marker block, keep every other byte, back up first, and make `uninstall`
restore the file exactly. There is a regression test built from a real machine's
script with three other tools in it — do not weaken it.

**A2A details that are easy to get wrong** (all verified against the installed
SDK, not the docs):

- JSON-RPC method names in 1.0 are gRPC-style — `SendMessage`,
  `SubscribeToTask` — *not* `message/send`. Those are the 0.3 names, which we
  also accept via `enable_v0_3_compat`.
- `A2A-Version: 1.0` must be sent, or a request is read as 0.3.
- Types are protobuf (`a2a.types.a2a_pb2`), not pydantic. `protocol_version`
  lives on `AgentInterface`, not on `AgentCard`.
- `EventQueue.enqueue_event` is a coroutine — awaiting it is not optional; a
  missing `await` hangs the request instead of failing.
- The SDK's REST binding mounts a greedy `/{tenant}` at the root, so our routes
  are registered *before* it.

**A name that implements somebody else's specification is read by that
specification.** This is a rule about removing code, and it was learned by
breaking it. A dead-code sweep found `HubClient.agent_card` with no caller
anywhere in the repository — true, and beside the point:
`/.well-known/agent-card.json` is A2A's discovery endpoint, the hub publishes
the card, and the client half of a protocol surface does not stop existing
because nothing here happens to ask for it. A third party writing against this
client must find the method where the specification says it is.

So before deleting anything unreferenced, ask which of these it is:

- **ours, and unused** — delete it, and say in the commit what claimed to read
  it, because a comment asserting a reader that does not exist is the more
  interesting half of the find;
- **somebody else's protocol** — A2A routes and client methods, the well-known
  paths, the card's fields, the JSON-RPC method names above — keep it, and give
  it a docstring saying why it has no caller, or the next sweep reaches the same
  wrong conclusion;
- **a constant naming a value in the data model** — a state, a kind, a key
  somebody else can see. `batch.OPEN` was removed because only `CLOSED` is
  compared against here, a batch being open by being not closed. But `"open"`
  is the schema's `DEFAULT`, it rides in every `--json` payload, and the bundle
  describes the board in those words: the vocabulary is read whether or not
  this spelling of it is. Keep both ends of a pair, and pin them to whatever
  writes them — `tests/test_a_state_constant_is_read_by_its_data_model.py`
  asserts the constant against the schema so the two cannot drift;
- **a re-export** — check where its readers moved to before deciding. Several
  here exist so an older import path still answers;
- **a check nobody wired up** — `Inbox.gaps` reports sequence numbers that
  never arrived, and no other surface can: `last_seq` says how far the log
  reaches, the unread count says what has not been read, and a conversation
  with one message missing reads perfectly. It was removed for having no
  caller. The answer was to give it one — it is in `collab check` and
  `collab logs` now. Before deleting something that can detect a fault, ask
  whether the fault is worth detecting.

A protocol name kept this way needs a test, because a docstring does not make
the reference graph find a reader and the next sweep is run by a tool, not by
whoever read the docstring. See
`tests/test_the_a2a_client_surface_is_not_dead_code.py`.

## Releasing

Bump the version in `pyproject.toml` and `src/collab/__init__.py`, tag it, and
create a release. Do **not** move a tag that has already been published — cut
the next patch version instead.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Streaming tests run against a real uvicorn server rather than Starlette's
`TestClient`, which does not behave with SSE. If you are adding behaviour to the
feed, follow that pattern — it is the honest test.

Please add a test that fails before your change and passes after. A bug fix
without a test tends to come back.

## Style

Match what is there: type hints, `from __future__ import annotations`, and
comments that explain *why* rather than restating the code. The existing
comments are a reasonable guide to the level of explanation that earns its
place.

## Reporting a security issue

Please do not open a public issue for anything involving tokens, authentication,
or access control. Open a private security advisory on the repository instead.
