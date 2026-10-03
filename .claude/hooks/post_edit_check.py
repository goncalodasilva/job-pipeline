#!/usr/bin/env python3
"""PostToolUse: format and lint an edited Python file, and report leftovers to Claude.

Runs only once the project has a pyproject.toml and uv is installed; otherwise
it does nothing. Exit code 2 shows the remaining ruff findings to Claude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = data.get("tool_input") or {}
    raw = str(tool_input.get("file_path") or "")
    if not raw.endswith(".py"):
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    if not (project / "pyproject.toml").exists() or shutil.which("uv") is None:
        return 0
    file = Path(raw) if Path(raw).is_absolute() else project / raw
    if not file.exists():
        return 0

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--no-sync", *args],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=90,
        )

    try:
        formatted = run(["ruff", "format", str(file)])
        if "Failed to spawn" in formatted.stderr:
            return 0  # ruff isn't installed in the project environment yet
        checked = run(["ruff", "check", "--fix", str(file)])
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if checked.returncode != 0:
        output = (checked.stdout + checked.stderr).strip()
        print(f"ruff still reports problems in {raw}:\n{output[-3000:]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
