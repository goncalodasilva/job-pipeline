"""Shared, stdlib-only hook policy; native adapters live in agent_hook.py (ADR-096).

Hooks catch common mistakes. They complement native permissions and the sandbox;
they cannot interpret arbitrary programs executed through a shell.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Decision:
    action: Literal["allow", "deny", "ask"]
    reason: str = ""


SECRET_PATHS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(^|/)\.env$",
        r"(^|/)\.env\.(?!example$)[^/]+$",
        r"\.(pem|key|p12|pfx)$",
        r"(^|/)id_(rsa|ecdsa|ed25519)(\.pub)?$",
        r"(^|/)\.(aws|ssh|gnupg)(/|$)",
        r"(^|/)\.credentials\.json$",
    )
)
GUARDRAIL_PATHS = (
    ".claude/settings.json",
    ".claude/hooks",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".codex/hooks",
    ".codex/rules",
    "scripts/agent_support.py",
    "scripts/agent_hook.py",
)
FORCE_PUSH = re.compile(
    r"\bgit\s+push\b[^;&|]*\s(--force(-with-lease|-if-includes)?(=\S*)?|-f|\+\S+)(\s|$)"
)
SHELL_DENIALS = (
    (FORCE_PUSH, "Force pushes are not allowed. Ask the user to push."),
    (
        re.compile(
            r"(?:^|[\s;&|(\'\"])(?:cat|less|more|head|tail|bat|nl|grep|rg|sed|awk|cp|mv|xxd|strings|source|\.)\s"
            r"[^;&|]*?(?<=[\s/'\"])\.env(?:\.(?!example\b)[\w.-]+)?(?=$|[\s'\";&|])"
        ),
        "Reading or copying .env files is not allowed.",
    ),
    (re.compile(r"\bgit\s+commit\b[^;&|]*--no-verify\b"), "Commits must run pre-commit hooks."),
    (
        re.compile(r"(^|[;&|]\s*)(env|printenv|set|export\s+-p)\s*($|[;&|])"),
        "Do not print the whole environment.",
    ),
    (
        re.compile(r"\bprintenv\s+\w*(KEY|TOKEN|SECRET|PASSWORD)\w*", re.I),
        "Do not print secret environment variables.",
    ),
    (
        re.compile(r"\becho\b[^;&|]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*\}?", re.I),
        "Do not print secret environment variables.",
    ),
    (
        re.compile(
            r"\bkeyring\s+get\b|\bsecurity\s+find-generic-password\b|\bsecret-tool\s+lookup\b"
        ),
        "Do not read system keychain secrets.",
    ),
    (
        re.compile(r"\brm\s+(-[\w-]+\s+)*(/|/\*|~|~/|\$HOME|\$HOME/)(\s|$)"),
        "Do not delete home or root.",
    ),
)
SHELL_MUTATION = re.compile(
    r"(?:^|[\s;&|])(?:rm|mv|cp|tee|touch|install|truncate|chmod)\s|"
    r"\bsed\s+-\S*i\b|(?<![<])>(?!&)|write_text|write_bytes|\.unlink\(|\.rename\("
)
REAL_DATA_HINTS = re.compile(
    r"Application Support/jobpipeline\b|AppData[\\/](Local|Roaming)[\\/]jobpipeline\b", re.I
)
PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.M)
FILE_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")
SHELL_TOOLS = ("Bash", "exec_command", "shell_command")


def project_root(start: Path | None = None) -> Path:
    """Resolve against this checkout, including a linked worktree and nested cwd."""
    start = (start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        if (parent / "docs/PROGRESS.md").is_file():
            return parent
    return start


def operation_dir(data: Mapping[str, object]) -> Path:
    """Keep the session checkout root separate from a tool's actual working directory."""
    base = Path(string(data.get("cwd")) or os.getcwd()).resolve()
    args = tool_input(data)
    workdir = string(args.get("workdir")) or string(args.get("cwd"))
    return (base / workdir).resolve() if workdir else base


def patch_paths(command: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(PATCH_PATH.findall(command)))


def string(value: object) -> str:
    return value if isinstance(value, str) else ""


def tool_input(data: Mapping[str, object]) -> Mapping[str, object]:
    value = data.get("tool_input")
    return value if isinstance(value, dict) else {}


def call_paths(data: Mapping[str, object]) -> tuple[str, ...]:
    tool = string(data.get("tool_name")).rsplit(".", 1)[-1]
    args = tool_input(data)
    if tool == "apply_patch":
        command = (
            string(data.get("tool_input"))
            or string(args.get("command"))
            or string(args.get("input"))
            or string(args.get("patch"))
        )
        return (
            patch_paths(command)
            if command.startswith("*** Begin Patch\n")
            and command.rstrip().endswith("*** End Patch")
            else ()
        )
    if tool in FILE_TOOLS:
        path = (
            string(args.get("file_path"))
            or string(args.get("path"))
            or string(args.get("notebook_path"))
        )
        return (path,) if path else ()
    return ()


