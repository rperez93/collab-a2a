# Editing settings in the terminal

![The settings editor filtered to worker controls](../assets/settings-panel.png)

This is a real tmux terminal buffer rendered with a disposable configuration.

Run `collab config --tui` to browse and change the same settings available from
`collab config <key> <value>`. The editor needs an interactive terminal.

Use the arrow keys or `j` / `k` to select a setting, or click its row. The mouse
wheel moves through the list. `/` or **Search** filters names and descriptions;
press Enter to finish searching. `?` or **Help** shows the controls.

Enter or **Edit** opens a draft. Type the value, use Home / End and the arrow
keys to move the caret, and Ctrl-U to clear it. Ctrl-N inserts a newline for
rules and worker instructions. Space or **Toggle** changes a boolean draft.
Enter or **Save** validates and writes the value. Escape or **Cancel** discards
the draft. Invalid values stay in the editor with the registry's error message.
Commands are edited as text; saving a configured command lets its usual runtime
consumer use it.

`r` or **Reset** shows the default and asks for confirmation before applying it.
A star beside a setting means its effective value differs from its default.

Changes saved by another terminal appear automatically. Saving rereads the
selected value; if it changed while the draft was open, the editor keeps that
external value and asks you to cancel and reopen. Unrelated external changes
are preserved by the setting's normal writer. Most settings reload immediately;
name and colour updates to an active session run after closing the editor.

Filter for `worker` to adjust its default provider models, budget, timing and
instructions. Filter for `watch_participant` to choose the panel's field order
and initial expansion. Filter for `stats_prices` to enter exact model rates as
JSON, in USD per million tokens, for example:

```json
{"example-model": {"input": 1, "output": 2, "cached_input": 0.1}}
```

These example rates only illustrate the format. Estimates require the exact
model identifier and your supplied prices.
