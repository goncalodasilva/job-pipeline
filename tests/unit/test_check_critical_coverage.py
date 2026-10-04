"""Critical coverage script (M0.6, spec 4.3, ADR-028).

Each test builds a fictional repository tree in ``tmp_path`` (touching the critical module
files that should exist) and a coverage.py-shaped JSON report, then runs the script's
``main`` with ``--root`` pointing at that tree. The real repository is only read to
check that the registry matches spec section 4.3.
"""

import importlib
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "TECHNICAL_SPEC.md"
SPEC_BULLET_PREFIX = "- At least 95% branch coverage for critical modules:"
BACKTICKED_APP_PATH = re.compile(r"`(app/[^`]+\.py)`")

SECURITY = "app/api/security.py"
RUNNER = "app/tasks/runner.py"
DETECTION = "app/onboarding/detection.py"
BUDGETS = "app/domain/budgets.py"
USAGE = "app/services/usage.py"
GUARD = "app/modules/fetch/guard.py"
HONESTY = "app/domain/honesty.py"

EXPECTED_REGISTRY = (
    (SECURITY, 0),
    (RUNNER, 0),
    (DETECTION, 1),
    (BUDGETS, 2),
    (USAGE, 2),
    (GUARD, 4),
    (HONESTY, 6),
)
ALL_MODULES = tuple(path for path, _ in EXPECTED_REGISTRY)


@pytest.fixture
def checker() -> ModuleType:
    """Import the script per test so a missing script fails each test, not collection."""
    return importlib.import_module("scripts.check_critical_coverage")


def summary(
    *,
    num_branches: int,
    covered_branches: int,
    num_statements: int = 100,
    covered_lines: int = 100,
) -> dict[str, Any]:
    percent = 100.0 * covered_lines / num_statements if num_statements else 100.0
    return {
        "summary": {
            "covered_lines": covered_lines,
            "num_statements": num_statements,
            "percent_covered": percent,
            "missing_lines": num_statements - covered_lines,
            "excluded_lines": 0,
            "num_branches": num_branches,
            "num_partial_branches": 0,
            "covered_branches": covered_branches,
            "missing_branches": num_branches - covered_branches,
        }
    }


def make_repo(root: Path, present: tuple[str, ...]) -> Path:
    """Create the given module files (empty) under ``root``."""
    for rel in present:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return root


def write_report(tmp_path: Path, files: dict[str, dict[str, Any]]) -> Path:
    report = {
        "meta": {"version": "7.6.1", "branch_coverage": True, "show_contexts": False},
        "files": files,
        "totals": {"covered_lines": 0, "num_statements": 0, "percent_covered": 100.0},
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def run(
    checker: ModuleType,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    report: Path,
    *extra: str,
) -> tuple[int, str]:
    code = checker.main(["--json", str(report), "--root", str(root), *extra])
    return code, capsys.readouterr().out


def line_for(output: str, module: str) -> str:
    matches = [line for line in output.splitlines() if module in line]
    assert len(matches) == 1, f"expected one line for {module}, got {matches!r}\n{output}"
    return matches[0]


def assert_every_module_reported(output: str) -> None:
    for module in ALL_MODULES:
        line_for(output, module)


def test_passes_when_all_present_modules_at_or_above_95_branch(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER, DETECTION))
    report = write_report(
        tmp_path,
        {
            SECURITY: summary(num_branches=40, covered_branches=38),  # exactly 95.0%
            RUNNER: summary(num_branches=0, covered_branches=0),  # no branches -> 100%
            DETECTION: summary(num_branches=10, covered_branches=10),
        },
    )

    code, out = run(checker, capsys, root, report)

    assert code == 0
    assert_every_module_reported(out)
    for module in (SECURITY, RUNNER, DETECTION):
        line = line_for(out, module)
        assert "OK" in line
        assert "FAIL" not in line
    for module in (BUDGETS, USAGE, GUARD, HONESTY):
        assert "not yet present" in line_for(out, module)


