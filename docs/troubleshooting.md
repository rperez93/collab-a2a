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
