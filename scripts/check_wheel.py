"""Build the wheel and check what a user would install (M9.6 part C; ADR-131 item 9; TD-118).

Usage:
    uv run python scripts/check_wheel.py [--wheel PATH]

Runs ``uv build --wheel`` into a temporary folder (or reads ``--wheel``) and checks:

- every default prompt (``prompts/<task>/prompt.md`` and ``schema.json``) is in the wheel as
  ``app/prompts/...``, byte for byte, and so is the demo dataset (``app/demo_data``), the
  templates, the static files and every migration;
- the version is the project's, and the ``jobpipeline`` command points at ``app.cli:main``;
- nothing else is packaged: every file under ``app/`` except the prompts is one git tracks
  (``git ls-files app``; a file a developer left there, such as a CV, a database or a data
  folder, fails the check), nothing is outside ``app/`` and the wheel's own metadata, no file
  is there twice, and no name looks like tests, docs, scripts, agent settings, secrets,
  databases, logs or caches;
- unpacked, the code finds its prompts and the demo dataset inside the package (the installed
  layout), not in this checkout.

Prints one line per problem and ``check_wheel: PASS`` or ``check_wheel: FAIL``. Exits 0 or 1. No
new dependency: the standard library and ``uv``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "app/__init__.py",
    "app/cli.py",
    "app/demo_data/workspace.json",
    "app/templates/base.html",
    "app/static/css/app.css",
    "app/static/js/app.js",
    "app/static/vendor/htmx.min.js",
)
FORBIDDEN_PARTS = frozenset(
    {"tests", "docs", "scripts", ".claude", ".agents", ".codex", ".github", "__pycache__"}
)
FORBIDDEN_NAME = re.compile(
    r"(^\.env($|\.)|^\.envrc$|^\.netrc$|^\.pypirc$|^credentials\.json$|^secrets\.toml$"
    r"|^config\.toml$|\.(pem|key|p8|p12|pfx|db|sqlite|sqlite3|pyc)$|\.log(\.\d+)?$|-journal$"
    r"|^id_(rsa|dsa|ecdsa|ed25519))",
    re.IGNORECASE,
)
ENTRY_POINT = "jobpipeline = app.cli:main"
PROBE = (
    "import json, app.wiring as w, app.services.demo as d; "
    "print(json.dumps({'prompts': str(w.PROMPTS_DIR), 'demo': str(d.DATASET)}))"
)


def project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        version: str = tomllib.load(handle)["project"]["version"]
    return version


def expected_prompts(root: Path = ROOT) -> dict[str, bytes]:
    """``app/prompts/<task>/<file>`` -> the bytes of the checkout's default prompt files."""
    return {
        f"app/prompts/{path.relative_to(root / 'prompts').as_posix()}": path.read_bytes()
        for path in sorted((root / "prompts").glob("*/*"))
        if path.is_file() and path.name in ("prompt.md", "schema.json")
    }


def expected_migrations(root: Path = ROOT) -> set[str]:
    folder = root / "app" / "modules" / "storage" / "migrations"
    return {f"app/modules/storage/migrations/{path.name}" for path in folder.glob("*.sql")}


