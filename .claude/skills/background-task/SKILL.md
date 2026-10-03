---
name: background-task
description: "Procedure for adding a background task type (anything that calls an AI or search provider, fetches a page, or runs longer than a moment), following the ten task-reliability rules. Use whenever work touches app/tasks/ or adds a button that starts long-running work."
---

# Add a background task type

The task runner's rules are in spec section 5, milestone M2. Every task type must honor all ten.

1. **Register the handler** in `app/tasks/handlers.py` with: a type name, a dedupe key built from type and target (for example `tailor_cv:application:42`), a timeout, and the service function it calls. Handlers are thin; logic lives in services.
2. **Budget first.** Before any external call, the handler goes through the usage service: reserve a usage record, honor warnings and blocks, and complete the record afterwards, success or failure.
3. **Heartbeat and cancellation.** Long steps call `ctx.heartbeat()`; between steps call `ctx.check_cancelled()`. Pass the cancel token to providers that support aborting.
4. **Atomic result.** Save the draft and mark the task succeeded in one transaction through `ctx.complete(...)`. Never overwrite earlier results; results are new drafts.
5. **Errors.** Raise the domain errors from spec 2.11; the runner records them and the UI shows the mapped message. Never retry automatically.
6. **UI.** The starting route enqueues and returns the task status fragment; the SSE `task-updated` event refreshes it (see the `htmx-ui` skill).
7. **Tests:**
   - duplicate enqueue returns the existing task;
   - success path stores the draft and the provenance (prompt id, version, model);
   - an injected failure after saving but before completion leaves no orphaned draft;
   - cancellation discards partial output;
   - the watchdog interrupts the task when the heartbeat stops (frozen clock);
   - a budget block stops the call before it is made and shows the reset date.
