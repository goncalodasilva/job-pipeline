---
name: next-task
description: "Run the full development loop for one task from docs/PROGRESS.md (the next open task, or the task id given): tests first, implement, verify, review, record, commit, report."
argument-hint: "[task id, e.g. M1.3]"
disable-model-invocation: true
allowed-tools: "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/next_task.py) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/next_task.py *)"
---

# Next task

## Status

!`python3 ${CLAUDE_SKILL_DIR}/scripts/next_task.py`

If the user passed a task id (`$ARGUMENTS`), work on that task instead: show it with `python3 ${CLAUDE_SKILL_DIR}/scripts/next_task.py <id>`. If the status says the milestone isn't planned, stop and suggest `/plan-milestone`. If every task is done, stop and suggest `/milestone-acceptance`.

## Loop

Work on exactly one task, then stop. Keep the user informed with one short line per step.

1. **Understand.** Read the task's lines in `docs/PROGRESS.md`, the acceptance criteria it references in `docs/TECHNICAL_SPEC.md` section 5, the spec sections it touches, and the matching parts of `docs/references/detailed-design.md`. Check `docs/DECISIONS.md` for related decisions. If the task has no acceptance criteria or something conflicts, stop and ask.
2. **Branch.** Make sure you're on the milestone branch (`m<n>-<slug>`); create it from main if needed.
3. **Tests first.** Delegate to `test-author` with the task id, its acceptance criteria and the relevant spec excerpts. Confirm the new tests fail, and fail for the expected reason.
4. **Implement.** Make the tests pass with the smallest coherent change that respects the module boundaries (spec 2.3). For a well-scoped task you may delegate to `implementer`. Use the procedure skills when they apply (`add-ai-task`, `add-provider`, `htmx-ui`, `db-migration`, `background-task`, `onboarding-step`).
5. **Verify.** Delegate to `verifier`: quick mode, or full mode if the task touched templates, static files, migrations or the task runner.
6. **Debug.** On failure, fix it or delegate to `debugger`. After three failed attempts at the same failure, stop and ask the user.
7. **Review.** Delegate to `code-reviewer`. Also consult `security-reviewer`, `ux-reviewer` or `prompt-engineer` when the change touched their areas (spec 6.3). Fix critical findings and should-fix findings that are cheap; list the rest for the user.
8. **Verify again** after review fixes.
9. **Record.** Delegate to `docs-keeper`: tick the task in `docs/PROGRESS.md` with evidence (test names, verify result), add a session-log line, and add an ADR if a decision was made.
10. **Commit.** Stage only the files for this task and commit with a conventional message naming the task id, e.g. `feat(onboarding): provider step test connection (M1.4)`. Never use `--no-verify`. Don't push.
11. **Report.** Tell the user what changed, the evidence (verify result, key tests), anything left for them (manual checks, open questions), and what the next task is. Then stop.

## Stop and ask the user when

- sources conflict or the spec is ambiguous;
- a new runtime dependency or stack change seems needed;
- the honesty rule, security model or stored personal data would change;
- the task's scope would change, or a test's expectation looks wrong;
- three attempts at the same failure didn't work;
- real credentials or the user's real data would be needed.
