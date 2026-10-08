---
name: plan-milestone
description: "Plan a roadmap milestone into small, testable tasks and write them to docs/PROGRESS.md after the user approves."
argument-hint: "M<n>"
disable-model-invocation: true
---

# Plan milestone $ARGUMENTS

1. **Gather context.** Read the milestone's section in `docs/TECHNICAL_SPEC.md` section 5 (goal, scope, acceptance criteria, test plan, exit checks), the spec sections it depends on, the related parts of `docs/references/detailed-design.md`, `docs/DECISIONS.md`, and the current state of the code and of `docs/PROGRESS.md`.
2. **Delegate to `planner`** with that context and these planning rules:
   - Every acceptance criterion of the milestone is covered by at least one task.
   - Each task fits in one session and produces a reviewable diff (roughly under 400 changed lines, tests included).
   - Tasks are ordered by dependency; any spike or risky unknown comes first.
   - Each task names its acceptance criteria, the tests to write, and the modules it touches.
   - Propose a checkpoint for tasks the user should see before or after they run unattended: changes to secrets, the honesty rule or stored personal data, and the first screen of a new area (ADR-065).
   - No task without tests; infrastructure and test-only tasks are allowed.
3. **Present the plan** to the user as a table (id, title, acceptance criteria, tests, touched modules, checkpoint), followed by open questions and risks. Wait for approval or changes.
4. **Write the approved plan** into `docs/PROGRESS.md` under `## <milestone> — <title>`, using exactly this format so scripts can parse it:

   ```
   Status: planned

   - [ ] M<n>.1 Title
     - AC: AC-M<n>.x, AC-M<n>.y; plus any task-specific criteria
     - Tests: what will prove it
     - Checkpoint: after — why the user wants to review it (only on tasks the user approved as checkpoints; `before` or `after`)
   ```

   Set `Current milestone: M<n>` and add a session-log line.
5. **Suggest the branch name** (`m<n>-<slug>`) and tell the user to start with `/run-milestone` (all tasks, stopping at checkpoints) or `/next-task` (one task). Don't write code in this skill.
