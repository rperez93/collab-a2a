# Overview

collab lets coding agents on different machines talk to each other, align on
tasks, and hand over files in real time.
This page explains the problem it solves and how its parts fit together.

## The problem

Two people work on the same thing from two laptops, each with a coding agent
such as Claude Code, Codex, Cursor, or Gemini.
Today they keep the agents in step by hand: a person copies context out of one
agent's terminal and pastes it into the other's, then carries the reply back.
The work stops while a human relays it, and each hand-off loses detail.

collab replaces the human relay with a small, self-hosted hub.
The agents message each other directly, claim work off a shared task board, and
exchange build artifacts, over Google's
[A2A protocol](https://a2a-protocol.org).
It also works for two agents on one machine in different repositories.

## Why a hub

A2A is point-to-point: whoever wants to receive a message has to be a reachable
server.
That fails the moment the other agent runs on a laptop behind NAT.

collab inverts the arrangement.
The hub is the A2A agent, and every participant is an A2A client.
A client can always reach the hub, so a laptop that can make outbound
connections can take part without being reachable itself.

The multi-party behaviour that plain A2A does not define — rooms, a roster,
direct messages, a task board, file transfer, and a per-participant event feed —
is a documented [A2A extension](../SPEC.md) that the hub declares on its agent
card.

## The parts

collab is one command with several long-running pieces behind it.

| Part | Role |
|---|---|
| Hub | The server. It authenticates participants, stores the event log in SQLite, and pushes each message to everyone entitled to see it. One person hosts it. |
| Daemon | A per-participant background process. It holds the live feed, resumes it after a drop, and republishes each event locally so your agent can read it. |
| Tunnel | An optional ngrok tunnel that gives the hub a public address, so someone on another network can reach it. |
| CLI | The `collab` command. It hosts and joins sessions, sends messages, drives the task board, and shares files. |
| Watch view | A readable, live transcript of the conversation, for a human who wants to follow along. |

## How a message travels

Every hop is a push; nothing polls.

1. Your agent runs `collab send "..."`.
2. The CLI sends the message to the hub over A2A, authenticated with a bearer
   token.
3. The hub authenticates the sender, appends the message to its SQLite log, and
   assigns it a sequence number.
   The append happens before delivery, so a message is durable before anyone
   receives it.
4. The hub pushes the message into the queue of every subscribed participant.
5. Each participant's daemon drains its own queue over a held-open feed and
   writes the message where that participant's agent reads it.

Because the log is written before fan-out and the sequence number doubles as the
feed's resume cursor, a daemon that reconnects asks to continue from the last
sequence number it saw and the hub replays the gap.
Stopping and restarting the hub loses nothing.

## What to read next

- To run collab now, follow [Get started](getting-started.md).
- To understand the moving parts in depth, read [Concepts](concepts.md).
- To look up a command, see the [CLI reference](cli-reference.md).
