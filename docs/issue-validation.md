# Issue validation against PR #87

The baseline for this check is commit `27c975e` on PR #87. The reports were read
through GitHub's issue API. Reproductions use temporary synthetic hubs, inboxes
and profiles; no participant's live state or credentials were inspected.

| Report | Baseline verdict | Resolution |
|---|---|---|
| [#84](https://github.com/rperez93/collab-a2a/issues/84): comments and task/project details silently cut after 4,000 characters | Still present in all three HTTP write paths | Accept and preserve complete text through 8,000 characters, including whitespace and multibyte characters; refuse longer content with HTTP 413 before writing. The finite limit matches chat. |
| [#85](https://github.com/rperez93/collab-a2a/issues/85): global sequence gaps count other participants' DMs as lost | Still present; local sequence arithmetic cannot establish visibility | Local gaps are explicitly unverified samples. Authenticated replay uses the hub's existing viewer predicate, advances across hidden pages and never returns another pair's DMs. |
| #85: subscriber overflow silently discards rows while keeping the feed open | Reproduced by filling a subscription's bounded queue | Close that subscriber with a retry reason, log and count the disconnect, and resume from durable history. Serialize persist-and-deliver so concurrent publisher completion cannot invert delivery order. |
| #85: known missing events cannot be fetched back | Still present; recv and watch only read local storage | Bounded explicit replay restores absent rows without changing existing read marks or regressing the daemon's resume cursor. Durable repair notices identify old rows behind a worker's cursor. |
| #85: activity churn dominates monitors | Default compact notices already suppress activity in PR #87; full transcript delivery remained unfiltered by kind | Preserve compact default; add `listen --kind chat --kind task` and `--no-activity` for explicitly chosen full delivery. |
| [#86](https://github.com/rperez93/collab-a2a/issues/86): fresh agent processes select the wrong identity | Partly addressed by PR #87's workspace/agent namespaces; explicit custom-home selection and ambiguous same-actor fallback remained | Persist foreground selections under the stable agent-session namespace. Validate saved selection state, refuse stale bindings and ambiguous unproven diagnostics, and honor explicit home overrides. |

The reported 79 missing events in the historical live session are **not**
attributed to queue overflow. The issue itself could not establish that cause;
this work proves and fixes the independently reproducible silent-drop path.
The new overflow counter starts at zero when the hub restarts and is available
in its snapshot. It is evidence for future incidents, not retrospective proof.

## Verification and budgets

`tests/test_issue_recovery.py` executes all three content endpoints, authenticated
viewer-filtered replay, late recovery and deduplication, an actual SSE generator's
overflow/reconnect lifecycle, fresh-process actor bindings and stale/ambiguous
selection. The existing live HTTP streaming tests cover reconnection and private
message visibility. Repair markers and recovered rows commit atomically; a later
page failure leaves the marker available for the worker/main notice integration.

The recovery HTTP reader stops at 8 MiB per page, uses a five-second I/O timeout
and checks a fifteen-second deadline between incoming chunks. Compressed pages
are refused to keep that byte limit meaningful. A repair checks its thirty-second
budget between pages and reads at most 500 pages of 200 scanned rows. One in-flight
page may extend the overall budget; this is a foreground command, not a daemon
operation. Incomplete repairs return their cursor so the operator can continue.

The regression tests measure wall time, process CPU and peak resident growth
against a flooding replay stream, a silent local HTTP peer, 10,000 publications
to a stalled subscriber and a billion-number global-sequence jump. The asserted
bounds are budgets rather than claims of production measurements. An unknown or
older hub without authenticated replay fails explicitly; no local-only fallback
is reported as a successful repair.

## Deliberate limits

- Previously truncated content cannot be reconstructed from the clipped store.
- A local gap sample cannot prove delivery loss. Only hub-filtered history can
  establish which visible rows are missing.
- Repaired rows append to JSONL in recovery order; their original sequence and
  timestamps remain intact. Existing rows are not appended a second time.
- Durable selection needs a stable host session ID. Without one, explicit
  `--home` or `COLLAB_HOME` remains the recovery route; there is no global current
  agent selector and no auto-adoption of legacy repository state.
- A repair notice is acknowledged only after its downstream notification is
  durable and deduplicated. The CLI/worker integration must preserve this order.


## Version 2 compatibility boundary

This worktree also prepares the `2.0.0` release boundary. Both peers advertise
Collab protocol major 2 and a stable package version at least `2.0.0` in major 2.
The guest checks the public compatibility endpoint before submitting an invite;
the host refuses incompatible joins before consuming an invite or changing a
participant. Prereleases, missing advertisements and unsupported future majors
fail explicitly. `tests/test_v2_compatibility.py` verifies the refusal ordering,
single-use invitation preservation, unchanged rejoin credentials and successful
stable 2.x peers. This boundary is separate from A2A's transport version.
