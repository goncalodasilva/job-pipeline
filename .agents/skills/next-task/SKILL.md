---
name: next-task
description: Execute one explicitly requested Job Pipeline task with tests first, verification, independent review, evidence and a task commit. Stop after one task.
---

# Run one task

Use the task id in the request, or run `python3 scripts/next_task.py` for the next open task. Show an explicit id with `python3 scripts/next_task.py M<n>.<k>`. This script reports status; a zero exit code does not establish readiness. If unplanned, suggest `$plan-milestone`; if all done, suggest `$milestone-acceptance`. A Blocked question needs the user's answer before proceeding.

Keep the user informed, one short update per meaningful phase. Perform these phases in the main session except independent reviews:

1. Read the task, its criteria/spec/design, relevant ADRs and matching AGENTS.md rules. Missing criteria/conflicting sources need clarification.
2. Inspect `git status`, current branch and relevant commits. Work on the milestone branch (`m<n>-<slug>`). Resume task-owned unfinished work at its actual phase. Never switch away with unrelated changes, stash them, or include them in the task commit; ask when ownership is unclear.
3. Write meaningful acceptance tests with the project's fakes, frozen clock, sequential ids and temporary data. Run the new tests and confirm expected missing-behavior failures before implementation. Do not change existing expectations without authorization.
4. Implement the smallest coherent change. Follow matching shared procedure skills: AI task, provider, htmx UI, database, background task, onboarding.
5. Run `bash scripts/verify.sh quick`, or full for templates/static/page routes/migrations/task runner. Fix failures; stop after three failed fixes of the same problem. No PASS claim without this run's final PASS line.
6. Delegate an independent `code-reviewer`; also security review for credentials/providers/network/uploads/middleware/tooling permissions/hooks, UX review for UI/routes, prompt review for prompts/task contracts/AI fixtures. Custom Codex profiles live in `.codex/agents/`. If profiles cannot be selected, spawn a generic subagent and pass the matching profile's instructions explicitly. Send task/criteria, diff scope, relevant rules and observed verification. Wait for findings; do not edit concurrently with review. If delegation is unavailable, report the outstanding independent review and stop before calling the task done.
7. Fix critical findings and cheap should-fix findings; record the rest. Re-verify after fixes. Record recurring review lessons in `docs/development/review-notes/`.
8. Tick the task with `Evidence: uncommitted; named tests; VERIFY <mode>: PASS`, plus leftover/manual checks and a dated session-log entry. Add an ADR for material decisions.
9. Stage only this task's files, inspect the staged diff, and commit a conventional message with the task id. Never use `--no-verify`. Do not push or merge. A failed pre-commit hook must be resolved and verification updated as needed.
10. Report what changed, verification, commit hash, leftovers and next task, then stop.

AGENTS.md/spec 6.7 stop conditions apply throughout. Milestone checkpoints and parking semantics are defined by ADR-065; the `$run-milestone` caller handles continuation and suppresses this skill's final stop/report between tasks.
