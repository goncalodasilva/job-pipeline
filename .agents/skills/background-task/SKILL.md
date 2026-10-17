---
name: background-task
description: "Procedure for adding a background task type (anything that calls an AI or search provider, fetches a page, or runs longer than a moment), following the task-reliability rules, the real TaskContext API and the budget gate. Use whenever work touches app/tasks/ or adds a button that starts long-running work."
---

# Add a background task type

The runner's rules are in spec section 5, milestone M2 (rules 1 to 10). Every task type must honor all ten. The code is the reference:

- `app/tasks/context.py`: what a handler receives (`TaskContext`).
- `app/tasks/handlers.py`: the `HandlerRegistry` class.
- `app/tasks/runner.py`: claims, cancellation, the watchdog, outcomes.
- `app/services/tasks.py`: the task rows (enqueue, cancel, retry, claim, finish).
- `app/services/practice.py` and `app/tasks/practice.py`: the only task type so far, a worked example.

## 1. Write the work in a service

Put the logic in a service under `app/services/`. The handler is thin: it calls the service and returns the result reference. A handler is an `async` function `TaskContext -> str | None`.

`TaskContext` (`app/tasks/context.py`) offers:

| Member | What it does |
| --- | --- |
| `ctx.task_id`, `ctx.task_type`, `ctx.attempt` | The row's id, type and attempt number. A retry adds one. |
| `ctx.input_ref` | The string the route stored with `enqueue(..., input_ref=...)`. Keep it short and structured: the practice task stores fixed-list values only. |
| `ctx.cancel_flag` | A `CancelFlag`. It satisfies the AI module's `CancelToken` by shape, so pass it to the AI runner. |
| `await ctx.heartbeat(progress=None)` | Records the heartbeat, and progress clamped to 0 to 100, in a worker thread. Raises `TaskCancelled` if a cancel was requested. Call it before each step. Heartbeats are the only sign of life: a stalled step stops them, and the watchdog interrupts the task after the type's timeout. |
| `ctx.check_cancelled()` | Raises `TaskCancelled` once the cancel flag is set. Call it between steps. |
| `await ctx.complete(write)` | Runs `write(conn)` and the running-to-succeeded update in ONE transaction, in a worker thread. `write` gets a `sqlite3.Connection`, must not commit, and returns the `result_ref` string. Raises `TaskCancelled` (a cancel was requested) or `TaskSuperseded` (this attempt is no longer running). A second `complete` is refused. |

Outcomes (`app/tasks/runner.py`):

- A returned result ends the task `succeeded`.
- An `AppError` ends it `failed`, storing the error's `kind` (never its text). The panel shows the spec 2.11 copy for that kind.
- Any other exception ends it `failed` with `TaskFailed`, and the log names only the exception type.
- Once the cancel flag is set, the task ends `cancelled`, however the handler ends.
- `stop()` and the watchdog end it `interrupted`.
- Nothing is retried automatically.

Anything the handler returns or raises after a successful `complete` is ignored.

## 2. Call providers through the meter

A handler never reserves or completes usage records itself. It calls the AI runner with a meter, the tier and the task id, as `PracticeService.run` does:

```python
await runner.run_task(
    TASK_NAME,
    inputs,
    ctx.cancel_flag,
    meter=ServiceUsageMeter(usage, service),  # app/services/usage_meter.py
    tier=tier,  # the chosen tier, as PracticeService.run passes it; None uses the task's own tier
    task_id=ctx.task_id,
)
```

`tier` overrides the task's tier for that run only: a value is used for "run once on Fast". `None` (the default) lets the executor resolve the task's own tier.

The executor reserves one record before each provider call and completes it after: on success, a provider error, invalid output or cancellation. A block before a call raises `BudgetBlocked`, and the call doesn't run. `meter` is required, so there is no uncounted call (ADR-080).

