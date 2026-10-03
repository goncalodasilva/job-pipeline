"""GitHub Actions CI workflow skeleton (M0.5, AC-M0.7 part, spec 4.6, ADR-022, ADR-032).

These tests only read `.github/workflows/ci.yml` and `pyproject.toml`. They never call
GitHub or run the workflow; evidence of a green run is recorded separately.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEST_JOBS = ("tests", "tests-other-os")
EXPECTED_OSES = {"ubuntu-latest", "macos-latest", "windows-latest"}
EXPECTED_PYTHONS = ["3.11", "3.14"]
SYNC_COMMAND = "uv sync --locked"
FAST_TEST_COMMAND = 'uv run pytest -m "not e2e and not smoke and not live"'
LINT_COMMANDS = (
    SYNC_COMMAND,
    "uv run ruff format --check .",
    "uv run ruff check .",
    "uv run mypy app",
    "uv run lint-imports",
    "uv run pre-commit run --hook-stage manual gitleaks-history",
)
EXCLUDES_PUSH = "github.event_name != 'push'"
PINNED_USES = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s]+)?@[0-9a-f]{40}")
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*['\"]?(?P<value>[^'\"\s#]+)['\"]?\s+#\s*v\d+\.\d+\.\d+\b"
)
PINNED_UV_VERSION = re.compile(r"\d+\.\d+\.\d+")
PYTHON_CLASSIFIER = re.compile(r"Programming Language :: Python :: (?P<version>3\.\d+)$")
SETUP_UV = "astral-sh/setup-uv@"
MATRIX_PYTHON = "${{ matrix.python }}"
MATRIX_OS = "${{ matrix.os }}"


def read_workflow_text() -> str:
    assert CI_WORKFLOW.is_file(), (
        f"missing CI workflow at {CI_WORKFLOW.relative_to(REPO_ROOT).as_posix()}"
    )
    return CI_WORKFLOW.read_text(encoding="utf-8")


def load_workflow() -> dict[Any, Any]:
    workflow = yaml.safe_load(read_workflow_text())
    assert isinstance(workflow, dict), "CI workflow must be a mapping"
    return workflow


def workflow_triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key `on:` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "CI workflow 'on' must be a mapping of events"
    return triggers


def workflow_jobs(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "CI workflow must define jobs"
    return jobs


def get_job(workflow: dict[Any, Any], name: str) -> dict[str, Any]:
    jobs = workflow_jobs(workflow)
    assert name in jobs, f"CI workflow has no '{name}' job (jobs: {sorted(jobs)})"
    job = jobs[name]
    assert isinstance(job, dict), f"job '{name}' must be a mapping"
    return job


def job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, "job must have steps"
    return steps


def run_commands(job: dict[str, Any]) -> str:
    return "\n".join(str(step["run"]) for step in job_steps(job) if "run" in step)


def setup_uv_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job_steps(job) if str(step.get("uses", "")).startswith(SETUP_UV)]


def matrix(job: dict[str, Any]) -> dict[str, Any]:
    strategy = job.get("strategy")
    assert isinstance(strategy, dict), "test job must have a strategy"
    values = strategy.get("matrix")
    assert isinstance(values, dict), "test job strategy must have a matrix"
    return values


def all_uses(workflow: dict[Any, Any]) -> list[str]:
    values: list[str] = []
    for job in workflow_jobs(workflow).values():
        if "uses" in job:
            values.append(str(job["uses"]))
        values.extend(str(step["uses"]) for step in job.get("steps", []) if "uses" in step)
    return values


def load_pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def python_classifiers(pyproject: dict[str, Any]) -> list[str]:
    found = []
    for classifier in pyproject["project"].get("classifiers", []):
        match = PYTHON_CLASSIFIER.match(classifier)
        if match:
            found.append(match["version"])
    return found


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def job_oses(job: dict[str, Any]) -> set[str]:
    runs_on = job.get("runs-on")
    if runs_on == MATRIX_OS:
        oses = matrix(job).get("os")
        assert isinstance(oses, list), "runs-on uses matrix.os but the matrix has no os list"
        return {str(os_name) for os_name in oses}
    assert isinstance(runs_on, str), f"unexpected runs-on {runs_on!r}"
    return {runs_on}


def test_ci_matrix_covers_three_os_and_oldest_newest_python() -> None:
    workflow = load_workflow()
    pyproject = load_pyproject()

    oses: set[str] = set()
    for name in TEST_JOBS:
        job = get_job(workflow, name)
        oses |= job_oses(job)
        pythons = [str(version) for version in matrix(job).get("python", [])]
        assert pythons == EXPECTED_PYTHONS, f"{name} must test Python {EXPECTED_PYTHONS}"
        assert job["strategy"].get("fail-fast") is False, f"{name} must set fail-fast: false"
        uv_steps = setup_uv_steps(job)
        assert uv_steps, f"{name} must install uv with astral-sh/setup-uv"
        for step in uv_steps:
            assert step.get("with", {}).get("python-version") == MATRIX_PYTHON, (
                f"{name} setup-uv must pass python-version: {MATRIX_PYTHON}"
            )
    assert oses == EXPECTED_OSES

    assert get_job(workflow, "tests").get("runs-on") == "ubuntu-latest"
    other = get_job(workflow, "tests-other-os")
    assert other.get("runs-on") == MATRIX_OS
    assert sorted(matrix(other).get("os", [])) == ["macos-latest", "windows-latest"]

    oldest, newest = EXPECTED_PYTHONS[0], EXPECTED_PYTHONS[-1]
    assert pyproject["project"]["requires-python"] == f">={oldest}"
    classifiers = python_classifiers(pyproject)
    assert oldest in classifiers
    assert max(classifiers, key=version_key) == newest


def test_ci_runs_macos_and_windows_only_off_push() -> None:
    workflow = load_workflow()
    triggers = workflow_triggers(workflow)

    assert "push" in triggers, "CI must run on push"
    push = triggers["push"] or {}
    assert not {"branches", "branches-ignore", "paths", "paths-ignore"} & set(push), (
        "push must run on every branch without filters"
    )
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "CI must run on pull requests to main"
    assert pull_request.get("branches") == ["main"]
    assert "workflow_dispatch" in triggers, "CI must allow manual dispatch (ADR-032)"
    schedule = triggers.get("schedule")
    assert isinstance(schedule, list) and len(schedule) == 1, "CI needs one weekly schedule"
    fields = str(schedule[0].get("cron", "")).split()
    assert len(fields) == 5, f"cron must have 5 fields, got {fields}"
    minute, hour, day_of_month, month, day_of_week = fields
    assert day_of_month == "*" and month == "*", "weekly cron must not restrict day or month"
    assert re.fullmatch(r"[0-6]|SUN|MON|TUE|WED|THU|FRI|SAT", day_of_week, re.IGNORECASE), (
        f"weekly cron day-of-week must be a single day, got {day_of_week!r}"
    )
    assert minute.isdigit() and hour.isdigit(), "weekly cron must fire once at a fixed time"

    tests_if = str(get_job(workflow, "tests").get("if", ""))
    assert EXCLUDES_PUSH not in tests_if, "Ubuntu tests must run on every push"
    other_if = str(get_job(workflow, "tests-other-os").get("if", ""))
    assert EXCLUDES_PUSH in other_if, "macOS and Windows must not run on plain pushes"

    for name in TEST_JOBS:
        commands = run_commands(get_job(workflow, name))
        assert SYNC_COMMAND in commands, f"{name} must run '{SYNC_COMMAND}'"
        assert FAST_TEST_COMMAND in commands, f"{name} must run '{FAST_TEST_COMMAND}'"


def test_ci_lint_job_runs_all_static_checks_and_history_secret_scan() -> None:
    lint = get_job(load_workflow(), "lint")

    assert lint.get("runs-on") == "ubuntu-latest"
    commands = run_commands(lint)
    missing = [command for command in LINT_COMMANDS if command not in commands]
    assert missing == [], f"lint job is missing commands: {missing}"

    checkouts = [
        step
        for step in job_steps(lint)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, "lint job must check out the repository once"
    assert checkouts[0].get("with", {}).get("fetch-depth") == 0, (
        "gitleaks-history needs the full history: fetch-depth: 0"
    )


def test_ci_actions_pinned_to_commit_shas() -> None:
    text = read_workflow_text()
    workflow = load_workflow()

    uses = all_uses(workflow)
    assert uses, "CI workflow uses no actions"
    for value in uses:
        assert PINNED_USES.fullmatch(value), f"{value!r} must be pinned to a 40-char commit SHA"

    commented = {match["value"] for line in text.splitlines() if (match := USES_LINE.match(line))}
    uncommented = [value for value in uses if value not in commented]
    assert uncommented == [], f"pinned actions need a '# vX.Y.Z' comment: {uncommented}"

    for name, job in workflow_jobs(workflow).items():
        uv_steps = setup_uv_steps(job)
        assert uv_steps, f"job '{name}' must install uv with astral-sh/setup-uv"
        for step in uv_steps:
            version = str(step.get("with", {}).get("version", ""))
            assert PINNED_UV_VERSION.fullmatch(version), (
                f"job '{name}' setup-uv must pin uv to an exact version, got {version!r}"
            )


def test_ci_has_read_only_permissions() -> None:
    workflow = load_workflow()

    assert workflow.get("permissions") == {"contents": "read"}
    for name, job in workflow_jobs(workflow).items():
        if "permissions" not in job:
            continue
        permissions = job["permissions"]
        assert isinstance(permissions, dict), f"job '{name}' must not use {permissions!r}"
        widened = {scope: level for scope, level in permissions.items() if level == "write"}
        assert widened == {}, f"job '{name}' widens permissions: {widened}"


def test_ci_concurrency_never_cancels_scheduled_or_dispatched_runs() -> None:
    concurrency = load_workflow().get("concurrency")
    assert isinstance(concurrency, dict), "CI must set concurrency"
    group = str(concurrency.get("group", ""))

    # Separate groups per event, so a push can't cancel a weekly or manual cross-OS run.
    for part in ("github.workflow", "github.event_name", "github.ref"):
        assert part in group, f"concurrency group must include {part}"
    assert concurrency.get("cancel-in-progress") == (
        "${{ github.event_name == 'push' || github.event_name == 'pull_request' }}"
    )


def test_ci_jobs_have_timeouts_and_drop_checkout_credentials() -> None:
    for name, job in workflow_jobs(load_workflow()).items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 30, f"job '{name}' needs a timeout"
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, name
