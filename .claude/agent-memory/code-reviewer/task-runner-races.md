---
name: task-runner-races
description: Race conditions and probes found reviewing the async task runner, watchdog and cancel paths (M2.8-M2.12); read when a change touches app/tasks or TaskService finish/cancel/claim
metadata:
  type: project
---

Moved from MEMORY.md on 2026-10-10 to keep the index short. Verify names against the code before citing them.

- Async task runners: probe (1) a handler that returns while stop() runs (cancel lands in the to_thread finish, conditional INTERRUPTED wins, result lost), (2) a handler raising CancelledError itself (done-callback drops it, row stuck running), (3) one failed claim (queue waits for an unrelated notify).
- "X before first claim" ordering tests that spy on claim_next only bite by race (recovery moved after runner.attach: step1 caught 2/3, lifespan 0/3); require a spy at the hand-off (runner.attach/_attach_tasks) asserting committed state there.
- Multi-step "commit then act" loops (watchdog: interrupt_stale per type, then cancel handlers): a raise on a later step skips the action for rows already committed, and they never come back (no longer running), so a hung handler keeps its slot; act per step. Also `col < ?` on a nullable column never matches NULL rows. Check "floor" constants actually bound what they claim (CONNECTION_TIMEOUT is not a run timeout).
- Commit-in-worker-thread awaited via bare `to_thread` (ctx.complete): a cancel at the await abandons a still-running commit; if the handler swallows it, the runner's own finish can race the commit (stop() commits nothing first). Shield and await the thread's result before re-raising.
- Event-driven cancel/notify keyed on an in-memory registry (runner `_handlers`): a notice published between the claim commit and registration (`_spawn`) finds no entry and is lost; probe by wrapping claim_next to request_cancel in-thread. Same-row retries: finishes/heartbeats keyed on id only can land on attempt N+1 (match attempt).
