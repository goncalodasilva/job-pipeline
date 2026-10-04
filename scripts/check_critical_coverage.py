"""Enforce branch coverage on the critical modules (spec 4.3, ADR-028).

Usage:
    uv run python scripts/check_critical_coverage.py [--json PATH]
        [--milestone M<n> | --from-progress] [--root DIR]

Reads a coverage.py JSON report (by default it writes one from `.coverage`, so run
`pytest --cov=app` first). Each critical module that exists must be measured and reach
THRESHOLD percent branch coverage. A module that doesn't exist yet is "not yet present",
unless --milestone says the milestone that introduces it has arrived, which is a failure.
--from-progress takes the milestone from the "Current milestone:" line of docs/PROGRESS.md
and requires the modules of every earlier milestone (the current one may still be in
progress), so CI tightens by itself when a milestone is accepted.
Exits 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# (repository-relative path, milestone that introduces it). Keep in sync with spec 4.3;
# a unit test compares the two.
CRITICAL_MODULES: tuple[tuple[str, int], ...] = (
    ("app/api/security.py", 0),
    ("app/tasks/runner.py", 0),
    ("app/onboarding/detection.py", 1),
    ("app/domain/budgets.py", 2),
    ("app/services/usage.py", 2),
    ("app/modules/fetch/guard.py", 4),
    ("app/domain/honesty.py", 6),
)
THRESHOLD = 95.0
REPO_ROOT = Path(__file__).resolve().parent.parent
MILESTONE = re.compile(r"[Mm]?(\d+)")
PROGRESS_FILE = Path("docs") / "PROGRESS.md"
CURRENT_MILESTONE = re.compile(r"^Current milestone:[ \t]*M(\d+)[ \t]*$", re.MULTILINE)


class ProgressError(ValueError):
    """docs/PROGRESS.md is missing or has no ``Current milestone: M<n>`` line."""


def current_milestone(root: Path) -> int:
    """The milestone in progress, from the ``Current milestone:`` line of docs/PROGRESS.md."""
    path = root / PROGRESS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProgressError(f"cannot read {PROGRESS_FILE.as_posix()}: {error}") from error
    match = CURRENT_MILESTONE.search(text)
    if match is None:
        raise ProgressError(f"{PROGRESS_FILE.as_posix()} has no 'Current milestone: M<n>' line")
    return int(match.group(1))


def milestone_number(value: str) -> int:
    match = MILESTONE.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"expected a milestone like M2, got {value!r}")
    return int(match.group(1))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enforce branch coverage on critical modules.")
    parser.add_argument("--json", type=Path, help="coverage.py JSON report")
    due = parser.add_mutually_exclusive_group()
    due.add_argument(
        "--milestone",
        type=milestone_number,
        help="fail if a module due by this milestone (inclusive) is missing, e.g. M2",
    )
    due.add_argument(
        "--from-progress",
        action="store_true",
        help="fail if a module due before the current milestone in docs/PROGRESS.md is missing",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    return parser.parse_args(argv)


def normalise(path: str, root: Path) -> str:
    """Turn a report path (POSIX, Windows or absolute) into a repo-relative POSIX path."""
    parts = PureWindowsPath(path).parts if "\\" in path else PurePosixPath(path).parts
    candidate = Path(*parts)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return candidate.as_posix()
    return PurePosixPath(*candidate.parts).as_posix()


def load_report(json_path: Path | None, root: Path) -> dict[str, Any] | None:
    if json_path is not None:
        report: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
        return report
    data_file = root / ".coverage"
    if not data_file.exists():
        return None
    import coverage  # dev dependency; only needed when no --json report is given

    config = root / "pyproject.toml"
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(root):
        # Report paths are relative to the working directory, so build them from the root.
        out = Path(tmp) / "coverage.json"
        cov = coverage.Coverage(
            data_file=str(data_file), config_file=str(config) if config.is_file() else False
        )
        cov.load()
        cov.json_report(outfile=str(out))
        generated: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
        return generated


def branch_percent(summary: dict[str, Any]) -> float:
    total = int(summary.get("num_branches", 0))
    if total == 0:
        return 100.0
    return 100.0 * int(summary.get("covered_branches", 0)) / total


def check(report: dict[str, Any], root: Path, milestone: int | None) -> tuple[list[str], bool]:
    files = {normalise(path, root): data for path, data in report.get("files", {}).items()}
    lines: list[str] = []
    ok = True
    for module, due in CRITICAL_MODULES:
        if not (root / module).is_file():
            if milestone is not None and due <= milestone:
                lines.append(f"{module}: FAIL (missing, due by M{due})")
                ok = False
            else:
                lines.append(f"{module}: not yet present (arrives in M{due})")
            continue
        if module not in files:
            lines.append(f"{module}: FAIL (not measured)")
            ok = False
            continue
        percent = branch_percent(files[module].get("summary", {}))
        if percent >= THRESHOLD:
            lines.append(f"{module}: OK ({percent:.1f}% branches)")
        else:
            lines.append(f"{module}: FAIL ({percent:.1f}% branches, needs {THRESHOLD:.0f}%)")
            ok = False
    return lines, ok


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = load_report(args.json, args.root)
    except (OSError, ValueError) as error:
        print(f"Cannot read coverage report: {error}")
        return 1
    if report is None:
        print("No coverage data found. Run `uv run pytest --cov=app` first.")
        return 1
    if report.get("meta", {}).get("branch_coverage") is not True:
        # Without branch data every file would look 100% covered, so fail closed.
        print("The coverage report has no branch data. Enable `branch = true` and re-run.")
        return 1
    milestone = args.milestone
    if args.from_progress:
        try:
            current = current_milestone(args.root)
        except ProgressError as error:
            print(f"Cannot tell the current milestone: {error}")
            return 1
        # Modules of the milestone in progress may not exist yet; earlier ones must. In M0
        # nothing is due yet (-1); /milestone-acceptance runs the inclusive --milestone check.
        milestone = current - 1
    lines, ok = check(report, args.root, milestone)
    print("\n".join(lines))
    print("Critical coverage: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
