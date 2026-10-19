---
name: verify
description: Run Job Pipeline verification before committing or claiming completion, and when the user requests checks. Select full for UI, migrations, task runner or milestone acceptance.
---

# Verify

Use `full` for changes to templates, static files, page routes, migrations or the task runner, and before milestone acceptance; otherwise use `quick`. Honor an explicit requested mode.

From the checkout root run `bash scripts/verify.sh <quick|full>`. Both coding tools use this same recipe. The final `VERIFY <mode>: PASS` or `FAIL` line from this run is authoritative; a cached Evidence entry is not a fresh run.

On failure, inspect the first meaningful error, fix the cause within scope and rerun. Stop after three failed fixes of the same failure. A sandbox denial, unavailable browser or incomplete run is not a passing result. For missing Chromium, install it once with `uv run playwright install chromium`, using native approval if downloads are blocked.

Report mode/result and meaningful failures briefly. Do not commit while verification fails. A SKIP because no tests exist is acceptable only when the task did not require tests in that category. Full mode includes pre-commit hooks that may format files; inspect resulting changes and rerun checks when needed.