**The budget service.** A real task type takes its service from the saved provider kind: `SERVICE_BY_PROVIDER_KIND[settings.kind]` (`app/domain/usage.py`; provider setup maps the kind the same way). Use that one service for the budget gate (section 4) and for the meter, so the check and the count always name the same budget. The practice task is the exception: it always counts on `fake` (`PRACTICE_SERVICE` in `app/services/practice.py`), and it runs only while fakes are allowed.

**The provider and the runner.** The handler gets its runner from the factory that wiring injects: `factory.task_runner(provider, settings, data_dir=...)`, where `factory` is an `AIProviderFactory` (`app/modules/ai/__init__.py`) built in `app/wiring.py`. The provider comes from `factory.ai_provider(settings, data_dir=...)`. The AI task must be registered in `TASK_SPECS` (`app/modules/ai/tasks.py`, empty until M3). Use the `add-ai-task` skill for that: it also covers the prompt, schema, fixtures and tests. The practice task is not in `TASK_SPECS`; wiring adds it to the runner's specs only while fakes are allowed.

**Fake output.** The fake provider can run any task while fakes are allowed. Output from the fake provider that a real task type stores must carry provider and model `fake` and be visibly labelled, so it is never taken for real output (honesty rule; ADR-088). ADR-088 requires this before any real task type stores fake output, and nothing stores it yet.

**Web search.** A task with `allow_web_search` can't run yet. The Claude provider refuses web-search requests with `ProviderRequestRejected` until searches are counted (ADR-080, ADR-057). The task that wires the usage hook removes the refusal (before M8).

## 3. Register the type

- The type name must match `[a-z][a-z0-9_]{0,63}` (`is_task_type`, `app/domain/usage.py`). A `:` is refused, because dedupe keys use it.
- Give it a `TaskTypeSpec(timeout, label)` (`app/domain/tasks.py`). `timeout` is how long the task may go without a heartbeat. It must be longer than `TASK_TIMEOUT_FLOOR` (`app/wiring.py`: one provider run plus its close), and `create_app` refuses a shorter one. `label` is a short constant for the panel.
- Register the type in `create_app` (`app/main.py`) with `handlers.register(TYPE, TaskTypeSpec(...), handler)`. The practice task is registered there only while fakes are allowed (`if task_registry is None and app_wiring.allow_fakes`). `app/tasks/handlers.py` holds the registry class, not the registrations.
- `TASK_SPECS` (`app/modules/ai/tasks.py`) is the AI task table, not the runner's registry. The practice task stays out of it.

## 4. Start it from a route

Routes call services only. `start_practice` (`app/api/tasks.py`) is the example. It is an `async` route, and each database call in it runs through `run_in_threadpool`: `gate.check`, `practice.offered` and `practice.start` (which calls `enqueue`). `BudgetGate.check` and `enqueue` block on the database, so a new async route wraps them the same way.

1. Validate the values against fixed lists. Anything else gets 422, and what was sent is never echoed.
2. Ask the budget gate first: `BudgetGate.check(service, tier=tier, confirmation=confirmation_from_form(form))` (`app/services/budget_gate.py`). Only a `Start` answer lets the task be queued. For htmx, `ShowWarning`, and `ShowTierCap` with a free Fast tier, answer 200 with the dialog. `BlockedAction`, `ShowTierCap` without a free Fast tier and `Unreadable` answer 409 with the section and the reason. A plain request never gets the dialog: it gets the reason as 409 text. None of these queues anything (ADR-086).
3. Call `service.enqueue(type, target, input_ref=...)`. It returns `TaskRef(id, created)`. Then answer the route's own target, as described below.

What the practice route answers. For htmx: 200 with the Testing tools section, the accepted choices still selected, focus on its status line and the outcome in an `announce` trigger. For a plain request: 303 to `/settings?practice=started#testing-tools`, or `already` when a task was already queued or running. A route for a new task type answers its own fragment, not this section.

