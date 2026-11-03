---
name: run-milestone
description: "Run every open task of the current milestone back to back with the /next-task loop, parking blocked tasks instead of stopping, honouring checkpoints, then prepare milestone acceptance."
argument-hint: "[M<n>] [--max N]"
disable-model-invocation: true
allowed-tools: "Bash(python3 ${CLAUDE_SKILL_DIR}/../next-task/scripts/next_task.py*) Bash(git add *) Bash(git commit *) Bash(git stash push *) Bash(git stash list*) Bash(git switch *) Bash(git checkout -b *)"
---

# Run milestone

## Status

!`python3 ${CLAUDE_SKILL_DIR}/../next-task/scripts/next_task.py --open`

Arguments: `$ARGUMENTS`. A milestone id must match `Current milestone:` in `docs/PROGRESS.md`, otherwise stop and ask. `--max N` caps the number of tasks this run finishes (default: all open tasks). Rules for this skill: ADR-065.

If the milestone isn't planned, stop and suggest `/plan-milestone`. If every task is done, go straight to **Finish**.

## Start: look at the working tree

Before the first task, run `git status` and `git branch --show-current`, and make sure you're on the milestone branch (`m<n>-<slug>`, create it from main if needed).

- **Clean:** start with the first eligible task.
- **A ticked task that isn't committed** (its Evidence says verify passed but `git log` has no commit for its id): run quick verification, and if it passes commit that task's files with its conventional message (step 10 of `/next-task`). Then continue.
- **Uncommitted work for the first open task:** resume it. Check which of its named tests exist and whether they pass, then pick up the `/next-task` loop at the right step instead of starting over.
- **Anything else** (changes you can't tie to one task): stop and ask.

Tell the user in one line what you found and where the run starts.

## Loop

Repeat until no eligible task is left, a stop condition is hit, or `--max` tasks are finished.

1. **Pick** the first open task without a `Blocked:` line that doesn't build on a parked task. If its `Checkpoint:` line says `before` and it isn't the first task of this run, stop (see **Report**).
2. **Run** steps 1–10 of `.claude/skills/next-task/SKILL.md` for it, exactly as written: tests first, implement, verify, debug, review, verify again, record, commit. Don't run its step 11. Use the procedure skills and reviewers it names.
3. **Leftovers.** When `docs-keeper` records the task, include anything you would have listed for the user (non-critical findings you didn't fix, manual checks) as a `Left for the user:` sentence at the end of the task's Evidence line. Nothing for the user stays only in this conversation.
4. **Say** one line: `M<n>.<k> done: <commit hash> <summary>`.
5. **Checkpoint after:** if the task's `Checkpoint:` line says `after`, stop (see **Report**).

Keep the main session terse: delegate well-scoped implementation to `implementer`, and don't paste large files or test logs into the conversation. `docs/PROGRESS.md` is the handoff if the context is compacted.

## Park a task (local problem, the run continues)

Correct spec-justified test errors directly under spec 4.4/ADR-133 and continue; they are not blockers. Park when the problem belongs to this task only: an ambiguous criterion (including a test whose correct expectation cannot be established from the spec), or three failed attempts at the same failure.

1. Stash its work: `git stash push --include-untracked -m "parked M<n>.<k>: <short reason>"`. Make sure the tree is clean afterwards.
2. Have `docs-keeper` add `  - Blocked: <date> — <the question for the user, with the options you see>` under the task, and commit that change (`docs(progress): park M<n>.<k>`).
3. Continue with the next task only if it clearly doesn't build on the parked one (it doesn't reference its criteria, fragments, services or tests). If you're not sure, stop the run.

A `Blocked:` line is removed only after the user answers. Then the task is eligible again: restore its stash (`git stash list`, then ask before `git stash pop` if the tree isn't clean).

## Stop the run (global problem)

Stop, leave the tree clean or with only the current task's work, and report when:

- sources conflict or the spec is ambiguous in a way that affects more than this task;
- a new runtime dependency or stack change seems needed;
- the honesty rule, the security model or stored personal data would change;
- the milestone's scope would change;
- real credentials or the user's real data would be needed;
- pushing, rewriting history or deleting data would be needed;
- verification fails for reasons outside the current task (for example a broken test from an earlier commit);
- a checkpoint or the `--max` cap is reached.

## Finish

When every task of the milestone is ticked, follow steps 1–5 of `.claude/skills/milestone-acceptance/SKILL.md`: full verification, critical coverage, criteria to tests, the acceptance report, then ask the user to run the manual checklist. Don't mark the milestone accepted; that waits for the user (its step 6).

## Report

End every run, however it ends, with:

- **Done this run:** one line per task with its commit hash.
- **Parked:** each `Blocked:` question, so the user can answer them in one go.
- **Left for the user:** the `Left for the user:` notes from the tasks done this run.
- **Why the run stopped**, and what comes next (`/run-milestone` again, an answer needed, or the manual checklist).

Don't push or merge.
