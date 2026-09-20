# Reading the live participant panel

![Expanded participant measurements in a real 90-column tmux pane](../assets/participant-panel.png)

The image above renders a real tmux terminal buffer with synthetic participants.
The [38-column capture](../assets/participant-panel-narrow.png) shows the same
measurements wrapped for a narrow side panel.

The participant panel has a compact summary per person. Select a person with
`J` / `K`, then press `Enter` or `Space` to open their measurements. `Right`
opens and `Left` closes. Click a participant header to do the same; clicking a
measurement selects its person. A selected header is highlighted.

Arrow keys, Page Up / Page Down and the mouse wheel scroll. In the combined
view, `Tab` changes pane and the wheel scrolls the pane under the pointer.
`End` / `G` in the conversation returns to new messages. Participant controls
leave conversation following and message disclosure unchanged.

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
