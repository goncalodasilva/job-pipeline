"""Small checkout-local status summary shared by both SessionStart adapters."""

from __future__ import annotations

from pathlib import Path

from scripts.next_task import sections, tasks


def context(root: Path, engine: str) -> str:
    prefix = "/" if engine == "claude" else "$"
    lines = ["Job Search Pipeline: session context"]
    try:
        text = (root / "docs/PROGRESS.md").read_text(encoding="utf-8")
        current = next(
            (
                line.split(":", 1)[1].strip()
                for line in text.splitlines()
                if line.startswith("Current milestone:")
            ),
            "",
        )
        section = sections(text).get(current, "")
        items = tasks(section)
        done = sum(item["done"] for item in items)
        lines.append(f"Current milestone: {current} ({done} of {len(items)} tasks done).")
        if not items:
            lines.append(f"Not planned yet: {prefix}plan-milestone {current}.")
        elif done == len(items):
            lines.append(f"All tasks done: {prefix}milestone-acceptance {current}.")
        else:
            lines.append("Next open tasks (check Blocked and Checkpoint details before starting):")
            lines.extend(
                f"  - {item['id']} {item['title']}"
                for item in [item for item in items if not item["done"]][:3]
            )
    except (OSError, ValueError):
        lines.append("Could not read docs/PROGRESS.md; load project status before working.")
    lines.append(
        "Read AGENTS.md and the applicable rules. Honesty: cite confirmed fact ids; no secrets or "
        "personal data; no network in tests; module boundaries; tests first; stop per spec 6.7. "
        f"Workflow: {prefix}plan-milestone, {prefix}next-task or {prefix}run-milestone, "
        f"{prefix}verify, {prefix}milestone-acceptance. PROGRESS.md is the shared handoff."
    )
    return "\n".join(lines)
