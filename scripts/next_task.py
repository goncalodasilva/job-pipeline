#!/usr/bin/env python3
"""Show the next open task (or a named task) from docs/PROGRESS.md.

Usage: next_task.py [M<n>.<k> | --open]
  --open  list every open task of the current milestone with its details.
Always exits 0, because skills inject its output and a failure would abort them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TypedDict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agent_support import project_root as discover_root


class Task(TypedDict):
    done: bool
    id: str
    title: str
    details: list[str]


TASK_LINE = re.compile(r"^- \[( |x|X)\] (M\d+\.\d+)\s+(.*)$")


def project_root() -> Path:
    return discover_root()


def sections(text: str) -> dict[str, str]:
    pattern = re.compile(r"^## (M\d+)\b.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    return {m.group(1): m.group(0) for m in pattern.finditer(text)}


def tasks(body: str) -> list[Task]:
    found: list[Task] = []
    current: Task | None = None
    for line in body.splitlines():
        match = TASK_LINE.match(line)
        if match:
            current = {
                "done": match.group(1).lower() == "x",
                "id": match.group(2),
                "title": match.group(3).strip(),
                "details": [],
            }
            found.append(current)
        elif current is not None and line.startswith("  ") and line.strip():
            current["details"].append(line.strip())
        elif line.strip() and not line.startswith("  "):
            current = None
    return found


def show(task: Task) -> None:
    state = "done" if task["done"] else "open"
    print(f"Task {task['id']} ({state}): {task['title']}")
    for detail in task["details"]:
        print(f"  {detail}")


def main() -> int:
    wanted = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    progress = project_root() / "docs" / "PROGRESS.md"
    if not progress.exists():
        print("docs/PROGRESS.md not found.")
        return 0
    text = progress.read_text(encoding="utf-8")
    by_milestone = sections(text)
    current_match = re.search(r"^Current milestone:\s*(M\d+)", text, re.MULTILINE)
    current = current_match.group(1) if current_match else None

    if wanted and wanted != "--open":
        milestone = wanted.split(".")[0]
        for task in tasks(by_milestone.get(milestone, "")):
            if task["id"] == wanted:
                show(task)
                return 0
        print(f"Task {wanted} was not found in docs/PROGRESS.md.")
        return 0

    if not current or current not in by_milestone:
        print("No current milestone set in docs/PROGRESS.md. Plan the milestone M<n> first.")
        return 0
    body = by_milestone[current]
    title = body.splitlines()[0].lstrip("# ").strip()
    all_tasks = tasks(body)
    done = sum(1 for t in all_tasks if t["done"])
    print(f"Current milestone: {title} ({done} of {len(all_tasks)} tasks done)")
    if not all_tasks:
        print(f"{current} is not planned yet. Plan the milestone {current} first.")
        return 0
    open_tasks = [t for t in all_tasks if not t["done"]]
    if not open_tasks:
        print(f"All {current} tasks are done. Run milestone acceptance for {current}.")
        return 0
    if wanted == "--open":
        print(f"Open tasks ({len(open_tasks)}):")
        for task in open_tasks:
            show(task)
        return 0
    print("Next task:")
    show(open_tasks[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
