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