def test_fails_and_names_module_below_threshold(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER))
    report = write_report(
        tmp_path,
        {
            SECURITY: summary(num_branches=20, covered_branches=20),
            RUNNER: summary(num_branches=50, covered_branches=47),  # 94.0%
        },
    )

    code, out = run(checker, capsys, root, report)

    assert code == 1
    assert_every_module_reported(out)
    runner_line = line_for(out, RUNNER)
    assert "FAIL" in runner_line
    assert "94." in runner_line
    assert "OK" in line_for(out, SECURITY)


def test_existing_critical_file_missing_from_report_fails(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER))
    report = write_report(tmp_path, {SECURITY: summary(num_branches=10, covered_branches=10)})

    code, out = run(checker, capsys, root, report)

    assert code == 1
    runner_line = line_for(out, RUNNER)
    assert "FAIL" in runner_line
    assert "not measured" in runner_line


def test_future_module_absent_reported_not_failed(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER))
    report = write_report(
        tmp_path,
        {
            SECURITY: summary(num_branches=10, covered_branches=10),
            RUNNER: summary(num_branches=10, covered_branches=10),
        },
    )

    code, out = run(checker, capsys, root, report)

    assert code == 0
    honesty_line = line_for(out, HONESTY)
    assert "not yet present" in honesty_line
    assert "FAIL" not in honesty_line

    code, out = run(checker, capsys, root, report, "--milestone", "M0")

    assert code == 0
    assert_every_module_reported(out)
    honesty_line = line_for(out, HONESTY)
    assert "not yet present" in honesty_line
    assert "FAIL" not in honesty_line


@pytest.mark.parametrize("milestone", ["M2", "2"])
def test_module_due_by_milestone_but_absent_fails(
    checker: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    milestone: str,
) -> None:
    present = (SECURITY, RUNNER, DETECTION, USAGE)
    root = make_repo(tmp_path / "repo", present)
    report = write_report(
        tmp_path, {m: summary(num_branches=10, covered_branches=10) for m in present}
    )

    code, out = run(checker, capsys, root, report, "--milestone", milestone)

    assert code == 1
    assert_every_module_reported(out)
    budgets_line = line_for(out, BUDGETS)
    assert "FAIL" in budgets_line
    assert "due by M2" in budgets_line
    for later in (GUARD, HONESTY):
        later_line = line_for(out, later)
        assert "not yet present" in later_line
        assert "FAIL" not in later_line


def test_threshold_uses_branch_not_line_coverage(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER))

    full_lines_half_branches = write_report(
        tmp_path,
        {
            SECURITY: summary(
                num_branches=20, covered_branches=10, num_statements=80, covered_lines=80
            ),
            RUNNER: summary(num_branches=10, covered_branches=10),
        },
    )
    code, out = run(checker, capsys, root, full_lines_half_branches)

    assert code == 1
    assert "FAIL" in line_for(out, SECURITY)

    half_lines_full_branches = write_report(
        tmp_path,
        {
            SECURITY: summary(
                num_branches=20, covered_branches=20, num_statements=80, covered_lines=40
            ),
            RUNNER: summary(num_branches=10, covered_branches=10),
        },
    )
    code, out = run(checker, capsys, root, half_lines_full_branches)

    assert code == 0
    middleware_line = line_for(out, SECURITY)
    assert "OK" in middleware_line
    assert "FAIL" not in middleware_line


def test_windows_style_paths_in_report_are_matched(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY, RUNNER))
    report = write_report(
        tmp_path,
        {
            "app\\api\\security.py": summary(num_branches=10, covered_branches=10),
            str(root / "app" / "tasks" / "runner.py"): summary(
                num_branches=10, covered_branches=10
            ),
        },
    )

    code, out = run(checker, capsys, root, report)

    assert code == 0
    for module in (SECURITY, RUNNER):
        line = line_for(out, module)
        assert "OK" in line
        assert "not measured" not in line


