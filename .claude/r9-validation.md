# R9 validation

- 110 focused task-pickup, documentation, and settings checks passed.
- Real HTTP CLI integration covers join offers, no automatic claim, task claim/completion, immediate completion hint, and opt-out.
- Existing bounded FIFO/oversized/deep JSON, lease/retry/concurrent consumer, ledger contention, and capacity freshness tests passed.
- Required gpt-6-astra review found no blocking findings; final artifact is r9-adversarial-review.md.

## Live shared-session evidence

Read one authenticated snapshot from session s_793f992d as arma, using only arma's own saved profile. With pickup enabled in a temporary isolated configuration, the real snapshot contained one unfinished task owned by arma and the branch correctly suppressed another offer. No persistent setting/session/task mutation, daemon, or extra polling occurred.

10,000 checks took 0.7462 seconds wall and 0.7192 seconds CPU. Process max RSS was 333452 KiB after loading the networking stack; the loop added 0 KiB to the high-water mark. An earlier measurement (0.501 seconds) used the machine's existing pickup opt-out, so it measured only that early exit and is superseded for ownership-check performance.

The actual R9 task was completed in the shared session using this branch. It transitioned activity to idle and correctly emitted no next-work hint because the machine's persistent `task_auto_pickup` setting is false. That preference was left unchanged. Positive prompt behavior and both opt-out branches are covered by the real-HTTP CLI integration tests.

Terminal-state filtering is defensive for alternate/cached task payloads: the current hub already emits only open tasks. No normal-path permanent-suppression bug is claimed.

Final full suite: **3760 passed, 7 failed, 4 skipped** in 383.06 seconds. Six failures reproduce on unchanged base (ps argv fallback and five tmux cases). One batch-daemon ready-refresh timeout passed on isolated rerun (1 passed, 3.22 seconds); that test also passes isolated on base. All R9 checks passed. The full suite is not green.
