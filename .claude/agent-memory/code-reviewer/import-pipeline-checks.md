---
name: import-pipeline-checks
description: Recurring gaps in upload-then-AI-task pipelines (CV import M3.4): unreachable kept uploads, mechanical needs_check false negatives, Retry skipping the budget gate
metadata:
  type: project
---

Patterns found reviewing the CV import pipeline (M3.4, 2026-10-12). Check them again for any later "store an upload, then run an AI task" feature.

- **Kept upload with no handle.** When the store succeeds but the start is refused (budget block, plain 409, no provider), the response carries no import id and no route lists stored uploads, so a personal file stays on disk with no way to start or remove it. Tests hide this by reading the id from the database. **How to apply:** ask what in the response (or which list route) lets the user reach a kept upload; require it or a recorded hand-off to the page task.
- **Mechanical number check compares bare digit runs.** "5k" or "5 million" passes when the source has "5"; a number moved to another claim passes; number words are invisible. Role dates, skill names and wording unrelated to the quote are not checked at all. Probe `drafts_from` directly with a tiny CV (pure, fast). **Why:** false negatives here decide which drafts look trustworthy.
- **Tasks panel Retry re-runs the AI call without the budget gate** (the gate is only in the feature's start route). Check whether an ADR accepts this for each new real task.
- **Remove order:** row deleted in the transaction, file unlinked after; a failed unlink leaves an orphan file with only a log line. Look for a sweep or a recorded debt item.
- Running the whole integration file for this feature takes several minutes (each import spawns parser children, about 0.6 s each): run it in the background from the start.
