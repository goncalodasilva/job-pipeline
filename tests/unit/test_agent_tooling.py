"""Agent adapters enforce the same policy for Claude tools and Codex patches (ADR-096)."""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def bash_executable() -> str:
    executable = shutil.which("bash")
    assert executable is not None, "Install Bash (Git Bash on Windows) for native hook tests."
    return executable


@pytest.fixture
def support() -> ModuleType:
    return importlib.import_module("scripts.agent_support")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/PROGRESS.md").write_text(
        "Current milestone: M3\n\n## M3 — Fictional work\nStatus: planned\n\n"
        "- [x] M3.1 Finished\n- [ ] M3.2 Next\n  - AC: AC-M3.2\n"
        "  - Checkpoint: after — review\n- [ ] M3.3 Parked\n  - Blocked: a question\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fictional'\n")
    return tmp_path


def event(repo: Path, tool: str, args: object) -> dict[str, object]:
    return {"cwd": str(repo), "tool_name": tool, "tool_input": args}


def patch(path: str, *, move: str = "") -> str:
    return (
        f"*** Begin Patch\n*** Update File: {path}\n"
        + (f"*** Move to: {move}\n" if move else "")
        + "@@\n-old\n+new\n*** End Patch"
    )


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "NotebookEdit"])
@pytest.mark.parametrize("path", [".env", ".env.local", ".ssh/id_ed25519", "private.pem"])
def test_secret_paths_blocked(support: ModuleType, repo: Path, tool: str, path: str) -> None:
    assert support.evaluate(event(repo, tool, {"file_path": path})).action == "deny"


@pytest.mark.parametrize("path", [".env", ".env.local", "private.key"])
@pytest.mark.parametrize("key", ["command", "input", "patch"])
def test_patch_payloads_cannot_write_secrets(
    support: ModuleType, repo: Path, path: str, key: str
) -> None:
    assert support.evaluate(event(repo, "apply_patch", {key: patch(path)})).action == "deny"


def test_patch_checks_every_path_and_rename_destination(support: ModuleType, repo: Path) -> None:
    assert (
        support.evaluate(event(repo, "apply_patch", patch("safe.py", move=".env"))).action == "deny"
    )
    multi = "*** Begin Patch\n*** Add File: safe.py\n+ok\n*** Delete File: .env\n*** End Patch"
    assert support.evaluate(event(repo, "apply_patch", {"command": multi})).action == "deny"


def test_patch_paths_include_deletions_and_moves(support: ModuleType) -> None:
    command = (
        "*** Begin Patch\n*** Update File: old name.py\n*** Move to: new name.py\n"
        "@@\n-old\n+*** Add File: not-a-file.py\n*** Delete File: gone.py\n*** End Patch"
    )
    assert support.patch_paths(command) == ("old name.py", "new name.py", "gone.py")


def test_secret_symlink_is_blocked(support: ModuleType, repo: Path) -> None:
    (repo / ".env").touch()
    (repo / "innocent.txt").symlink_to(repo / ".env")
    assert support.evaluate(event(repo, "Read", {"path": "innocent.txt"})).action == "deny"


def test_real_data_outside_repo_is_blocked(support: ModuleType, repo: Path) -> None:
    path = "/Users/fictional/Library/Application Support/jobpipeline/facts.db"
    assert support.evaluate(event(repo, "Read", {"file_path": path})).action == "deny"


@pytest.mark.parametrize("path", [".env.example", "app/example.py", "tests/fake.py"])
def test_regular_files_and_example_env_allowed(support: ModuleType, repo: Path, path: str) -> None:
    assert support.evaluate(event(repo, "apply_patch", patch(path))).action == "allow"


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".codex/hooks.json",
        ".codex/config.toml",
        ".codex/rules/project.rules",
        "scripts/agent_support.py",
        "scripts/agent_hook.py",
    ],
)
def test_both_tools_guardrails_require_approval(support: ModuleType, repo: Path, path: str) -> None:
    assert support.evaluate(event(repo, "apply_patch", patch(path))).action == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "git push --force-with-lease=main",
        "git push origin +HEAD:main",
        "git commit --no-verify -m example",
        "cat .env",
        "printenv EXAMPLE_TOKEN",
        "cat ~/.ssh/id_ed25519",
        "security find-generic-password -s fictional",
    ],
)
@pytest.mark.parametrize("tool,key", [("Bash", "command"), ("exec_command", "cmd")])
def test_shell_aliases_enforce_policy(
    support: ModuleType, repo: Path, command: str, tool: str, key: str
) -> None:
    assert support.evaluate(event(repo, tool, {key: command})).action == "deny"


