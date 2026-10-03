"""Pre-commit with pinned ruff and gitleaks, no gitleaks allowlist (M0.3, AC-M0.7 part).

These tests only read configuration files. They never run pre-commit, whose first
run downloads hook environments over the network.
"""

import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
RUFF_REPO = "https://github.com/astral-sh/ruff-pre-commit"
GITLEAKS_REPO = "https://github.com/gitleaks/gitleaks"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
FROZEN_REV_LINE = re.compile(
    r"^\s*rev:\s*['\"]?(?P<sha>[0-9a-f]{40})['\"]?\s+#\s*frozen:\s*v(?P<version>\d+\.\d+\.\d+)\s*$"
)
# Flags that would point gitleaks at a config, allowlist, baseline or ignore file.
GITLEAKS_ALLOWLIST_FLAGS = (
    "-c",
    "--config",
    "-b",
    "--baseline-path",
    "-i",
    "--gitleaks-ignore-path",
)


def read_precommit_text() -> str:
    assert PRECOMMIT_CONFIG.is_file(), f"missing {PRECOMMIT_CONFIG.name} at the repository root"
    return PRECOMMIT_CONFIG.read_text(encoding="utf-8")


def load_precommit_repos() -> list[dict[str, Any]]:
    config = yaml.safe_load(read_precommit_text())
    assert isinstance(config, dict), "pre-commit config must be a mapping"
    repos = config.get("repos")
    assert isinstance(repos, list), "pre-commit config must have a 'repos' list"
    assert repos, "pre-commit config must declare at least one repo"
    return repos


def find_repo(repos: list[dict[str, Any]], url: str) -> dict[str, Any]:
    matches = [repo for repo in repos if repo.get("repo") == url]
    assert len(matches) == 1, f"expected exactly one entry for {url}, found {len(matches)}"
    return matches[0]


def hook_ids(repo: dict[str, Any]) -> list[str]:
    return [str(hook.get("id")) for hook in repo.get("hooks", [])]


def test_precommit_config_has_pinned_ruff_and_gitleaks() -> None:
    repos = load_precommit_repos()

    ruff = find_repo(repos, RUFF_REPO)
    gitleaks = find_repo(repos, GITLEAKS_REPO)

    ruff_hooks = hook_ids(ruff)
    assert "ruff-check" in ruff_hooks
    assert "ruff-format" in ruff_hooks
    assert ruff_hooks.index("ruff-check") < ruff_hooks.index("ruff-format"), (
        "ruff-check (with --fix) must run before ruff-format"
    )
    ruff_check = next(hook for hook in ruff["hooks"] if hook.get("id") == "ruff-check")
    assert "--fix" in ruff_check.get("args", [])
    assert "gitleaks" in hook_ids(gitleaks)

    for repo in repos:
        if repo.get("repo") == "local":
            continue
        rev = repo.get("rev")
        assert isinstance(rev, str), f"{repo.get('repo')} has no string rev"
        assert COMMIT_SHA.fullmatch(rev), (
            f"{repo.get('repo')} rev {rev!r} must be a full 40-char lowercase commit SHA"
        )


def locked_ruff_version() -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [pkg["version"] for pkg in lock.get("package", []) if pkg.get("name") == "ruff"]
    assert len(versions) == 1, f"expected one locked ruff version, found {versions}"
    return str(versions[0])


def ruff_hook_frozen_version(text: str) -> str:
    # YAML parsers drop comments, so the "# frozen:" note is read from the raw lines.
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if RUFF_REPO in line),
        None,
    )
    assert start is not None, f"{RUFF_REPO} not found in pre-commit config"
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("- repo:"):
            break
        if line.lstrip().startswith("rev:"):
            match = FROZEN_REV_LINE.match(line)
            assert match, f"ruff rev line must be '<sha>  # frozen: vX.Y.Z', got {line!r}"
            return match["version"]
    raise AssertionError("ruff-pre-commit entry has no rev line")


def test_ruff_hook_version_matches_locked_ruff() -> None:
    hook_version = ruff_hook_frozen_version(read_precommit_text())

    assert hook_version == locked_ruff_version()


def test_ruff_honours_excludes_when_given_file_names() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = pyproject["tool"]["ruff"]

    assert ruff.get("force-exclude") is True
    assert ".claude" in ruff.get("extend-exclude", [])
    assert "docs" in ruff.get("extend-exclude", [])


def test_gitleaks_runs_without_allowlist() -> None:
    assert not (REPO_ROOT / ".gitleaks.toml").exists()
    assert not (REPO_ROOT / ".gitleaksignore").exists()

    gitleaks = find_repo(load_precommit_repos(), GITLEAKS_REPO)
    hooks = [hook for hook in gitleaks.get("hooks", []) if hook.get("id") == "gitleaks"]
    assert hooks, "the gitleaks repo must provide the gitleaks hook"
    for hook in hooks:
        tokens = [str(arg) for arg in hook.get("args", [])]
        tokens += shlex.split(str(hook.get("entry", "")))
        for token in tokens:
            flag = token.split("=", 1)[0]
            assert flag not in GITLEAKS_ALLOWLIST_FLAGS, f"gitleaks hook must not use {token!r}"


def tracked_files() -> list[Path]:
    # Fixed argv, no shell, no untrusted input.
    listing = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    names = [name for name in listing.stdout.decode("utf-8").split("\0") if name]
    assert names, "git ls-files returned no files"
    return [REPO_ROOT / name for name in names]


def test_no_inline_gitleaks_allow_comments() -> None:
    marker = "gitleaks" + ":allow"  # built at runtime so this file doesn't contain it
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in tracked_files()
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert offenders == []


def test_gitleaks_history_hook_scans_commits() -> None:
    gitleaks = find_repo(load_precommit_repos(), GITLEAKS_REPO)
    history = [
        hook for hook in gitleaks.get("hooks", []) if hook.get("alias") == "gitleaks-history"
    ]

    assert len(history) == 1, "a gitleaks-history hook must scan committed history (CI, verify)"
    hook = history[0]
    assert hook.get("id") == "gitleaks"
    assert hook.get("stages") == ["manual"], "history scanning runs on demand, not per commit"
    assert hook.get("always_run") is True, "without always_run pre-commit skips it: no files"
    entry = shlex.split(str(hook.get("entry", "")))
    assert entry[:2] == ["gitleaks", "git"]
    assert "--staged" not in entry, "--staged scans only the index and finds nothing in CI"
    assert "--pre-commit" not in entry