The check and the enqueue are two steps, so a second start can still be refused by the runner's reserve. That is a known limit (M2.24).

Dedupe: `enqueue` keeps one active task per `type:target` (`dedupe_key`). A second request while one is queued or running returns the existing id (AC-M2.1). A target such as `application:42` works.

Retry: `TaskService.retry(task_id)` re-queues a failed, cancelled or interrupted task as its next attempt on the same row (ADR-076 item 11). If another active task has the same dedupe key, that task is returned instead. Queued, running and succeeded tasks are refused with `TaskNotRetryable`. Retry is the only way a failed task runs again.

Crash recovery (ADR-076 item 10): when a workspace opens, running tasks become interrupted and queued tasks stay queued. Nothing is retried.

## 5. Save results as new rows

Write the result inside `ctx.complete`, as new rows. Never overwrite an earlier result (rule 3). Until M3 has a results table, the practice task stores only the reference `practice:<steps>`.

## 6. Show the task

- A route for a new task type can swap its button to the task fragment (`app/templates/fragments/tasks/_task.html`). The practice route doesn't: it answers the Testing tools section (section 4). Either way the fragment refetches on the `tasks:resync` and `tasks:updated` body events (see the `htmx-ui` skill, Live updates).
- Never read a status from an event. Events can arrive out of order, so the fragment refetches.
- The failure copy comes from `message_for_kind` (`app/domain/errors.py`): an exact match on a stored kind, and the generic message for anything else.

## 7. Log and store only ids and kinds

- Use constant format strings. Arguments are ids, type names, counts and exception type names only. Never log provider output, error text, prompts or row content (spec 2.10; ADR-088). The loggers in `app/tasks/runner.py` and `app/services/tasks.py` are the pattern.
- Never store `str(error)` in `tasks.error`. Store the error kind (M2.7).

## 8. Tests

Rule 7 and rule 10 of spec section 5 M2 require, for each new type:

- a duplicate enqueue returns the existing task (AC-M2.1 pattern: `tests/integration/test_task_service.py`);
- the success path stores the result, and its provenance (prompt id, version and model), in the same transaction (`tests/integration/failures/test_atomic_result.py`);
- an injected failure between writing the result and the success leaves no orphaned result, using a test trigger that aborts the success update (same file);
- cancellation during a call and between steps discards partial output (`tests/integration/failures/test_cancel.py`);
- the watchdog interrupts a task whose heartbeat stops, with a frozen clock (`tests/unit/tasks/test_watchdog.py`);
- a budget block stops the call before it is made and shows the reset date (`tests/integration/test_practice_task.py`, `tests/integration/test_budget_warning_dialog.py`);
- a process kill during the task leaves it interrupted, with no automatic retry, when the type is long enough to kill (`tests/integration/failures/test_process_kill.py`);
- no secret or personal data in rows, events or logs (`tests/integration/test_ac_m1_4_task_leaks.py`).

## The ten reliability rules (spec 5, M2)

1. One source of truth: the tasks table; every transition is a conditional update from the expected state.
2. No duplicate work: a dedupe key of type and target; an active duplicate returns the existing task.
3. Atomic results: the output and the succeeded state are written in one transaction; results are new rows, never overwrites.
4. Crash recovery: running tasks become interrupted on start; failed and interrupted tasks are never retried automatically.
5. Hung calls: a heartbeat while running; the watchdog interrupts tasks whose heartbeat stops beyond the type's timeout.
6. Cancellation: a flag checked between steps; the provider call is aborted where possible; partial output is discarded.
7. Usage accounting: a usage record is reserved before each external call and completed after it.
8. Reconnecting pages re-fetch task state, so missed events don't matter.
9. SQLite in WAL mode with a busy timeout and short transactions; the runner's database calls go through `asyncio.to_thread`, and nothing is awaited inside `Database.transaction()`.
10. Failure tests for each of the above.
