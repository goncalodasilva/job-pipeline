---
name: run-milestone
description: Run the current approved Job Pipeline milestone across eligible tasks, respecting checkpoints and blockers, then prepare acceptance. Use only when explicitly requested.
---

# Run the approved milestone

Read ADR-065, `docs/PROGRESS.md`, and `.agents/skills/next-task/SKILL.md`. Run `python3 scripts/next_task.py --open`. Parse the optional milestone id and `--max N` from the user's request; an explicit id must match Current milestone. The milestone must be planned. Status-script exit zero alone is not approval/readiness.

Inspect status, branch and commits before starting. Use the milestone branch. Resume uncommitted work belonging to the first open task at its actual phase. A completed uncommitted task needs verification and a task-owned commit. Stop if changes cannot be assigned to a task. Do not stash or commit another agent's work.

Loop through open tasks without Blocked questions that do not depend on parked tasks:

- Honor a `Checkpoint: before` unless this is the first task of this invocation (rerunning explicitly starts that checkpoint task, ADR-065).
- Run next-task phases 1–9, preserving failing tests first, independent reviewers, verification, evidence and one commit per task. Do not run its final stop/report between tasks.
- Put every leftover/manual check in the task Evidence as `Left for the user:`. Report one concise line with task id, commit and result.
- Stop at `Checkpoint: after`, the requested task cap, or a global stop condition.

Correct spec-justified test errors directly under spec 4.4/ADR-133 and continue; they are not blockers. A task-local ambiguity (including a test whose correct expectation cannot be established from the spec) or three failed fixes can be parked only when all current changes belong to that task. Save task-owned files with an explicit `git stash push --include-untracked -m "parked M<n>.<k>: <reason>" -- <task paths>` and verify the remaining tree is clean. Record an indented `- Blocked: <date> — <question and options>` and commit that status change. Continue only with clearly independent tasks. If ownership or dependencies are unclear, stop. Remove Blocked only after the user answers; inspect stashes before restoring and ask if the working tree is not clean.

Global stop conditions: AGENTS.md/spec 6.7; verification failures outside this task; checkpoint; task cap. Preserve the current work and report the cause.

When all tasks are ticked, follow `.agents/skills/milestone-acceptance/SKILL.md` through preparing its manual checklist. Acceptance still waits for the user.

Every run ends with completed tasks/commits, parked questions, leftovers, why it stopped, and the next action. No push or merge.