def tracked_app_files(root: Path = ROOT) -> set[str] | None:
    """The files under ``app/`` that git tracks and that exist on disk; None when git can't
    list them (not a git checkout, or git missing)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "app"],  # noqa: S607 - git from PATH
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return {name for name in names if name and (root / name).is_file()}


def _dist_info(names: list[str]) -> str | None:
    found = {name.split("/", 1)[0] for name in names if ".dist-info/" in name}
    return next(iter(found)) if len(found) == 1 else None


def check_members(
    wheel: zipfile.ZipFile, root: Path = ROOT, tracked: set[str] | None = None
) -> list[str]:
    """Every problem with what the wheel holds; empty when it is right. ``tracked`` is the set
    of files git tracks under ``app/``; by default it is asked of git in ``root``."""
    if tracked is None:
        tracked = tracked_app_files(root)
    names = [name for name in wheel.namelist() if not name.endswith("/")]
    present = set(names)
    problems: list[str] = [
        f"packaged twice: {name}" for name, count in sorted(Counter(names).items()) if count > 1
    ]
    dist_info = _dist_info(names)
    if dist_info is None:
        problems.append("no single *.dist-info folder")
    for name in names:
        path = PurePosixPath(name)
        top = path.parts[0]
        if top != "app" and top != dist_info:
            problems.append(f"packaged outside app/: {name}")
        if FORBIDDEN_PARTS.intersection(path.parts) or FORBIDDEN_NAME.search(path.name):
            problems.append(f"must not be packaged: {name}")
    for name in REQUIRED_FILES:
        if name not in present:
            problems.append(f"missing: {name}")
    if tracked is None:
        problems.append("git could not list the files of app/; run this in a git checkout")
    else:
        packaged = {n for n in present if n.startswith("app/") and not n.startswith("app/prompts/")}
        for extra in sorted(packaged - tracked):
            problems.append(f"not tracked by git (git add it, or remove it from app/): {extra}")
        for lost in sorted(tracked - packaged):
            problems.append(f"tracked by git but not packaged: {lost}")
    prompts = expected_prompts(root)
    if not prompts:
        problems.append("the checkout has no default prompts to compare")
    for name, data in prompts.items():
        if name not in present:
            problems.append(f"missing prompt: {name}")
        elif wheel.read(name) != data:
            problems.append(f"prompt differs from the checkout: {name}")
    packaged_prompts = {n for n in names if n.startswith("app/prompts/")}
    for extra in sorted(packaged_prompts - set(prompts)):
        problems.append(f"unexpected file in app/prompts: {extra}")
    demo = root / "app" / "demo_data" / "workspace.json"
    if (
        "app/demo_data/workspace.json" in present
        and wheel.read("app/demo_data/workspace.json") != demo.read_bytes()
    ):
        problems.append("the demo dataset differs from the checkout")
    for name in sorted(expected_migrations(root) - present):
        problems.append(f"missing migration: {name}")
    if dist_info is not None:
        metadata = wheel.read(f"{dist_info}/METADATA").decode("utf-8")
        version = project_version(root)
        if f"\nVersion: {version}\n" not in metadata:
            problems.append(f"METADATA does not say Version: {version}")
        entry_points = f"{dist_info}/entry_points.txt"
        if entry_points not in present or ENTRY_POINT not in wheel.read(entry_points).decode():
            problems.append(f"the command is not '{ENTRY_POINT}'")
    return problems


def check_installed_layout(wheel_path: Path) -> list[str]:
    """Unpack the wheel and ask its code where its prompts and demo dataset are."""
    with tempfile.TemporaryDirectory(prefix="jobpipeline-wheel-") as temporary:
        unpacked = Path(temporary).resolve()
        with zipfile.ZipFile(wheel_path) as wheel:
            wheel.extractall(unpacked)
        env = {**os.environ, "PYTHONPATH": str(unpacked)}
        result = subprocess.run(  # noqa: S603 - this interpreter, a fixed probe
            [sys.executable, "-c", PROBE],
            capture_output=True,
            text=True,
            env=env,
            cwd=unpacked,
            check=False,
        )
        if result.returncode != 0:
            return [f"the unpacked wheel does not import: {result.stderr.strip()[-500:]}"]
        found = json.loads(result.stdout.strip().splitlines()[-1])
        problems = []
        prompts, demo = Path(found["prompts"]), Path(found["demo"])
        if prompts != unpacked / "app" / "prompts" or not (prompts / "summarize_job").is_dir():
            problems.append(f"installed PROMPTS_DIR is not the packaged folder: {prompts}")
        if demo != unpacked / "app" / "demo_data" / "workspace.json" or not demo.is_file():
            problems.append(f"installed demo dataset is not the packaged file: {demo}")
        return problems


def build_wheel(out_dir: Path) -> Path:
    result = subprocess.run(  # noqa: S603 - fixed arguments
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],  # noqa: S607 - uv from PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"uv build failed:\n{result.stderr[-2000:]}")
    wheels = sorted(out_dir.glob("jobpipeline-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel in {out_dir}, found {len(wheels)}")
    return wheels[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--wheel", type=Path, help="check this wheel instead of building one")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="jobpipeline-build-") as temporary:
        wheel_path = args.wheel if args.wheel is not None else build_wheel(Path(temporary))
        with zipfile.ZipFile(wheel_path) as wheel:
            problems = check_members(wheel)
            count = len([n for n in wheel.namelist() if not n.endswith("/")])
        if not problems:
            problems = check_installed_layout(wheel_path)
        size_kb = wheel_path.stat().st_size // 1024
        for problem in problems:
            print(f"check_wheel: {problem}")
        if problems:
            noun = "problem" if len(problems) == 1 else "problems"
            print(f"check_wheel: FAIL ({len(problems)} {noun} in {wheel_path.name})")
            return 1
        print(f"check_wheel: PASS ({wheel_path.name}, {count} files, {size_kb} KB)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