def test_registry_matches_spec_section_4_3(checker: ModuleType) -> None:
    spec_lines = SPEC.read_text(encoding="utf-8").splitlines()
    bullets = [line for line in spec_lines if line.startswith(SPEC_BULLET_PREFIX)]
    assert len(bullets) == 1, "spec 4.3 critical-modules bullet not found exactly once"
    spec_paths = set(BACKTICKED_APP_PATH.findall(bullets[0]))
    assert spec_paths, "no backticked app/...py paths in the spec 4.3 bullet"

    registry: tuple[tuple[str, int], ...] = checker.CRITICAL_MODULES

    assert {path for path, _ in registry} == spec_paths
    assert set(registry) == set(EXPECTED_REGISTRY)  # milestones per ADR-028
    assert checker.THRESHOLD == 95.0


@pytest.mark.parametrize("bad", ["X", "M", "MX", "M-1", ""])
def test_invalid_milestone_argument_rejected(
    checker: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    bad: str,
) -> None:
    root = make_repo(tmp_path / "repo", ())
    report = write_report(tmp_path, {})

    with pytest.raises(SystemExit) as exc_info:
        checker.main(["--json", str(report), "--root", str(root), "--milestone", bad])

    assert exc_info.value.code not in (0, None)
    assert "--milestone" in capsys.readouterr().err


def test_report_without_branch_data_fails(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY,))
    report = write_report(tmp_path, {SECURITY: summary(num_branches=0, covered_branches=0)})
    data = json.loads(report.read_text(encoding="utf-8"))
    data["meta"]["branch_coverage"] = False
    report.write_text(json.dumps(data), encoding="utf-8")

    code, output = run(checker, capsys, root, report)

    assert code == 1
    assert "branch" in output.lower()


def test_absolute_path_outside_root_is_not_matched(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY,))
    elsewhere = (tmp_path / "elsewhere" / SECURITY).resolve()
    report = write_report(tmp_path, {str(elsewhere): summary(num_branches=10, covered_branches=10)})

    code, output = run(checker, capsys, root, report)

    assert code == 1
    assert "not measured" in line_for(output, SECURITY)


MIDDLEWARE_SOURCE = """
def allowed(host):
    if host == "127.0.0.1":
        return True
    return False
"""


def _record_coverage(root: Path, *, calls: tuple[str, ...]) -> None:
    """Measure real branch coverage of a fictional middleware module into root/.coverage."""
    import coverage

    module = root / SECURITY
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(MIDDLEWARE_SOURCE, encoding="utf-8")
    cov = coverage.Coverage(
        data_file=str(root / ".coverage"), branch=True, config_file=False, source=[str(root)]
    )
    namespace: dict[str, Any] = {}
    cov.start()
    try:
        exec(compile(MIDDLEWARE_SOURCE, str(module), "exec"), namespace)  # noqa: S102
        for host in calls:
            namespace["allowed"](host)
    finally:
        cov.stop()
    cov.save()


@pytest.mark.parametrize(
    ("calls", "verdict"), [(("127.0.0.1", "evil"), "OK"), (("evil",), "FAIL (50.0%")]
)
def test_default_report_from_coverage_data_works_from_any_directory(
    checker: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: tuple[str, ...],
    verdict: str,
) -> None:
    root = tmp_path / "repo"
    _record_coverage(root, calls=calls)
    monkeypatch.chdir(tmp_path)  # not the repository root

    code = checker.main(["--root", str(root)])
    output = capsys.readouterr().out

    assert verdict in line_for(output, SECURITY)
    assert code == (0 if verdict == "OK" else 1)


def test_missing_coverage_data_fails_with_a_hint(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY,))

    code = checker.main(["--root", str(root)])

    assert code == 1
    assert "pytest --cov=app" in capsys.readouterr().out


