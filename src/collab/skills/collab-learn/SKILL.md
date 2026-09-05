---
name: collab-learn
description: Look up what this repository has already taught the agents working on it, record something the next one will need, say when a learning actually helped, and ask the other agents in the session for theirs. Use when starting a task in an unfamiliar repository, when stuck on something that smells like a known pitfall, when the user asks "what do we know about X" or "did anyone already solve this", when you have just found something out the hard way, when somebody says "record this for the others" or "share what I learned", and when joining a session for a repository you hold no learnings for.
---

# What this repository has already taught somebody

A session is a conversation, and a conversation is the wrong shape for a fact.
Something one agent worked out at four in the afternoon is a hundred messages
back by five: invisible to the agent that joins tomorrow, and to you after a
compaction. Every session in a repository ends up rediscovering the same
handful of things.

So collab keeps them. Not in the repository — in a store belonging to **this
agent**, outside every checkout, holding what it has learnt about each
repository it has worked on and grouped by a key that two machines agree on.
Your learnings for this repository are the same set of facts the other agent
has for it, if the two of you have synced.


## Running collab

Examples here say `collab`. Use whichever of these resolves — check once, at
the start, and use the same form throughout:

```bash
command -v collab || ls .venv/bin/collab
```


## 1. How to look

Do this **before** starting a task, not after being surprised by something.

```bash
collab learn list                      # the index, most used first
collab learn search kafka retention    # a few words
collab learn search --tag infra        # or a whole area
collab learn read <slug>               # one of them, in full
```

`list` and `search` print one line each: the slug, the title, how many agents
have found it useful, and — when the words matched in the body rather than the
title — the line they matched. `search` takes a few words rather than a
sentence, and a half-typed last word still matches.

**Read one with `collab learn read <slug>`, never by opening the file.** Two
reasons, and the second is the one that matters. The file has fifteen lines of
frontmatter in front of two lines of content, which is noise in your context.
And a file you open directly is counted by nothing: collab cannot see it, so
the learning looks unused to every other agent, and the ones that are actually
carrying the repository sink down the list.

Empty output is not a fault. It means nobody has recorded anything for this
repository yet, and `collab learn sync` is how you ask the others.


## 2. How to say one helped

```bash
collab learn used <slug> --note "avoided the retention trap in the consumer"
```

Run it **right after** the learning actually did something: you applied a rule
it stated, you avoided a pitfall it named, you reproduced a bug it described.
Not when you read it and thought it looked useful.

Reading and using are counted separately on purpose. Reading one costs nothing
and proves nothing; an agent that applied it and found it true is the only
thing that can say it was worth writing. `used` is what ranks the index, so it
is how the next agent finds the three learnings that matter among the thirty
that exist. A `--note` is worth the eight words: it says what it helped with.


## 3. How to record one

```bash
collab learn add "The staging bucket needs the eu-west key, not the default" \
  --body "Found by trying both: the default key returns 403 on PUT only.
Applies to staging; production uses the default." \
  --tags infra,staging
```

What makes a good one:

- **A title that states the fact**, not the topic. "The staging bucket needs
  the eu-west key" is a learning; "staging bucket" is a filing cabinet.
- **A body that says how it was established and where it applies.** The next
  agent has to be able to tell whether your finding covers its case, and
  "found by trying both" is what lets it decide.
- **Tags for the area**, so `--tag` finds it later.

**When NOT to record one.** A fact the code already states — a constant, a
type, something the README says — is better read from the code, and a copy of
it in a learning is a copy that will go stale. A one-off ("the test was flaky
that afternoon") is not a fact about the repository. And **never** a secret, a
token, a customer name or anything personal: these files are synced to every
other agent in the session.

The command returns immediately. Your daemon writes the file and publishes it
in the background, so recording something never costs you a turn.


## 4. How to get the others'

```bash
collab learn sync            # ask everyone in the session
collab learn list            # what arrived
```

Do this **on joining a session for a repository you hold no learnings for** —
`collab host` and `collab join` tell you which case you are in. Every other
agent answers with its most-used learnings for this repository, sent directly
to you rather than to the room. It returns at once; the answers land over the
next few seconds.

**What leaves your machine, exactly.** Only the learnings for the repository
**this session is in**. Your store holds every repository you have worked on,
and a sync request cannot name one — the responder uses its own session's
repository and ignores anything the request says about it. So joining a session
about repository A never publishes what you know about B, whoever asks and
however they ask.


## 5. Where it all is

```bash
collab config learnings_dir          # the folder, outside every repository
collab learn list --all              # every repository in the store
```

Nothing is written into the checkout, so nothing appears in a diff. Setting
`learnings_dir` to an empty string turns the whole feature off.


## What not to do

- **Do not open the files directly.** Uncounted, and mostly frontmatter.
- **Do not record what the code already says.** It will go stale and the code
  will not.
- **Do not record a secret.** Everything here is shared with the session.
- **Do not `used` something you only read.** The count is the one signal that
  separates a learning that carries the repository from one that was written
  once and never helped anybody.
- **Do not wait for `sync`.** It returns at once by design; carry on and check
  `collab learn list` in a minute.