@pytest.mark.parametrize("args", [{}, {"command": "not a patch"}, {"command": 1}])
def test_unrecognized_patch_shape_is_blocked(support: ModuleType, repo: Path, args: object) -> None:
    assert support.evaluate(event(repo, "apply_patch", args)).action == "deny"


def test_unknown_non_file_tool_is_allowed(support: ModuleType, repo: Path) -> None:
    assert support.evaluate(event(repo, "update_plan", {"plan": []})).action == "allow"


def test_root_discovery_from_subdirectory_ignores_stale_claude_env(
    support: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = repo / "app/nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/fictional/stale-checkout")
    assert support.project_root(nested) == repo


def run_hook(
    engine: str, kind: str, payload: object, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed interpreter and local script
        [sys.executable, str(ROOT / "scripts/agent_hook.py"), engine, kind],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=cwd,
        check=False,
    )


def test_codex_approval_is_a_block_not_unsupported_ask(repo: Path) -> None:
    result = run_hook("codex", "pre", event(repo, "apply_patch", patch(".codex/hooks.json")), repo)
    assert result.returncode == 2
    assert '"ask"' not in result.stdout
    assert "maintenance" in result.stderr.lower()


def test_claude_approval_uses_native_permission_prompt(repo: Path) -> None:
    result = run_hook(
        "claude", "pre", event(repo, "Edit", {"file_path": ".claude/settings.json"}), repo
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_malformed_policy_input_blocks_without_traceback(repo: Path) -> None:
    result = subprocess.run(  # noqa: S603 — fixed interpreter and local script
        [sys.executable, str(ROOT / "scripts/agent_hook.py"), "codex", "pre"],
        input="not json",
        text=True,
        capture_output=True,
        cwd=repo,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_session_context_from_nested_directory_and_compaction(repo: Path) -> None:
    nested = repo / "app"
    nested.mkdir()
    result = run_hook("codex", "session", {"cwd": str(nested), "source": "compact"}, nested)
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "M3" in context and "M3.2 Next" in context and "M3.3 Parked" in context
    assert "$next-task" in context


def test_post_edit_formats_only_existing_changed_python_files(
    support: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ["one.py", "renamed.py", "untouched.py", "notes.md"]:
        (repo / name).touch()
    command = (
        "*** Begin Patch\n*** Update File: one.py\n@@\n-a\n+b\n"
        "*** Update File: gone.py\n*** Move to: renamed.py\n@@\n-a\n+b\n"
        "*** Delete File: deleted.py\n*** Update File: notes.md\n@@\n-a\n+b\n*** End Patch"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(support.shutil, "which", lambda name: "/fictional/uv")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs["cwd"] == repo
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    assert support.post_edit(event(repo, "apply_patch", {"command": command})) == 0
    assert len(calls) == 2
    for args in calls:
        assert str(repo / "one.py") in args and str(repo / "renamed.py") in args
        assert all(
            "untouched" not in arg and "deleted" not in arg and "notes.md" not in arg
            for arg in args
        )
        assert "--offline" in args and "--no-sync" in args


def test_post_edit_reports_format_failure(
    support: ModuleType,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo / "broken.py").touch()
    monkeypatch.setattr(support.shutil, "which", lambda name: "/fictional/uv")
    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="invalid syntax"),
    )
    assert support.post_edit(event(repo, "Write", {"file_path": "broken.py"})) == 2
    assert "invalid syntax" in capsys.readouterr().err


def test_task_helper_retains_checkpoint_and_blocked_details(repo: Path) -> None:
    result = subprocess.run(  # noqa: S603 — fixed interpreter and local script
        [sys.executable, str(ROOT / "scripts/next_task.py"), "--open"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Checkpoint: after" in result.stdout and "Blocked: a question" in result.stdout
    assert "Task M3.1" not in result.stdout


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_configured_native_pre_hook_blocks_synthetic_secret_patch(engine: str, repo: Path) -> None:
    """Execute the command in the shipped native config, including its real wrapper/root lookup."""
    config = ROOT / (".claude/settings.json" if engine == "claude" else ".codex/hooks.json")
    group = json.loads(config.read_text())["hooks"]["PreToolUse"][0]
    tool = "Write" if engine == "claude" else "apply_patch"
    assert re.search(group["matcher"], tool)
    args = {"file_path": ".env"} if engine == "claude" else {"command": patch(".env")}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}
    result = subprocess.run(  # noqa: S603 — execute the shipped, trusted project hook definition
        [bash_executable(), "-c", group["hooks"][0]["command"]],
        cwd=ROOT / "app",
        input=json.dumps(event(repo, tool, args)),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "Secret files are off limits" in result.stderr


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_configured_session_hook_emits_native_context(engine: str) -> None:
    config = ROOT / (".claude/settings.json" if engine == "claude" else ".codex/hooks.json")
    hook = json.loads(config.read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    result = subprocess.run(  # noqa: S603 — execute the shipped project hook definition
        [bash_executable(), "-c", hook["command"]],
        cwd=ROOT / "app",
        input=json.dumps({"cwd": str(ROOT / "app"), "source": "compact"}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        check=False,
    )
    assert result.returncode == 0
    output = result.stdout
    if engine == "codex":
        output = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "Current milestone:" in output and "AGENTS.md" in output


def test_legacy_helpers_match_shared_commands_from_subdirectory() -> None:
    results = []
    for relative in ("scripts/next_task.py", ".claude/skills/next-task/scripts/next_task.py"):
        results.append(
            subprocess.run(  # noqa: S603 — fixed local scripts
                [sys.executable, str(ROOT / relative)],
                cwd=ROOT / "app",
                text=True,
                capture_output=True,
                check=False,
            )
        )
    assert results[0].returncode == results[1].returncode == 0
    assert results[0].stdout == results[1].stdout

    results = []
    for relative in ("scripts/verify.sh", ".claude/skills/verify/scripts/verify.sh"):
        results.append(
            subprocess.run(  # noqa: S603 — invalid mode exits before running checks
                [bash_executable(), str(ROOT / relative), "invalid-mode"],
                cwd=ROOT / "app",
                text=True,
                capture_output=True,
                check=False,
            )
        )
    assert results[0].returncode == results[1].returncode == 2
    assert results[0].stdout == results[1].stdout


@pytest.mark.parametrize(
    "command",
    [
        "uv run jobpipeline --data-dir=/home/fictional/.local/share/jobpipeline",
        "JOBPIPELINE_DATA_DIR=/home/fictional/.local/share/jobpipeline uv run jobpipeline",
        "cat .env; true",
        "cat .env|head",
        "bash -c 'cat .env'",
    ],
)
def test_shell_options_assignments_and_punctuation_are_checked(
    support: ModuleType,
    repo: Path,
    command: str,
) -> None:
    assert support.evaluate(event(repo, "Bash", {"command": command})).action == "deny"


def test_shell_mutation_with_adjacent_punctuation_needs_approval(
    support: ModuleType,
    repo: Path,
) -> None:
    assert (
        support.evaluate(event(repo, "Bash", {"command": "rm -f .codex/hooks.json;"})).action
        == "ask"
    )


@pytest.mark.parametrize("explicit_workdir", [False, True])
def test_relative_paths_resolve_from_event_or_tool_working_directory(
    support: ModuleType,
    repo: Path,
    explicit_workdir: bool,
) -> None:
    nested = repo / "scripts"
    nested.mkdir()
    payload = event(
        repo if explicit_workdir else nested, "apply_patch", {"command": patch("agent_support.py")}
    )
    if explicit_workdir:
        payload["tool_input"]["workdir"] = str(nested)
    assert support.evaluate(payload).action == "ask"
    assert (
        support.evaluate(
            event(
                repo,
                "exec_command",
                {
                    "cmd": "tee agent_support.py",
                    "workdir": str(nested),
                },
            )
        ).action
        == "ask"
    )
    assert (
        support.evaluate(event(nested, "apply_patch", patch("safe.py", move="../.env"))).action
        == "deny"
    )


def test_post_edit_uses_nested_working_directory(
    support: ModuleType,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = repo / "app"
    nested.mkdir()
    (nested / "changed.py").touch()
    (repo / "changed.py").touch()
    calls: list[list[str]] = []
    monkeypatch.setattr(support.shutil, "which", lambda name: "/fictional/uv")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    assert support.post_edit(event(nested, "apply_patch", patch("changed.py"))) == 0
    assert len(calls) == 2
    assert all(
        str(nested / "changed.py") in args and str(repo / "changed.py") not in args
        for args in calls
    )
