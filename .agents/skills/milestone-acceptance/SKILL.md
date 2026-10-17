---
name: milestone-acceptance
description: Prepare acceptance evidence and the manual checklist for an explicitly requested completed Job Pipeline milestone; record acceptance only after the user's confirmation.
---

# Accept a milestone

Take the milestone id from the request and read its spec criteria and PROGRESS section. All tasks must be ticked; otherwise list the remaining tasks and stop.

1. Run `bash scripts/verify.sh full` and `uv run python scripts/check_critical_coverage.py --milestone M<n>`. Both must pass. A missing critical module due by this milestone fails acceptance.
2. Map every acceptance criterion to its named `test_ac_m<n>_<k>_...` tests and observed results. A criterion without a passing test remains unaccepted.
3. Write `docs/acceptance/M<n>.md` with date, branch, commit, criteria/tests/results table, verification summary, manual checklist from the milestone exit checks, and limitations/follow-ups. Live-provider checks belong on the user's checklist; the user supplies their own credentials.
4. Present the report and ask the user to run/confirm the manual checklist. Do not mark the milestone accepted yet.
5. Only after confirmation, set `Status: accepted (YYYY-MM-DD)`, move Current milestone forward, and record the confirmation in the session log. Suggest that the user merge the milestone branch. Do not merge or push.
