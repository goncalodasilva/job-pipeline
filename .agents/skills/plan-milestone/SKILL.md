---
name: plan-milestone
description: Plan a requested Job Pipeline milestone into small tasks for user approval. Use when explicitly asked to plan a milestone; do not implement it.
---

# Plan a milestone

Take the milestone id from the user's request. Run `python3 scripts/next_task.py --open`, then read that milestone in spec section 5, relevant design/ADRs, and the actual code. The main session plans; delegate a focused investigation only if it resolves a concrete unknown.

Propose dependency-ordered tasks covering every acceptance criterion. Each task names criteria, meaningful tests and touched modules and fits one session (roughly under 400 changed lines including tests). Include infrastructure/test-only tasks when needed. Propose before/after checkpoints for secrets, honesty, stored personal data and the first screen of a new area (ADR-065).

Present a table (id, title, criteria, tests, modules, checkpoint), open questions and risks. Wait for approval before writing the plan. Preserve previous tasks/evidence when revising an existing plan.

After approval, write `docs/PROGRESS.md` with `Current milestone: M<n>`, a `## M<n> — Title` heading and `Status: planned`. Preserve this parsed format:

```
- [ ] M<n>.1 Title
  - AC: AC-M<n>.x
  - Tests: named behavior checks
  - Checkpoint: after — reason (only when approved)
```

Add a session-log entry. Use the milestone branch convention `m<n>-<slug>` and suggest `$next-task` or `$run-milestone`. No application implementation during planning.