def is_temp(path: Path) -> bool:
    # These are OS temporary roots, not locations where this helper creates files.
    roots = (tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders", "/var/folders")  # noqa: S108
    return any(path.is_relative_to(Path(root).resolve()) for root in roots)


def check_path(raw: str, root: Path, *, writing: bool, cwd: Path | None = None) -> Decision:
    path = Path(os.path.expanduser(raw))
    resolved = (path if path.is_absolute() else (cwd or root) / path).resolve()
    if any(
        pattern.search(candidate)
        for pattern in SECRET_PATHS
        for candidate in (path.as_posix(), resolved.as_posix())
    ):
        return Decision("deny", "Secret files are off limits. Use .env.example for documentation.")
    if (
        not resolved.is_relative_to(root)
        and not is_temp(resolved)
        and "jobpipeline" in (part.lower() for part in resolved.parts)
    ):
        return Decision(
            "deny",
            "The user's real jobpipeline data/config folders are off limits; "
            "use temporary --data-dir.",
        )
    if writing and resolved.is_relative_to(root):
        relative = resolved.relative_to(root).as_posix()
        if any(
            relative == protected or relative.startswith(protected + "/")
            for protected in GUARDRAIL_PATHS
        ):
            return Decision(
                "ask",
                "Changing settings, hooks or their shared policy requires the user's approval.",
            )
    return Decision("allow")


def check_shell(command: str, root: Path, cwd: Path) -> Decision:
    for pattern, reason in SHELL_DENIALS:
        if pattern.search(command):
            return Decision("deny", reason)
    if REAL_DATA_HINTS.search(command):
        return Decision("deny", "The user's real jobpipeline data/config folders are off limits.")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return Decision("deny", "Cannot parse the shell command for the project policy.")
    result = Decision("allow")
    writing = SHELL_MUTATION.search(command) is not None
    for token in tokens:
        # Inspect path-shaped tokens; never echo the command or its values into hook output.
        if "=" in token:
            token = token.split("=", 1)[1]
        elif token.startswith("-"):
            continue
        if not token or "\n" in token:
            continue
        decision = check_path(token.replace("$HOME/", "~/"), root, writing=writing, cwd=cwd)
        if decision.action == "deny":
            return decision
        if decision.action == "ask":
            result = decision
    return result


def evaluate(data: Mapping[str, object]) -> Decision:
    root = project_root(Path(string(data.get("cwd")) or os.getcwd()))
    cwd = operation_dir(data)
    tool = string(data.get("tool_name")).rsplit(".", 1)[-1]
    if tool in SHELL_TOOLS:
        args = tool_input(data)
        command = string(args.get("command")) or string(args.get("cmd"))
        return (
            check_shell(command, root, cwd)
            if command
            else Decision("deny", "Missing shell command in hook input.")
        )
    if tool not in (*FILE_TOOLS, "apply_patch"):
        return Decision("allow")
    paths = call_paths(data)
    if not paths:
        return Decision("deny", "Missing file paths or unrecognized patch payload in hook input.")
    result = Decision("allow")
    for path in paths:
        decision = check_path(path, root, writing=tool != "Read", cwd=cwd)
        if decision.action == "deny":
            return decision
        if decision.action == "ask":
            result = decision
    return result


def post_edit(data: Mapping[str, object]) -> int:
    root = project_root(Path(string(data.get("cwd")) or os.getcwd()))
    cwd = operation_dir(data)
    files = tuple(
        dict.fromkeys(
            (cwd / path).resolve()
            for path in call_paths(data)
            if path.endswith(".py")
            and (cwd / path).is_file()
            and check_path(path, root, writing=False, cwd=cwd).action == "allow"
            and (cwd / path).resolve().is_relative_to(root)
        )
    )
    uv = shutil.which("uv")
    if not files or not (root / "pyproject.toml").is_file() or uv is None:
        return 0
    for check in (("format",), ("check", "--fix")):
        try:
            result = subprocess.run(  # noqa: S603 — resolved uv, fixed arguments, no shell
                [
                    uv,
                    "run",
                    "--offline",
                    "--no-sync",
                    "ruff",
                    *check,
                    *(str(file) for file in files),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(
                "Post-edit ruff unavailable; run scripts/verify.sh before committing.",
                file=sys.stderr,
            )
            return 0
        if result.returncode:
            print(
                f"Post-edit ruff {check[0]} failed:\n{(result.stdout + result.stderr)[-3000:]}",
                file=sys.stderr,
            )
            return 2
    return 0
