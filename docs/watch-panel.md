# Reading the live participant panel

![Expanded participant measurements in a real 110-column tmux pane](../assets/participant-panel.png)

The image above renders a real tmux terminal buffer with synthetic participants.
The [38-column capture](../assets/participant-panel-narrow.png) shows the same
measurements wrapped for a narrow side panel.

The participant panel has a compact summary per person. Select a person with
`J` / `K`, then press `Enter` or `Space` to open their measurements. `Right`
opens and `Left` closes. Click a participant header to do the same; clicking a
measurement selects its person. The selected header spans the available width, with connection state pinned
at its right edge. Compact cards use at most two measurement rows. Expanded
cards label main-agent and worker measurements separately: two columns at
88 content columns or wider, and stacked groups below that width.

Arrow keys, Page Up / Page Down and the mouse wheel scroll. In the combined
view, `Tab` changes pane and the wheel scrolls the pane under the pointer.
`End` / `G` in the conversation returns to new messages. Participant controls
leave conversation following and message disclosure unchanged.

In the combined view, `+` grows the roster and `-` shrinks it by five percentage
points. Drag the `CONVERSATION` divider to preview the split; releasing saves
`watch_roster_size`, the same setting edited by the CLI and settings TUI. Only
the completed gesture writes configuration. In a tmux split, use tmux's native
pane resize controls. Layouts are cached independently of scroll offsets, and
mouse motion is requested only during a drag.

`v` toggles all participant details; `f` cycles the saved fields, usage fields,
and identity fields. The matching `[v details]` and `[f saved]` controls on
the participant heading also respond to clicks; narrow panes use `[v]` / `[f]`.
The controls stay visible while scrolling. These choices affect only this viewer.

Save defaults with `collab config watch_participant_details true` and
`collab config watch_participant_fields context,quota,cost,subagents,worker`.
The ordered fields are `model`, `context`, `quota`, `cost`, `subagents`, `worker`
and `location`. A saved change reaches an open pane immediately and replaces
its temporary field and disclosure choices. Expanded rows wrap to the pane
width; scroll to reach the remaining rows in a narrow tmux panel.

Unknown measurements say «unknown». Zero appears only when reported as zero.
Quota percentages are usage, not remaining allowance. Cost rows distinguish
estimated and reported amounts and name their scope; an absent kind or scope
is identified as unknown. A provider observation's timestamp determines its
age – a fresh heartbeat cannot make an old observation current. Quota readings
have their own age, so a newer token report cannot make an old allowance
reading current. Expanded
rows show the source; unavailable source or observation time stays unknown.

Coding subagents show active and total counts when known. The communication
worker has its own state, model, turns, attempts, pending items, errors, tokens
and cost. Its usage is displayed separately from the participant's session
usage because the two sources may overlap. A disabled worker does not imply
that coding subagents are absent.


### Separate worker allowance and context

![Main and worker usage in the actual terminal](../assets/worker-telemetry.png)

The worker's quota, relationship to the main account, context, cache writes and
measurement ages are independent rows. «Unknown» is distinct from zero. The
configured next model and historical usage model are labeled separately. These
synthetic figures demonstrate layout; they are not a real account measurement.

In a narrow transcript, a dated header now moves above the body when it would
leave fewer than twelve text columns. Crossing midnight no longer causes
ordinary messages to fold just because the date appeared.

See the [theme engine](theme-engine.md) for semantic colors, safe glyphs and
participant spacing, indentation and column preferences. Cyberpunk and Matrix
are built-in options; appearance reloads without restarting the viewer.
