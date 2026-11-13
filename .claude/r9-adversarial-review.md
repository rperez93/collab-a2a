# R9 adversarial review

Reviewed the uncommitted diff in `arma-r9-pickup`: `task_pickup.py`, foreground `cmd_join` output, tests, rules, and skills/docs. No implementation changes or participant/session access were performed.

## Status

**No blocking findings.** The foreground offer is conditional on the opt-in setting, a current batch with eligible submitted/unowned work, and no unfinished owned task. It uses the already authenticated join/current snapshot, issues no extra request, reserves nothing, and outputs fixed local instructions plus a numeric count. No peer titles, descriptions, IDs, or other prose become instructions.

The background change preserves fresh snapshot/heartbeat requirements, explicit activity, the configured idle delay, and fresh known native capacity while working. Unknown owned task states remain conservative blockers. The four terminal states match `Store.tasks(open_only=True)`.

## Verification

- `tests/test_task_pickup.py`: **33 passed**.
- `git diff --check`: passed.
- Reviewed join call placement: the offer uses the snapshot already returned by onboarding, optionally refreshed by the existing authenticated request; no new polling was added.
- Existing competing-consumer, lease/retry, ledger contention, stale status, bounded snapshot read, capacity freshness, and no-reservation tests continue passing.
- New coverage checks terminal states, unfinished/unknown owned states, opt-out, wrong batch, archived projects, immediate foreground offering without idle inference, and no peer-prose injection or snapshot mutation.

## Nonblocking qualification and follow-up

The normal `Hub.snapshot` implementation already emits `Store.tasks(open_only=True)`, which excludes completed, canceled, failed, and rejected tasks. Therefore the terminal-state eligibility change is defensive for alternate/cached payloads; these tests alone do not establish a currently reachable permanent suppression bug with normal fresh hub snapshots. Describe this accurately in release notes unless a separate reproducer exists.

A narrow CLI integration test could additionally assert that a successful `cmd_join` prints the offer once when eligible and suppresses it when `task_auto_pickup=false`. The helper tests and inspected three-line call site are sufficient for this review; this is additional regression protection, not a merge blocker.

## Final re-review

Reviewed the added completion-boundary output and live CLI regression. **No blocking findings in the final diff.** The next-work hint is inside the successful complete/fail/cancel branch, requires the returned task ID to match locally recorded current activity, and respects `task_auto_pickup`. It consists entirely of fixed local text, does not claim that tasks actually remain available, and makes no new request. It asks the agent to inspect the board before claiming suitable work within the accepted goal.

The new parametrized HTTP test exercises CLI join, explicit claim, and completion with pickup enabled and disabled. It verifies that an offer leaves all task ownership unchanged and that completion returns local activity to idle. This addresses the suggested CLI integration coverage above. Documentation now explicitly qualifies terminal filtering as defensive for cached/alternate board payloads, addressing the release-note qualification.

Final local recheck: **33 passed, 2 deselected** (the two HTTP integration variants require socket access unavailable in this review sandbox); `git diff --check` passed. The implementation agent reports **110 focused/documentation tests passed**, including the HTTP variants. Its full-suite run remains in progress; this review does not claim a completed full-suite result.
