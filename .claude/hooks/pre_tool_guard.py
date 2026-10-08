#!/usr/bin/env python3
"""PreToolUse guard for Claude Code.

Blocks tool calls that could expose secrets, touch the user's real Job Search
Pipeline data, or damage the repository. Changes to the guardrails themselves
require the user's approval through a permission prompt.

Exit code 2 blocks the call and shows stderr to Claude. Malformed input fails
open (exit 0) so a hook-format change can't brick a session; the permission
rules in .claude/settings.json remain the second line of defense.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

APP_DIR_NAME = "jobpipeline"  # platformdirs name of the real config and data folders

SECRET_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),
    re.compile(r"\.(pem|key|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|/)id_(rsa|ecdsa|ed25519)(\.pub)?$"),
    re.compile(r"(^|/)\.(aws|ssh|gnupg)(/|$)"),
    re.compile(r"(^|/)\.credentials\.json$"),
]

GUARDRAIL_PATHS = [
    re.compile(r"(^|/)\.claude/settings\.json$"),
    re.compile(r"(^|/)\.claude/hooks/"),
]

# Also catches --force-with-lease=<ref>, --force-if-includes and +<refspec> (ADR-075).
FORCE_PUSH = re.compile(
    r"\bgit\s+push\b[^;&|]*\s(--force(-with-lease|-if-includes)?(=\S*)?|-f|\+\S+)(\s|$)"
)

BASH_RULES: list[tuple[re.Pattern[str], str]] = [
    (FORCE_PUSH, "Force pushes are not allowed. Ask the user to push."),
    (
        re.compile(r"\bgit\s+commit\b[^;&|]*--no-verify\b"),
        "Commits must run the pre-commit hooks (gitleaks, ruff). Remove --no-verify.",
    ),
    (
        re.compile(
            r"(?:^|[\s;&|(])(?:cat|less|more|head|tail|bat|nl|grep|rg|sed|awk|cp|mv|xxd|strings|source|\.)\s"
            r"[^;&|]*?(?<=[\s/'\"])\.env(?:\.(?!example\b)[\w.-]+)?(?=$|[\s'\";&|])"
        ),
        "Reading or copying .env files is not allowed; secrets stay out of the conversation.",
    ),
    (
        re.compile(r"(^|[;&|]\s*)(env|printenv|set|export\s+-p)\s*($|[;&|])"),
        "Printing the whole environment could expose secrets.",
    ),
    (
        re.compile(r"\bprintenv\s+\w*(KEY|TOKEN|SECRET|PASSWORD)\w*", re.IGNORECASE),
        "Printing secret environment variables is not allowed.",
    ),
    (
        re.compile(r"\becho\b[^;&|]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*\}?", re.IGNORECASE),
        "Printing secret environment variables is not allowed.",
    ),
    (
        re.compile(r"\bkeyring\s+get\b|\bsecurity\s+find-generic-password\b|\bsecret-tool\s+lookup\b"),
        "Reading secrets from the system keychain is not allowed.",
    ),
    (
        re.compile(r"\brm\s+(-[\w-]+\s+)*(/|/\*|~|~/|\$HOME|\$HOME/)(\s|$)"),
        "Deleting the home or root folder is not allowed.",
    ),
]


def temp_roots() -> list[str]:
    roots = {tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders", "/var/folders"}
    return [r.rstrip("/") + "/" for r in roots if r]


def is_temp(path: str) -> bool:
    return any(path.startswith(root) for root in temp_roots()) or "pytest-" in path


def project_dir(data: dict) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()).resolve()


def touches_real_app_data(raw_path: str, project: Path) -> bool:
    """True for paths outside the project and temp folders inside a 'jobpipeline' folder."""
    expanded = os.path.expanduser(raw_path)
    path = Path(expanded)
    if not path.is_absolute():
        path = project / path
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = resolved.as_posix()
    if text.startswith(project.as_posix() + "/") or text == project.as_posix():
        return False
    if is_temp(text):
        return False
    return any(part.lower() == APP_DIR_NAME for part in resolved.parts)


def block(message: str) -> int:
    print(f"Blocked by .claude/hooks/pre_tool_guard.py: {message}", file=sys.stderr)
    return 2


def ask(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def check_file_tool(tool: str, tool_input: dict, project: Path) -> int:
    raw = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw:
        return 0
    posix = Path(os.path.expanduser(raw)).as_posix()
    for pattern in SECRET_PATH_PATTERNS:
        if pattern.search(posix):
            return block(f"{tool} on '{raw}' could expose secrets. Use .env.example for documentation.")
    if touches_real_app_data(raw, project):
        return block(
            f"'{raw}' is inside the user's real Job Search Pipeline data or config folder. "
            "Use a temporary data folder (--data-dir) for development and tests."
        )
    if tool in ("Edit", "Write") and any(p.search(posix) for p in GUARDRAIL_PATHS):
        return ask("This edit changes the project's guardrails (settings or hooks). The user must approve it.")
    return 0


REAL_DATA_HINTS = [
    re.compile(rf"Application Support/{APP_DIR_NAME}\b", re.IGNORECASE),
    re.compile(rf"AppData[\\/](Local|Roaming)[\\/]{APP_DIR_NAME}\b", re.IGNORECASE),
]


def check_bash(command: str, project: Path) -> int:
    for pattern, message in BASH_RULES:
        if pattern.search(command):
            return block(message)
    real_data_message = (
        "The user's real Job Search Pipeline data and config folders are off limits. "
        "Use a temporary data folder (--data-dir) instead."
    )
    if any(hint.search(command) for hint in REAL_DATA_HINTS):
        return block(real_data_message)
    for token in re.split(r"[\s'\"=]+", command):
        if f"/{APP_DIR_NAME}" in token.lower() and token.startswith(("/", "~", "$HOME")):
            expanded = os.path.expanduser(token.replace("$HOME", "~", 1))
            if expanded.startswith(project.as_posix() + "/"):
                continue
            if not is_temp(expanded):
                return block(real_data_message)
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    project = project_dir(data)
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        return check_file_tool(tool, tool_input, project)
    if tool == "Bash":
        return check_bash(str(tool_input.get("command") or ""), project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