def test_unreadable_report_fails_without_traceback(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", (SECURITY,))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    assert checker.main(["--json", str(broken), "--root", str(root)]) == 1
    assert checker.main(["--json", str(tmp_path / "absent.json"), "--root", str(root)]) == 1
    assert "Cannot read coverage report" in capsys.readouterr().out


# --- --from-progress: the milestone comes from docs/PROGRESS.md (M0.19 follow-up) ----------


def write_progress(root: Path, current: str | None) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    header = f"Current milestone: {current}\n\n" if current is not None else ""
    (docs / "PROGRESS.md").write_text(f"# Progress\n\n{header}## M0 — Fictional\n", "utf-8")


def test_from_progress_requires_modules_of_earlier_milestones(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Current milestone M3: everything due by M2 must exist; M3 and later may be missing."""
    present = (SECURITY, RUNNER, DETECTION, USAGE)
    root = make_repo(tmp_path / "repo", present)
    write_progress(root, "M3")
    report = write_report(
        tmp_path, {m: summary(num_branches=10, covered_branches=10) for m in present}
    )

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 1
    assert "due by M2" in line_for(out, BUDGETS)
    for later in (GUARD, HONESTY):
        assert "not yet present" in line_for(out, later)


def test_from_progress_allows_current_milestone_modules_to_be_missing(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """While M2 is in progress, its critical modules may not exist yet."""
    present = (SECURITY, RUNNER, DETECTION)
    root = make_repo(tmp_path / "repo", present)
    write_progress(root, "M2")
    report = write_report(
        tmp_path, {m: summary(num_branches=10, covered_branches=10) for m in present}
    )

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 0, out
    assert "not yet present" in line_for(out, BUDGETS)
    assert "not yet present" in line_for(out, USAGE)


def test_from_progress_catches_a_renamed_module_after_its_milestone(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The M0.19 bug: a registry path that doesn't exist must fail once its milestone is past."""
    present = (RUNNER,)  # the security middleware is missing (renamed or moved)
    root = make_repo(tmp_path / "repo", present)
    write_progress(root, "M1")
    report = write_report(tmp_path, {RUNNER: summary(num_branches=10, covered_branches=10)})

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 1
    assert "FAIL (missing, due by M0)" in line_for(out, SECURITY)


def test_from_progress_in_m0_requires_nothing_missing(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """M0 has no earlier milestone; acceptance's inclusive --milestone M0 is the backstop."""
    root = make_repo(tmp_path / "repo", ())
    write_progress(root, "M0")
    report = write_report(tmp_path, {})

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 0, out
    for module in ALL_MODULES:
        assert "not yet present" in line_for(out, module)


def test_from_progress_without_progress_file_fails(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", ALL_MODULES)
    report = write_report(tmp_path, {})

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 1
    assert "current milestone" in out.lower()


def test_current_milestone_line_must_be_on_one_line(checker: ModuleType, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PROGRESS.md").write_text("Current milestone:\nM5\n", encoding="utf-8")

    with pytest.raises(checker.ProgressError):
        checker.current_milestone(tmp_path)


@pytest.mark.parametrize("current", [None, "soon"])
def test_from_progress_fails_closed_without_a_current_milestone(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path, current: str | None
) -> None:
    root = make_repo(tmp_path / "repo", ALL_MODULES)
    write_progress(root, current)
    report = write_report(
        tmp_path, {m: summary(num_branches=10, covered_branches=10) for m in ALL_MODULES}
    )

    code, out = run(checker, capsys, root, report, "--from-progress")

    assert code == 1
    assert "Current milestone" in out


def test_from_progress_and_milestone_are_exclusive(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "repo", ALL_MODULES)
    write_progress(root, "M1")
    report = write_report(tmp_path, {})

    with pytest.raises(SystemExit) as excinfo:
        run(checker, capsys, root, report, "--from-progress", "--milestone", "M1")

    assert excinfo.value.code == 2


def test_repository_progress_names_a_current_milestone(checker: ModuleType) -> None:
    """CI relies on the real docs/PROGRESS.md having a parseable Current milestone line."""
    assert checker.current_milestone(checker.REPO_ROOT) >= 0
