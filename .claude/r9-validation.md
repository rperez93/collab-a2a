# R9 validation

- 110 focused task-pickup, documentation, and settings checks passed.
- Real HTTP CLI integration covers join offers, no automatic claim, task claim/completion, immediate completion hint, and opt-out.
- Existing bounded FIFO/oversized/deep JSON, lease/retry/concurrent consumer, ledger contention, and capacity freshness tests passed.
- Required gpt-6-astra review found no blocking findings; final artifact is r9-adversarial-review.md.

## Live shared-session evidence

Read one authenticated snapshot from session s_793f992d as arma, using only arma's own saved profile. The real snapshot contained two unfinished tasks owned by arma. The branch correctly suppressed another foreground offer. No session/task mutation, daemon, or extra polling occurred.

10,000 checks against that snapshot took 0.501 seconds wall and 0.4858 seconds CPU. Process max RSS was 323128 KiB after loading the networking stack; the check loop added 0 KiB to the high-water mark. These measurements describe the guard, not a claimed reduction in total process memory.

Terminal-state filtering is defensive for alternate/cached task payloads: the current hub already emits only open tasks. No normal-path permanent-suppression bug is claimed.

Full suite is running; results will be appended before marking ready.
