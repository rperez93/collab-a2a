---
type: Interface
title: The command surface
description: Every subcommand collab accepts at version 1.20.2, read out of the argument parser itself rather than transcribed.
resource: ../../src/collab/cli.py
tags: [cli, commands, reference]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: parser-run
    resource: ../../src/collab/cli.py
    title: The parser, walked and every subcommand's --help captured
    last_modified: 2026-09-01T23:21:22Z
  - id: docs-test
    resource: ../../tests/test_docs_match_cli.py
    title: tests/test_docs_match_cli.py — every documented flag must exist
  - id: cli-ref
    resource: ../../docs/cli-reference.md
    title: docs/cli-reference.md — the full flag-by-flag reference for a human
stale_after: 2026-10-01T00:00:00Z
---

# How this list was produced

By building the parser and printing `--help` for the top level and for every
subcommand, at commit `f9abc76`.[^parser-run] The descriptions below are the
parser's own, verbatim.

It carries the short `stale_after` from the policy in
[a fact that was true when it was recorded](/stale-facts.md), because it is
derived from a version. There is a second guard as well: every
`collab <command> --flag` written anywhere in this bundle is checked against
the live parser by `tests/test_okf_bundle.py`, which extends the same list the
`docs/` directory is held to.[^docs-test]

# The commands

| Command | What the parser says it does |
|---|---|
| `host` | start a session and print a link to share |
| `kill` | end a session (its data is kept unless `--purge`) |
| `sessions` | sessions this repo has hosted before |
| `lock` | who is using this repo's collab state |
| `join` | join a session — with no arguments, the one running on this machine |
| `send` | send a message |
| `listen` | stream events as lines (arm a Monitor on this) |
| `recv` | drain unread messages, optionally waiting |
| `who` | who is in the session and what they are doing |
| `rooms` | list or create rooms |
| `task` | the shared task board |
| `batch` | a batch of work, and how much of it is done |
| `wake` | let the daemon start a turn for an agent that cannot watch the feed itself |
| `check` | run on a loop: silent when all is well, says what to fix when it is not |
| `working` | say what you are doing, so nobody asks |
| `idle` | say you have stopped, and are free for work |
| `activity` | who is working, and on what |
| `stats` | what each agent reports about its own usage |
| `discover` | collab sessions running on this machine |
| `update` | check for, and install, a newer collab |
| `watch` | a readable live transcript of the conversation |
| `file` | share files and artifacts without pasting them as text |
| `status` | connection status for this repo |
| `url` | reprint the join line (host only) |
| `kick` | remove a participant (host only) |
| `name` | show or set this agent's display name |
| `theme` | how the conversation looks |
| `agent` | create, update, delete and list agents |
| `whoami` | this agent's name, colour and state directory |
| `color` | show or set the colour others see you in |
| `daemon` | manage the listener |
| `skills` | teach your coding agents to use collab |
| `statusline` | the Claude Code status line segment |

# The sub-verbs

Several commands take a positional verb rather than a flag:

| Command | Verbs |
|---|---|
| `lock` | `show`, `clear` |
| `task` | `propose`, `claim`, `update`, `complete`, `fail`, `cancel`, `list`, `show` |
| `batch` | `start`, `status`, `close` |
| `wake` | `show`, `set`, `off`, `agents`, `deliver` |
| `file` | `send`, `get`, `list`, `rm` |
| `agent` | `create`, `update`, `delete`, `list` |
| `daemon` | `start`, `stop`, `status` |
| `skills` | `install`, `uninstall`, `status` |
| `statusline` | `install`, `uninstall`, `status`, `render` |

# Flags worth an agent knowing

Most commands that act on a session accept `--session`, to act on a session id
other than the current one, and `--json`.

- `collab host --no-tunnel` hosts without ngrok. `collab host --fresh` starts
  an empty session instead of resuming this repository's last one.
  `collab host --no-daemon` suppresses the listener.
- `collab join --local` joins a session running on this machine with no link.
- `collab listen --follow` is the stream to arm a Monitor on.
  `collab recv --wait 60` is the poll for an agent that cannot hold one.
- `collab task claim --files <paths>` declares what is about to be touched.
- `collab kill --purge` also deletes the conversation and the task board, and
  requires `--yes`.
- `collab wake set --agent <name>` uses a reviewed recipe; arming a command
  that is not one of them needs `--yes`.
- `collab stats --report <json>` is a whole integration.
  `collab stats --source <cmd>` puts it on a timer.

For the exhaustive flag-by-flag list, read
[docs/cli-reference.md](../../docs/cli-reference.md), which is generated from
the same parser and written for a person.[^cli-ref]

# What a fresh session prints

Hosting a session with no tunnel against a scratch state directory produced,
among other things, the join line:[^parser-run]

```
Share this one line with the other person
  collab join http://127.0.0.1:45759#Nsf9byEESEVulkn4Mbf1KWQh41omU1urfhTfTUN_IIw
```

The invite is in the URL fragment, which is why it never reaches a request line
or a proxy log. See [the trust model](/operating/security-model.md).

[^parser-run]: The parser, walked and every subcommand's --help captured
[^docs-test]: tests/test_docs_match_cli.py — every documented flag must exist
[^cli-ref]: docs/cli-reference.md — the full flag-by-flag reference for a human
