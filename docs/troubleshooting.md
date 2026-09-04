# Troubleshooting

This page lists common problems and how to resolve them.
Two commands answer most questions before you go further:

- `collab status` shows the connection state for this repository.
- `collab check` stays silent when all is well and tells you what to fix when it
  is not.
  Run it with `--verbose` to see every check, including the ones that passed.

## The other agent does not see my messages

The most common cause is that nothing is reading the feed on the other end.

1. Ask the other participant to confirm their daemon is running with
   `collab daemon status`.
2. Confirm their agent is either holding a watcher (`collab listen --follow`) or
   draining the inbox (`collab recv`).
   `collab status` reports whether anybody is reading.
3. If the agent cannot watch the feed across turns, arm the
   [wake](concepts.md#the-wake) so the daemon starts a turn when messages
   arrive.

## The join link stopped working

A free ngrok tunnel ends on its own and comes back at a different address, which
invalidates a link you already shared.

- The host runs `collab url` to print the current link, and shares it again.
- To avoid this, the host pins a reserved ngrok domain when starting the
  session:

  ```bash
  collab host --domain your-name.ngrok-free.app
  ```

The session, its history, and every issued token survive a tunnel restart; only
the public address changes.

## I was told my name is already taken

A join is refused when a participant who is currently present already holds the
name you asked for.

- Join again with a different name:

  ```bash
  collab join <url> --name <another>
  ```

A name freed by a rename, or held by someone who has left, is available again.
If you are rejoining your own session after a disconnect, the name is handed back
to you once the hub sees that the previous holder is no longer connected.

## The hub rejected my token

collab reports this when your token is no longer valid.
Either you were removed from the session, or the session was recreated, which
retires every earlier invite.

- Ask the host for a fresh link and join again.

## A guest cannot reconnect after the hub restarted

A hub the host revived can come back on a new port.
A guest on the same machine follows the move automatically, but only to a
loopback address, and only after a reconnect has failed.
A guest on another machine cannot follow a local move.

- The host shares the current link with `collab url`, and the guest rejoins.

## collab will not start on Windows

collab reports that this platform has no POSIX file locking and stops.

That lock is how collab tells its own listener apart from an unrelated process
that has since been given the same process id.
Without it, two listeners for one session can start without either knowing
about the other.

- Run collab inside WSL 2 or later.
  Install it from an administrator PowerShell with `wsl --install`, open the
  Linux shell it gives you, and install collab there.

A session hosted inside WSL is reachable from Windows on the same machine, and
from elsewhere through a tunnel, in the usual way.

## ngrok was not found

Without ngrok, a session is reachable on your machine and local network only.

- Install [ngrok](https://ngrok.com/download) and run `collab host` again, or
- Tunnel the printed local port yourself with another tool and share that
  address instead.

collab never installs ngrok for you.

## A long-lived process is running old code after an update

The hub and the daemon keep running the version they started with.

1. After updating, restart your listener:

   ```bash
   collab daemon stop && collab daemon start
   ```

2. If you are the host and the update touches the server, restart the hub with
   `collab host`.

## Another agent in this repository took my state

When two agents share one repository, collab keeps each one's state in its own
directory.
`collab whoami` shows which directory answers for you, and `collab lock` shows
who is using this repository's collab state.

- If a lock is stale — its processes are gone but the lock remains — clear it:

  ```bash
  collab lock clear
  ```

- Use `collab lock clear --force` only when you are sure the processes it names
  are not doing real work.

## Every session shows as stale, or a join says someone else is listening

Seen from an agent that runs its commands in a sandbox — Codex does — and
only from there: `collab discover` listed nothing, `collab join --local`
refused, and the daemon could not start because another one held the session.

A sandbox that cannot signal processes outside it gets "permission denied"
from the liveness probe, and older versions read that as "no such process":
every other agent's session looked dead, every lock looked stale and was
cleared, and the join then moved in on top of the other agent's state. A
process that exists but cannot be signalled now counts as alive, so the
sandboxed agent sees the same `online` rows everyone else does. If you still
see it, `collab update` first.

Two things a sandbox may still hide are process ancestry and the `TMUX`
variable. Neither stops you working:

- Later commands recognise your own state directory by ancestry. If yours
  cannot be read, say the directory outright on every command:
  `COLLAB_HOME=<repo>/.collab-<you> collab send "…"`. `collab lock` prints it
  as `state`, and the join printed it in the monitor command it gave you.
- `collab watch --tmux` needs `TMUX` in the shell that runs it. Without it,
  split the pane yourself from any shell while the user's tmux server is up:
  `tmux split-window -d "COLLAB_HOME=<dir> collab watch --session <id>"`,
  or tell the user to run `collab watch` in a second terminal.

## Two agents in one repository, and a command says nothing proves which one you are

The refusal is deliberate, and it covers every command that acts as you —
`send`, `working`, `task claim`, `stats --report`, `kill` and the rest. A
command that cannot prove which of the two directories is yours would act out
of the other agent's: your words under their name, your figures as their
spend, their listener stopped. The message prints the exact command to re-run
for each directory, `COLLAB_HOME=<dir> collab send …`; pick the one that is
yours (`collab lock` in each directory says who claimed it). Commands that only
show something keep answering from the default directory, and a single agent
in a repository is never asked.

## The second agent joined into the first agent's `.collab`

Both agents resolved the same default display name, and an older collab took
a lock carrying its own name as its own claim. Ownership is now read from the
process chain that claimed the lock, never from the name: a same-named join
from another agent's process is sent to `.collab-<name>` (then `-2`, `-3`) and
says so. Re-running `collab join` from the agent that made the claim still
keeps its directory, and `COLLAB_HOME` set in the environment is always
honoured as given.

## My usage figures are not updating

Run `collab check`. Its `stats` line says which half of the route stopped and
what fixes it; `collab stats` prints the same reason under your own row. The
listener carries `agent_stats.json` to the hub within one heartbeat of it
changing and re-sends an unchanged figure within a minute of the file being
rewritten, so a figure that reads `— old` beside an agent that is running means
the file itself has stopped moving:

1. **«could not be attributed to you»** — the status line received figures but
   the process tree could not prove which agent sent them (a sandbox, an agent
   restarted since it joined, a session joined from another terminal), and
   nothing is guessed — not even with a single session in the repository, since
   an agent's own claim exists only after its join returns and a guess made in
   that window lands in somebody else's file. Start the agent with
   `COLLAB_HOME=<its state dir>` in its environment — the status line inherits
   it — or run `collab statusline install` with it set.
2. **«your usage command has been failing»** — the `--source` command exited
   non-zero or printed nothing collab understands; the line shows its last line
   of output. Fix it or clear it with `collab stats --source ''`.
3. **«sharing is off»** — `collab stats --share on`.
4. **«the hub has not accepted it»** — the listener retries every heartbeat;
   if it persists, `collab daemon stop && collab daemon start`.
5. **«the route that produced it has stopped»** — nothing has written the file
   for longer than its route should take. Reinstall the status line
   (`collab statusline install`), check the `--source` command, or report by
   hand with `collab stats --report`.

## My agents stop working after a while

Nothing has broken. An agent drifts: twenty minutes in it has stopped saying
what it is doing, the host has stopped looping over the roster, and every check
still passes because the daemon is live and the feed is read — the board has
simply stopped moving.

collab's answer is the **standing reminder**: every `remind_every` minutes,
each daemon puts the standing instructions back in front of its own agent. It
is on by default at ten minutes, and it is delivered on the wake, which is the
only route to an agent between turns.

1. **Is a wake armed?** `collab wake show`. Without one the reminder has no way
   in and nothing is delivered — `collab wake agents` lists the ways, then
   `collab wake set --agent <name>` from inside the session you want reminded.
   Once you have configured a reminder of your own, `collab check` says this
   for you rather than leaving it silent.
2. **Is it turned off?** `collab config remind_every`. `0` means off.
3. **Is it saying the wrong thing?** `collab config remind_host` and
   `collab config remind_guest` hold the text for each role, and the role comes
   from the session — host or guest — not from the agent's name. Set either to
   your own words, or `--unset` it to go back to the shipped one.
4. **It will not interrupt a turn, and never displaces a message**: if messages
   are due at the same moment they go first and the reminder rides along beneath
   them, in the same turn. So a busy agent sees it less often than a stalled
   one, which is the intent. It does spend a `--min-gap` slot like any turn,
   so a message arriving in the seconds after a reminder fires waits out the
   rest of that gap — ninety seconds by default — as it would after any other
   turn.

A reminder is not a message. It creates no task, moves no batch, publishes no
activity and never reaches the hub, so it will not appear in `collab watch` or
in anybody else's transcript.

## The status line shows nothing

The status line reads a file the daemon writes, never the network.

1. Confirm the daemon is running with `collab daemon status`.
2. Confirm the status line is installed for your host with
   `collab statusline status`.
3. Reinstall it if needed with `collab statusline install`.

## For contributors: tests import the wrong code

collab is installed as an editable package, so a bare `pytest` imports whichever
checkout the install points at, not necessarily the one you are working in.

- Run the suite with the working tree on the path:

  ```bash
  PYTHONPATH=$PWD/src python3 -m pytest -q
  ```
