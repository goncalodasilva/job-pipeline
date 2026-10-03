---
name: milestone-acceptance
description: "Close a milestone: confirm every task is done, run full verification, map each acceptance criterion to passing tests, write the acceptance report, and give the user the manual checklist."
argument-hint: "M<n>"
disable-model-invocation: true
---

# Milestone acceptance for $ARGUMENTS

1. **Tasks.** Confirm every task of the milestone in `docs/PROGRESS.md` is ticked. If not, list the open ones and stop.
2. **Full verification.** Delegate to `verifier` in full mode. If it fails, stop and report.
3. **Criteria to tests.** For each acceptance criterion of the milestone in `docs/TECHNICAL_SPEC.md` section 5, find its tests (named `test_ac_m<n>_<k>_...`) and their results. A criterion without a passing test is not accepted.
4. **Report.** Write `docs/acceptance/M<n>.md`:

   ```
   # M<n> acceptance — <title>
   Date: <date> · Branch: <branch> · Commit: <hash>

   ## Automated results
   | Criterion | Tests | Result |

   ## Verification
   <full verify summary>

   ## Manual checklist (for the user)
   - [ ] <exit checks and manual items from the spec>

   ## Notes
   <known limitations, follow-ups>
   ```

5. **Ask the user** to run the manual checklist and confirm. Live tests (`-m live`) are part of the manual checklist when the milestone has them; the user runs them with their own credentials.
6. **After the user confirms,** delegate to `docs-keeper`: mark the milestone `Status: accepted (<date>)`, set `Current milestone` to the next one, and add a session-log line. Suggest that the user merge the milestone branch into main. Don't merge or push yourself.
