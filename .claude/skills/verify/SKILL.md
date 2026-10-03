---
name: verify
description: "Verify the project before every commit and whenever work is about to be called done. Runs format, lint, types, module boundaries, fast tests and a smoke run; \"full\" adds end-to-end tests, coverage thresholds and all pre-commit hooks."
argument-hint: "[quick|full]"
allowed-tools: "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/verify.sh) Bash(bash ${CLAUDE_SKILL_DIR}/scripts/verify.sh *)"
---

# Verify

Choose the mode:

- `full` when the change touched `app/templates/`, `app/static/`, page routes, migrations or the task runner, before `/milestone-acceptance`, or when the user asks for it;
- `quick` otherwise. If `$ARGUMENTS` says `quick` or `full`, use that.

Run:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/verify.sh <mode>
```

The last line is `VERIFY <mode>: PASS` or `VERIFY <mode>: FAIL (<steps>)`.

- On PASS, report it in one line, with the step list if useful.
- On FAIL, read the failing step's output, fix the cause (or delegate to `debugger`), and run again. Never commit while verification fails, and never claim success without a PASS line from this run.
- If end-to-end tests can't start because browsers are missing, run `uv run playwright install chromium` once, then verify again.
- A SKIP line means no tests exist yet for that marker. That's fine early in the project, but a SKIP for a marker the current task should have covered means tests are missing.
