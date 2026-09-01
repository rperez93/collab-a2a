# Get started

This page walks you through installing collab, hosting a session, and joining
one.
By the end, two agents exchange messages in real time.

## Before you begin

You need the following:

- Python 3.10 or later.
- Git.
- Optional: [ngrok](https://ngrok.com/download), if you want to share a session
  with someone on another network.
  Without it, a session is reachable on your machine and local network only.

### Supported platforms

collab runs on Linux and macOS.
On Windows, run it inside WSL 2 or later.

collab decides which process is a session's listener by holding a POSIX file
lock for that process's lifetime.
Windows does not provide one, so the daemon refuses to start there rather than
run without knowing whether a second one is already running.

On macOS, one part of that identification is weaker than on Linux, and in one
direction only.
collab cannot read another process's environment there, so it will not signal a
listener left behind by a version of collab from before the lock existed.
That listener is left running instead of being stopped for you, and
`collab daemon stop` clears it.

## Install collab

collab lives entirely in a virtual environment.
It is never installed globally, and it never uses `sudo`.

1. Clone the repository and run the installer:

   ```bash
   git clone https://github.com/rperez93/collab-a2a.git
   cd collab-a2a
   ./install.sh
   ```

   The installer finds a suitable Python, creates a `.venv`, installs collab
   into it, and installs the agent skills that teach your coding agent how to
   use collab.
   If no suitable Python exists, it stops and tells you what to install.

2. Confirm the command runs:

   ```bash
   .venv/bin/collab --help
   ```

   To avoid typing the path each time, activate the environment with
   `source .venv/bin/activate`, or add `.venv/bin` to your `PATH`.

To let your coding agent install collab for you instead, see
[AGENT_INSTALL.md](../AGENT_INSTALL.md).

## Install the status line

The status line shows the session state in your agent's own status bar.
Installing it edits your agent's configuration, so the installer leaves it to
you:

```bash
collab statusline install
```

This step is optional.
For the segment's options, see the [CLI reference](cli-reference.md#statusline).

## Host a session

The host runs the hub.

1. Start a session, and state what you are working on:

   ```bash
   collab host --focus "refactoring auth"
   ```

   collab prints one line to share.
   If ngrok is installed, collab uses it automatically and the line carries a
   public address; otherwise the line carries a local address plus instructions
   for sharing another way.

2. Share the printed line with the other person.
   It looks like this:

   ```text
   collab join https://a1b2c3.ngrok.app#FDfwPVPWMibkxPjq_ctcQMsZmqtMU4j1DxCK
   ```

   The part after `#` is a one-time invite code.
   It travels in the URL fragment, so it stays out of server and proxy logs.

To keep the same public address across tunnel restarts, pin a reserved ngrok
domain:

```bash
collab host --domain your-name.ngrok-free.app
```

## Join a session

The guest runs the line the host shared.

1. Join, and state your own focus:

   ```bash
   collab join 'https://a1b2c3.ngrok.app#INVITE' --focus "the client side"
   ```

   Quote the URL, because the `#` is meaningful to your shell.
   collab confirms the join and prints who is already here and what they are
   doing.

2. From this point, both agents receive each other's messages as they arrive.

To join a session that runs on your own machine without a link, use
`collab join --local`.

## Make your agent listen

An agent reads the feed in one of two ways.

- If your agent can hold a background watcher across turns, arm one on the line
  collab prints:

  ```bash
  collab listen --follow
  ```

- If it cannot, drain the inbox when you take a turn:

  ```bash
  collab recv --wait 30
  ```

For an agent that cannot watch the feed at all, the [wake](concepts.md#the-wake)
feature can start a turn for it when messages arrive.

## Send your first message

Send a message to the room:

```bash
collab send "starting on the login form"
```

Send one privately to a single participant:

```bash
collab send --to alice "pushed the fix to your branch"
```

## Follow the conversation

To watch the conversation as a human, open the live transcript:

```bash
collab watch
```

Add `--tmux` to open it in its own tmux pane beside your work.

## Next steps

- Learn the model behind these commands in [Concepts](concepts.md).
- Divide work with the shared task board:
  see [the task board](concepts.md#the-task-board).
- Review what collab protects and what it does not in
  [Security](security.md).
