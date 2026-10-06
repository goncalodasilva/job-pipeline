#!/usr/bin/env python3
"""SessionStart: add the current milestone, next open tasks and golden rules to context.

Always exits 0; whatever it prints becomes context for the session.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_TASKS = 3


def summarize(progress: Path) -> list[str]:
    text = progress.read_text(encoding="utf-8")
    match = re.search(r"^Current milestone:\s*(M\d+)", text, re.MULTILINE)
    if not match:
        return ["docs/PROGRESS.md has no 'Current milestone:' line."]
    current = match.group(1)
    section = re.search(
        rf"^## {re.escape(current)}\b.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not section:
        return [f"Current milestone: {current} (no section found in docs/PROGRESS.md)."]
    body = section.group(0)
    title = body.splitlines()[0].lstrip("# ").strip()
    done = len(re.findall(r"^- \[x\]", body, re.MULTILINE | re.IGNORECASE))
    open_tasks = re.findall(r"^- \[ \] (.+)$", body, re.MULTILINE)
    lines = [f"Current milestone: {title} ({done} done, {len(open_tasks)} open)."]
    if not open_tasks and done == 0:
        lines.append(f"Not planned yet: run /plan-milestone {current}.")
    elif not open_tasks:
        lines.append(f"All tasks done: run /milestone-acceptance {current}.")
    else:
        lines.append("Next open tasks:")
        lines.extend(f"  - {task}" for task in open_tasks[:MAX_TASKS])
    return lines


def main() -> int:
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    progress = project / "docs" / "PROGRESS.md"
    print("Job Search Pipeline: session context")
    try:
        if progress.exists():
            for line in summarize(progress):
                print(line)
        else:
            print("docs/PROGRESS.md is missing.")
    except Exception as exc:  # never break session start
        print(f"(Could not read docs/PROGRESS.md: {exc})")
    print(
        "Reminders: honesty rule (cite fact ids), no secrets or personal data, no network in tests, "
        "respect module boundaries, tests first, stop and ask per spec 6.7. "
        "Workflow: /plan-milestone, /run-milestone or /next-task, /verify, /milestone-acceptance."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
