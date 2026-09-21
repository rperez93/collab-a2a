# CLI reference

Generated from the command parser. Existing commands remain available; `collab config --tui` adds the interactive editor.

## Commands

- [host](#host)
- [kill](#kill)
- [sessions](#sessions)
- [lock](#lock)
- [join](#join)
- [send](#send)
- [learn](#learn)
- [listen](#listen)
- [recv](#recv)
- [who](#who)
- [rooms](#rooms)
- [task](#task)
- [project](#project)
- [batch](#batch)
- [wake](#wake)
- [remind](#remind)
- [logs](#logs)
- [issue](#issue)
- [compact](#compact)
- [new](#new)
- [check](#check)
- [working](#working)
- [idle](#idle)
- [activity](#activity)
- [stats](#stats)
- [rules](#rules)
- [discover](#discover)
- [update](#update)
- [watch](#watch)
- [demo](#demo)
- [file](#file)
- [status](#status)
- [url](#url)
- [kick](#kick)
- [name](#name)
- [theme](#theme)
- [agent](#agent)
- [whoami](#whoami)
- [config](#config)
- [color](#color)
- [fold](#fold)
- [worker](#worker)
- [daemon](#daemon)
- [capacity](#capacity)
- [skills](#skills)
- [statusline](#statusline)

## host

```text
usage: collab host [-h] [--name NAME] [--port PORT] [--bind BIND]
                   [--focus FOCUS] [--home FOLDER] [--title TITLE]
                   [--domain DOMAIN] [--no-tunnel] [--no-daemon] [--keep]
                   [--no-update-check] [--update] [--fresh]
                   [--resume [SESSION_ID]]

options:
  -h, --help            show this help message and exit
  --name NAME           your display name (default: your global collab name)
  --port PORT           port to bind (default: a free one)
  --bind BIND           interface to bind; 0.0.0.0 exposes it on your LAN
  --focus FOCUS         what you are working on, shown to others
  --home FOLDER         explicit state folder (default: private external
                        repo/agent namespace)
  --title TITLE         a name for the session, shown to everyone
  --domain DOMAIN       a reserved ngrok domain, so the URL survives a tunnel
                        restart
  --no-tunnel           skip ngrok even if installed
  --no-daemon           do not start listening
  --keep                leave it running when this agent quits
  --no-update-check     do not check for a newer collab first
  --update              install a newer collab without asking, if there is one
  --fresh               start an empty session instead of resuming this repo's
                        last one
  --resume [SESSION_ID]
                        resume a previous session (the most recent by default)
```

## kill

```text
usage: collab kill [-h] [--disarm] [--all] [--purge] [--yes] [session_id]

positional arguments:
  session_id  which session (default: the one you are in)

options:
  -h, --help  show this help message and exit
  --disarm    also turn off the wake armed for this session
  --all       every session this repo hosts
  --purge     also delete its conversation and task board, for good
  --yes, -y   required with --purge
```

## sessions

```text
usage: collab sessions [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json
```

## lock

```text
usage: collab lock [-h] [--force] [--json] [{show,clear}]

positional arguments:
  {show,clear}  show the lock (default), or clear it

options:
  -h, --help    show this help message and exit
  --force       clear a lock whose processes are still alive
  --json
```

## join

```text
usage: collab join [-h] [--agent AGENT] [--local] [--name NAME]
                   [--focus FOCUS] [--home FOLDER] [--no-daemon] [--keep]
                   [--no-update-check] [--update]
                   [url]

positional arguments:
  url                the join URL (https://host#INVITE), or a session id with
                     --local

options:
  -h, --help         show this help message and exit
  --agent AGENT      which of this repo's agents is joining
  --local            join a session running on this machine, no link needed
  --name NAME        your display name
  --focus FOCUS      what you are working on, announced on arrival
  --home FOLDER      explicit state folder (default: private external
                     repo/agent namespace)
  --no-daemon        do not start listening
  --keep             leave it running when this agent quits
  --no-update-check  do not check for a newer collab first
  --update           install a newer collab without asking, if there is one
```

## send

```text
usage: collab send [-h] [--room ROOM] [--to TO] [--thread THREAD]
                   [--session SESSION]
                   text [text ...]

positional arguments:
  text

options:
  -h, --help         show this help message and exit
  --room ROOM        room to post in (default: your current room)
  --to TO            send privately to one participant
  --thread THREAD    thread id to reply in
  --session SESSION  act on this session id instead of the current one
```

## learn

```text
usage: collab learn [-h] [--body TEXT] [--tags A,B] [--source URL]
                    [--note TEXT] [--tag T] [--limit N] [--want N]
                    [--wait [SECONDS]] [--all] [--json] [--session SESSION]
                    {add,list,search,read,used,sync} [TEXT ...]

positional arguments:
  {add,list,search,read,used,sync}
                        record one, list them, search them, read one, say one
                        helped, or ask the others for theirs
  TEXT                  the title for `add`, the slug for `read` and `used`,
                        the words for `search`

options:
  -h, --help            show this help message and exit
  --body TEXT           with `add`: the detail; `-` reads it from stdin
  --tags A,B            with `add`: tags for the area
  --source URL          with `add`: where it was established
  --note TEXT           with `used`: what it helped with
  --tag T               with `search`: only this tag
  --limit N             how many to show
  --want N              with `sync`: how many each agent should send
  --wait [SECONDS]      with `sync`: wait and report what arrived, instead of
                        returning at once
  --all                 every repository in the store, not only this one
  --json
  --session SESSION     act on this session id instead of the current one
```

## listen

```text
usage: collab listen [-h] [--follow] [--json] [--kind KIND] [--no-activity]
                     [--room ROOM] [--limit LIMIT] [--delivery {notice,full}]
                     [--replay REPLAY] [--mine-too] [--exit-when-idle]
                     [--session SESSION]

options:
  -h, --help            show this help message and exit
  --follow, -f          keep streaming as events arrive
  --json                emit JSON events or notices instead of formatted lines
  --kind KIND           only this event kind; repeat to select several
  --no-activity         omit activity events from full delivery
  --room ROOM           only this room
  --limit LIMIT         how many past events to print
  --delivery {notice,full}
                        coalesced inbox notices (default), or full event text
  --replay REPLAY       replay this many past events first
  --mine-too            include your own messages
  --exit-when-idle      stop if the daemon is not running
  --session SESSION     act on this session id instead of the current one
```

## recv

```text
usage: collab recv [-h] [--repair] [--repair-after REPAIR_AFTER] [--wait WAIT]
                   [--limit LIMIT] [--json] [--peek] [--mine-too]
                   [--session SESSION]

options:
  -h, --help            show this help message and exit
  --repair              recover missing visible events from authenticated hub
                        history
  --repair-after REPAIR_AFTER
                        resume a bounded repair after this sequence
  --wait WAIT           seconds to wait for a message
  --limit LIMIT
  --json
  --peek                do not mark as read
  --mine-too
  --session SESSION     act on this session id instead of the current one
```

## who

```text
usage: collab who [-h] [--json] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --json
  --session SESSION  act on this session id instead of the current one
```

## rooms

```text
usage: collab rooms [-h] [--create CREATE] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --create CREATE    create a room with this name
  --session SESSION  act on this session id instead of the current one
```

## task

```text
usage: collab task [-h] [--id ID] [--detail DETAIL] [--project ID] [--url URL]
                   [--number NUMBER] [--files [PATH ...]] [--room ROOM]
                   [--open] [--json] [--session SESSION]
                   {propose,claim,update,complete,fail,cancel,list,show,comment,pr,pr-remove,move}
                   [title]

positional arguments:
  {propose,claim,update,complete,fail,cancel,list,show,comment,pr,pr-remove,move}
  title                 title when proposing, or the text when commenting

options:
  -h, --help            show this help message and exit
  --id ID               task id for show/claim/update/complete
  --detail DETAIL       longer description
  --project ID          with propose, the project to file it under; with move,
                        where to file it — `--project ''` takes it out of the
                        one it is in. `move` changes nothing else: not the
                        state, not the owner, not the batch
  --url URL             with pr/pr-remove: the pull request's url
  --number NUMBER       with pr: its number, if the url does not end in one
  --files [PATH ...]    with claim: the files you are about to touch
  --room ROOM
  --open                list only open tasks
  --json
  --session SESSION     act on this session id instead of the current one
```

## project

```text
usage: collab project [-h] [--id ID] [--owner NAME] [--detail DETAIL]
                      [--archived] [--json] [--session SESSION]
                      {propose,list,show,assign,update,delete,comment,archive,unarchive}
                      [title]

positional arguments:
  {propose,list,show,assign,update,delete,comment,archive,unarchive}
  title                 title when proposing, or the text when commenting

options:
  -h, --help            show this help message and exit
  --id ID               project id for
                        show/assign/update/delete/comment/archive/unarchive
  --owner NAME          who it belongs to; --owner '' leaves it unassigned
  --detail DETAIL       longer description
  --archived            with list: include retired projects
  --json
  --session SESSION     act on this session id instead of the current one
```

## batch

```text
usage: collab batch [-h] [--json] [--session SESSION]
                    [{start,status,close}] [name]

positional arguments:
  {start,status,close}
  name                  what the batch is, when starting one

options:
  -h, --help            show this help message and exit
  --json
  --session SESSION     act on this session id instead of the current one
```

## wake

```text
usage: collab wake [-h] [--to KIND] [--expect-command NAME] [--expect-pid PID]
                   [--agent NAME] [--target ID] [--notify NOTIFY]
                   [--delivery {notice,full}] [--settle SECONDS]
                   [--min-gap SECONDS] [--timeout SECONDS] [--yes] [--json]
                   [--session SESSION]
                   [{show,set,off,agents,deliver}] [COMMAND ...]

positional arguments:
  {show,set,off,agents,deliver}
  COMMAND               with `set`: the command to run; the messages arrive on
                        its standard input

options:
  -h, --help            show this help message and exit
  --to KIND             with `deliver`: how to reach the session. Run by the
                        daemon, not meant to be typed
  --expect-command NAME
                        with `deliver`: the program that was in the pane when
                        this was armed; type into nothing else
  --expect-pid PID      with `deliver`: the process that was in the pane when
                        this was armed; refuse to type into any other
  --agent NAME          use the known recipe for this agent (`collab wake
                        agents` lists them)
  --target ID           which live session to reach — a Codex thread id, a
                        tmux pane. Taken from your own environment if unset
  --notify NOTIFY       optional command told after each turn
  --delivery {notice,full}
                        compact inbox notices (default), or full batch text
  --settle SECONDS      how long to let a burst finish before waking
  --min-gap SECONDS     never start two turns for messages closer together
                        than this
  --timeout SECONDS     kill a woken turn that runs longer than this
  --yes                 arm a command that is not one of the reviewed recipes;
                        it will run unattended
  --json
  --session SESSION     act on this session id instead of the current one
```

## remind

```text
usage: collab remind [-h] [--host] [--guest] [--file PATH] [--session SESSION]
                     [{show,set,add,clear,now}] [text ...]

positional arguments:
  {show,set,add,clear,now}
                        show what is in force; set replaces it; add appends a
                        paragraph; clear gives back the shipped one; now asks
                        for a delivery immediately
  text                  the text, for set and add

options:
  -h, --help            show this help message and exit
  --host                the host reminder; defaults to this session's role
  --guest               the guest reminder; defaults to this session's role
  --file PATH           read the text from a file, or - for stdin
  --session SESSION     act on this session id instead of the current one
```

## logs

```text
usage: collab logs [-h] [--lines N] [--follow] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --lines N          how many of each to show (default 40)
  --follow           keep printing what is appended, until Ctrl-C
  --session SESSION  act on this session id instead of the current one
```

## issue

```text
usage: collab issue [-h] [--out FILE] [--session SESSION] [{draft}]

positional arguments:
  {draft}

options:
  -h, --help         show this help message and exit
  --out FILE         write it here instead of beside the diagnostics
  --session SESSION  act on this session id instead of the current one
```

## compact

```text
usage: collab compact [-h] [--agent NAME] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --agent NAME       which agent in this checkout, when it holds more than one
  --session SESSION  act on this session id instead of the current one
```

## new

```text
usage: collab new [-h] [--agent NAME] [--all] [--agree ID] [--decline ID]
                  [--reason TEXT] [--status] [--json] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --agent NAME       which agent in this checkout, when it holds more than one
  --all              ask everyone to start a fresh session; they agree and
                     every daemon acts on its own agent
  --agree ID         agree with an open proposal, once your own work is at a
                     boundary
  --decline ID       decline one, with --reason
  --reason TEXT      why, on a proposal or a decline
  --status           what is open, who has answered, and how long it has
  --json             with --status, emit raw JSON
  --session SESSION  act on this session id instead of the current one
```

## check

```text
usage: collab check [-h] [--json] [--verbose] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --json
  --verbose, -v      show every check, including the ones that passed
  --session SESSION  act on this session id instead of the current one
```

## working

```text
usage: collab working [-h] [--files [PATH ...]] [--task TASK]
                      [--session SESSION]
                      [what ...]

positional arguments:
  what                the objective, in one line

options:
  -h, --help          show this help message and exit
  --files [PATH ...]  the files you are touching
  --task TASK         the task id this belongs to, if there is one
  --session SESSION   act on this session id instead of the current one
```

## idle

```text
usage: collab idle [-h] [--session SESSION] [note ...]

positional arguments:
  note               optional: what you are waiting on

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
```

## activity

```text
usage: collab activity [-h] [--json] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --json
  --session SESSION  act on this session id instead of the current one
```

## stats

```text
usage: collab stats [-h] [--json] [--share {on,off}]
                    [--provider {codex,claude,opencode,cursor}]
                    [--report JSON] [--clear-quota] [--agent NAME]
                    [--probe NAME] [--source CMD] [--interval SECONDS]
                    [--session SESSION]

options:
  -h, --help            show this help message and exit
  --json
  --share {on,off}      share your own usage with the session (default: on)
  --provider {codex,claude,opencode,cursor}
                        native snapshot format for --report
  --report JSON         report your own usage as a JSON object, or '-' for
                        stdin — this is how any agent shares figures; a report
                        that carries 'quotas' replaces your quota, one that
                        does not leaves it; quota_five_hour alone is a map of
                        that one window, a statement about it and about no
                        others; a field set to null is erased for everyone,
                        which is the only way to take a wrong one off the
                        roster
  --clear-quota         tell everyone you no longer have quota information
                        (posts an empty 'quotas' map) — use it when your tool
                        has stopped showing you a quota
  --agent NAME          arm the usage command collab ships for this agent, for
                        tools that will only tell a program rather than a
                        shell (codex)
  --probe NAME          ask that agent for its quota once and print the JSON —
                        what --agent arms, and what to run to see why it is
                        not answering
  --source CMD          a shell command printing your usage as JSON; collab
                        runs it on a timer so the figures stay current by
                        themselves (pass '' to clear)
  --interval SECONDS    how often to run --source (default 120)
  --session SESSION     act on this session id instead of the current one
```

## rules

```text
usage: collab rules [-h] [--default]

options:
  -h, --help  show this help message and exit
  --default   only the shipped rules, verbatim — `> COLLAB.md` seeds a repo
```

## discover

```text
usage: collab discover [-h] [--all] [--json]

options:
  -h, --help  show this help message and exit
  --all       include stale records
  --json
```

## update

```text
usage: collab update [-h] [--check] [--yes]

options:
  -h, --help  show this help message and exit
  --check     only report, do not install
  --yes, -y   do not ask
```

## watch

```text
usage: collab watch [-h] [--tmux] [--vertical] [--percent PERCENT]
                    [--no-follow] [--plain] [--limit N]
                    [--layout {split,tmux,chat,roster}] [--roster-size PCT]
                    [--roster-position {top,bottom,left,right}] [--save]
                    [--demo] [--session SESSION]

options:
  -h, --help            show this help message and exit
  --tmux                open it in a new tmux pane instead of here
  --panel [{auto,tmux,ghostty,iterm2}]
                        open a tmux or native macOS terminal split
  --vertical            with --tmux, split below instead of to the right
  --percent PERCENT     with --tmux, how much of the window to give the pane
  --no-follow           print and exit
  --plain               scrolling text instead of the full-screen view
  --limit N             how much history to open with (default: 5 in the full
                        view, 200 plain)
  --layout {split,tmux,chat,roster}
                        split: one window · tmux: two real panes ·
                        chat/roster: one of them only (default: your saved
                        setting)
  --roster-size PCT     how much room the roster gets (default 30)
  --roster-position {top,bottom,left,right}
                        where the roster pane goes in the tmux layout
  --save                remember these layout choices as your default
  --demo                open the viewer on a simulated conversation, with no
                        session and nothing on the network
  --session SESSION     act on this session id instead of the current one
```

## demo

```text
usage: collab demo [-h] [{agent,watch}]

positional arguments:
  {agent,watch}  agent: the fake agent's terminal alone · watch: the viewer on
                 the simulated session alone (as `watch --demo`) · neither:
                 both, side by side

options:
  -h, --help     show this help message and exit
```

## file

```text
usage: collab file [-h] [--to TO] [--room ROOM] [--output OUTPUT] [--keep]
                   [--json] [--session SESSION]
                   {send,get,list,rm} [target]

positional arguments:
  {send,get,list,rm}
  target               path to send, or file id to get/remove

options:
  -h, --help           show this help message and exit
  --to TO              share privately with one participant
  --room ROOM
  --output, -o OUTPUT  directory to save into (default: here)
  --keep               do not confirm receipt, so the host keeps its copy
  --json
  --session SESSION    act on this session id instead of the current one
```

## status

```text
usage: collab status [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json
```

## url

```text
usage: collab url [-h] [--rotate] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --rotate           retire every invite issued so far, mint a new one and
                     print it — the session stays up and nobody already in it
                     is disconnected
  --session SESSION  act on this session id instead of the current one
```

## kick

```text
usage: collab kick [-h] [--session SESSION] name

positional arguments:
  name

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
```

## name

```text
usage: collab name [-h] [--agent AGENT] [value]

positional arguments:
  value

options:
  -h, --help     show this help message and exit
  --agent AGENT  which agent directory this belongs to, when the repo has more
                 than one
```

## theme

```text
usage: collab theme [-h] [-l] [-n NAME] [--from THEME] [--check] [value]

positional arguments:
  value

options:
  -h, --help      show this help message and exit
  -l, --list      list the themes in your themes folder
  -n, --new NAME  write a new theme file you can edit
  --from THEME    start the new file from this theme instead of the one you
                  have on
  --check         report anything mis-written in your theme files
```

## agent

```text
usage: collab agent [-h] [--color COLOR] [--rename RENAME] [--force]
                    {create,update,delete,list} [name]

positional arguments:
  {create,update,delete,list}
  name

options:
  -h, --help            show this help message and exit
  --color COLOR         the colour others see it in
  --rename RENAME       update: its new display name
  --force               delete: do not ask, even with a terminal
```

## whoami

```text
usage: collab whoami [-h]

options:
  -h, --help  show this help message and exit
```

## config

```text
usage: collab config [-h] [--unset] [--edit] [--json] [--tui] [key] [value]

positional arguments:
  key         the setting to show or change
  value       its new value

options:
  -h, --help  show this help message and exit
  --unset     put a setting back to its default
  --edit      edit a setting interactively; offer an external editor for text
  --json
  --tui       interactive keyboard/mouse settings editor

Actions:
  collab config                         list settings, values and defaults
  collab config KEY                     show one setting and its actions
  collab config KEY VALUE               set directly
  collab config KEY --unset             restore the default
  collab config KEY --edit              edit interactively (terminal required)
  collab config --tui                   browse/edit; r resets to default
  collab config editor nano             choose vim, nvim, nano, or a command
  collab config editor 'nvim -f'         editor arguments are supported
  collab config editor --unset          use VISUAL, then EDITOR, then vi

Text editing asks whether to use an external terminal editor or inline input.
Save and quit the external editor to return the temporary file's UTF-8 content.
The CLI validates and saves; the TUI keeps a draft until Enter/Save.
TUI controls: arrows/j/k select, / searches, Enter edits, r resets, ? helps,
Esc cancels a draft, q quits. Editor commands run without a shell.
```

## color

```text
usage: collab color [-h] [--agent AGENT] [value]

positional arguments:
  value          a hex colour like #00cccc, or 'none' to clear it

options:
  -h, --help     show this help message and exit
  --agent AGENT  which agent directory this belongs to, when the repo has more
                 than one
```

## fold

```text
usage: collab fold [-h] [value]

positional arguments:
  value       a number of lines, 'off' to never fold, or 'auto' to let the
              theme decide

options:
  -h, --help  show this help message and exit
```

## worker

```text
usage: collab worker [-h]
                     {start,off,status,pending,reply,context,send,stats} ...

positional arguments:
  {start,off,status,pending,reply,context,send,stats}

options:
  -h, --help            show this help message and exit
```

## worker start

```text
usage: collab worker start [-h] [--session SESSION]
                           --agent {codex,claude,opencode,cursor,command}
                           [--model MODEL] --scope SCOPE
                           [--command WORKER_COMMAND]

options:
  -h, --help            show this help message and exit
  --session SESSION     act on this session id instead of the current one
  --agent {codex,claude,opencode,cursor,command}
  --model MODEL         pin model id; omission follows worker_PROVIDER_model
                        from collab config
  --scope SCOPE         explicit authority delegated to the conversation
                        worker
  --command WORKER_COMMAND
                        with --agent command: JSON argv array; reads a JSON
                        turn on stdin
```

## worker off

```text
usage: collab worker off [-h] [--session SESSION]

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
```

## worker status

```text
usage: collab worker status [-h] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
  --json
```

## worker pending

```text
usage: collab worker pending [-h] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
  --json
```

## worker reply

```text
usage: collab worker reply [-h] [--session SESSION] decision_id text

positional arguments:
  decision_id
  text

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
```

## worker context

```text
usage: collab worker context [-h] [--session SESSION] text

positional arguments:
  text

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
```

## worker send

```text
usage: collab worker send [-h] [--session SESSION] --to TO [--room ROOM] text

positional arguments:
  text               exact message to queue for reliable worker delivery

options:
  -h, --help         show this help message and exit
  --session SESSION  act on this session id instead of the current one
  --to TO            explicit participant name
  --room ROOM        optional room for the addressed message
```

## worker stats

```text
usage: collab worker stats [-h] [--session SESSION] [--report JSON]
                           [--provider {canonical,codex,claude,opencode,cursor}]
                           [--source CMD] [--interval SECONDS]
                           [--quota-scope {shared_account,independent,unknown}]
                           [--json]

options:
  -h, --help            show this help message and exit
  --session SESSION     act on this session id instead of the current one
  --report JSON         worker usage snapshot, or - for stdin
  --provider {canonical,codex,claude,opencode,cursor}
                        native snapshot adapter; canonical for a normalized
                        report
  --source CMD          local worker usage command; empty clears
  --interval SECONDS    source interval, 10–86400 seconds; default 120
  --quota-scope {shared_account,independent,unknown}
                        whether the worker allowance shares the coding agent's
                        account
  --json                JSON output (also the default)
```

## daemon

```text
usage: collab daemon [-h] [--disarm] [--keep] [--session SESSION]
                     [{start,stop,status}]

positional arguments:
  {start,stop,status}

options:
  -h, --help           show this help message and exit
  --disarm             with `stop`: also turn off the wake for this session
  --keep               with `start`: leave it running when this agent quits
  --session SESSION    act on this session id instead of the current one
```

## capacity

```text
usage: collab capacity [-h] [--report JSON] [--participant PARTICIPANT]
                       [--limit LIMIT] [--active ACTIVE] [--reserve RESERVE]
                       [--percent-per-child PERCENT_PER_CHILD]
                       [--task-budget TASK_BUDGET] [--max-age MAX_AGE]
                       [--window WINDOW] [--json] [--session SESSION]

options:
  -h, --help            show this help message and exit
  --report JSON         canonical stats snapshot or - for stdin; otherwise use
                        own session
  --participant PARTICIPANT
                        session member name or ID
  --limit LIMIT         native host maximum concurrent children
  --active ACTIVE       current active native children
  --reserve RESERVE     percentage points kept for the main agent
  --percent-per-child PERCENT_PER_CHILD
                        calibrated percentage points per child in each quota
                        window
  --task-budget TASK_BUDGET
                        explicit per-child task budget, alternative to
                        calibration
  --max-age MAX_AGE     maximum observation age in seconds
  --window WINDOW       applicable quota window; repeat to select several
  --json                structured output (also the default)
  --session SESSION     act on this session id instead of the current one
```

## skills

```text
usage: collab skills [-h] [--name NAME] [--description DESCRIPTION]
                     [--after AFTER] [--limit LIMIT] [--session SESSION]
                     [--agent NAME] [--copy] [--force] [--all] [--json]
                     {install,uninstall,status,publish,shared,show,withdraw}
                     [target]

positional arguments:
  {install,uninstall,status,publish,shared,show,withdraw}
  target                selected SKILL.md path or shared publication ID

options:
  -h, --help            show this help message and exit
  --name NAME           published skill name override
  --description DESCRIPTION
                        published skill description override
  --after AFTER         shared inventory page cursor
  --limit LIMIT         shared inventory page size
  --session SESSION     act on this session id instead of the current one
  --agent NAME          just this agent (default: every one detected here)
  --copy                copy the skills instead of symlinking them
  --force               replace skills of the same name that are already there
  --all                 with status, also list agents not installed here
  --json
```

## statusline

```text
usage: collab statusline [-h] [--agent {auto,claude-code,tmux,generic}]
                         [--scope {global,project}] [--plain] [--json]
                         [--cwd CWD] [--width WIDTH]
                         {install,uninstall,status,render}

positional arguments:
  {install,uninstall,status,render}

options:
  -h, --help            show this help message and exit
  --agent {auto,claude-code,tmux,generic}
                        which host to wire up (default: detect)
  --scope {global,project}
  --plain               render without ANSI colour
  --json                render structured output
  --cwd CWD             render the session for this directory
  --width WIDTH         truncate the rendered line
```
