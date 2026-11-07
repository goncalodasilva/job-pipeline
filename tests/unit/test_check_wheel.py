"""``scripts/check_wheel.py`` (M9.6 part C; TD-118): what it accepts and refuses, on small
fabricated wheels in a fictional checkout. The real build is checked by ``verify.sh full``."""

import zipfile
from pathlib import Path

import pytest

from scripts.check_wheel import (
    ENTRY_POINT,
    REQUIRED_FILES,
    check_installed_layout,
    check_members,
    tracked_app_files,
)

VERSION = "1.0.0"
DIST = f"jobpipeline-{VERSION}.dist-info"


def checkout(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "jobpipeline"\nversion = "{VERSION}"\n'
    )
    for task in ("summarize_job", "assess_fit"):
        (root / "prompts" / task).mkdir(parents=True)
        (root / "prompts" / task / "prompt.md").write_text(f"version: 1\n{task}\n")
        (root / "prompts" / task / "schema.json").write_text("{}")
    (root / "app" / "demo_data").mkdir(parents=True)
    (root / "app" / "demo_data" / "workspace.json").write_text('{"format": 1}')
    migrations = root / "app" / "modules" / "storage" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0001_first.sql").write_text("SELECT 1;")
    return root


def members(root: Path) -> dict[str, bytes]:
    found = {name: b"" for name in REQUIRED_FILES}
    found["app/demo_data/workspace.json"] = (root / "app/demo_data/workspace.json").read_bytes()
    for path in (root / "prompts").glob("*/*"):
        found[f"app/prompts/{path.relative_to(root / 'prompts').as_posix()}"] = path.read_bytes()
    found["app/modules/storage/migrations/0001_first.sql"] = b"SELECT 1;"
    found[f"{DIST}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: jobpipeline\nVersion: {VERSION}\n".encode()
    )
    found[f"{DIST}/entry_points.txt"] = f"[console_scripts]\n{ENTRY_POINT}\n".encode()
    return found


def tracked(content: dict[str, bytes]) -> set[str]:
    """What git would list under ``app/`` for this content: everything but the prompts."""
    return {n for n in content if n.startswith("app/") and not n.startswith("app/prompts/")}


def wheel(path: Path, content: dict[str, bytes]) -> zipfile.ZipFile:
    with zipfile.ZipFile(path, "w") as handle:
        for name, data in content.items():
            handle.writestr(name, data)
    return zipfile.ZipFile(path)


def test_a_complete_wheel_passes(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    with wheel(tmp_path / "ok.whl", content) as built:
        assert check_members(built, root, tracked(content)) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda m: m.pop("app/prompts/summarize_job/prompt.md"), "missing prompt"),
        (lambda m: m.update({"app/prompts/assess_fit/schema.json": b"[]"}), "prompt differs"),
        (lambda m: m.update({"app/prompts/stray/prompt.md": b"x"}), "unexpected file"),
        (lambda m: m.update({"prompts/summarize_job/prompt.md": b"x"}), "outside app/"),
        (lambda m: m.update({"app/tests/test_x.py": b""}), "must not be packaged"),
        (lambda m: m.update({"app/.env": b"X=1"}), "must not be packaged"),
        (lambda m: m.update({"app/data/app.db": b""}), "must not be packaged"),
        (lambda m: m.update({"app/__pycache__/cli.cpython-311.pyc": b""}), "must not be packaged"),
        (lambda m: m.pop("app/demo_data/workspace.json"), "missing: app/demo_data"),
        (lambda m: m.update({"app/demo_data/workspace.json": b"{}"}), "demo dataset differs"),
        (lambda m: m.pop("app/modules/storage/migrations/0001_first.sql"), "missing migration"),
        (lambda m: m.pop("app/templates/base.html"), "missing: app/templates/base.html"),
        (
            lambda m: m.update({f"{DIST}/METADATA": b"Name: jobpipeline\nVersion: 0.1.0\n"}),
            "Version: 1.0.0",
        ),
        (lambda m: m.update({f"{DIST}/entry_points.txt": b"[console_scripts]\n"}), "command"),
    ],
)
def test_a_wrong_wheel_names_the_problem(tmp_path: Path, change, expected: str) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    files = tracked(content)
    change(content)
    with wheel(tmp_path / "bad.whl", content) as built:
        problems = check_members(built, root, files)
    assert any(expected in problem for problem in problems), problems


def test_a_file_packaged_twice_is_named(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    path = tmp_path / "twice.whl"
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(path, "w") as handle:
        for name, data in content.items():
            handle.writestr(name, data)
        handle.writestr("app/demo_data/workspace.json", content["app/demo_data/workspace.json"])
    with zipfile.ZipFile(path) as built:
        problems = check_members(built, root, tracked(content))
    assert problems == ["packaged twice: app/demo_data/workspace.json"]


@pytest.mark.parametrize(
    "stray",
    ["app/imports/cv.pdf", "app/assets/photo-1.jpg", "app/notes.md", "app/data/backups/x.sql"],
)
def test_a_file_git_does_not_track_is_named(tmp_path: Path, stray: str) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    files = tracked(content)
    content[stray] = b"fictional"
    with wheel(tmp_path / "stray.whl", content) as built:
        problems = check_members(built, root, files)
    assert problems == [f"not tracked by git (git add it, or remove it from app/): {stray}"]


def test_a_tracked_file_left_out_of_the_wheel_is_named(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    files = tracked(content) | {"app/templates/pages/today.html"}
    with wheel(tmp_path / "short.whl", content) as built:
        problems = check_members(built, root, files)
    assert problems == ["tracked by git but not packaged: app/templates/pages/today.html"]


def test_outside_a_git_checkout_the_check_fails(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    assert tracked_app_files(root) is None
    with wheel(tmp_path / "ok.whl", members(root)) as built:
        problems = check_members(built, root)
    assert any("git could not list" in problem for problem in problems), problems


@pytest.mark.parametrize(
    "name", ["jobpipeline.log.1", "app.db-journal", "credentials.json", ".netrc", "config.toml"]
)
def test_secret_and_data_names_are_refused_even_when_tracked(tmp_path: Path, name: str) -> None:
    root = checkout(tmp_path / "checkout")
    content = members(root)
    content[f"app/{name}"] = b"fictional"
    with wheel(tmp_path / "named.whl", content) as built:
        problems = check_members(built, root, tracked(content))
    assert f"must not be packaged: app/{name}" in problems


PACKAGED_WIRING = (
    "from pathlib import Path\nPROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'\n"
)
CHECKOUT_WIRING = (
    "from pathlib import Path\nPROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'\n"
)
DEMO = (
    "from pathlib import Path\n"
    "DATASET = Path(__file__).resolve().parent.parent / 'demo_data' / 'workspace.json'\n"
)


def layout_wheel(path: Path, wiring: str) -> Path:
    """A tiny fictional package with the two names the probe asks for."""
    content = {
        "app/__init__.py": b"",
        "app/wiring.py": wiring.encode(),
        "app/services/__init__.py": b"",
        "app/services/demo.py": DEMO.encode(),
        "app/demo_data/workspace.json": b"{}",
        "app/prompts/summarize_job/prompt.md": b"version: 1\n",
    }
    wheel(path, content).close()
    return path


def test_the_installed_layout_probe_passes_for_packaged_prompts(tmp_path: Path) -> None:
    assert check_installed_layout(layout_wheel(tmp_path / "ok.whl", PACKAGED_WIRING)) == []


def test_the_installed_layout_probe_names_prompts_outside_the_package(tmp_path: Path) -> None:
    problems = check_installed_layout(layout_wheel(tmp_path / "bad.whl", CHECKOUT_WIRING))
    assert len(problems) == 1 and "installed PROMPTS_DIR is not the packaged folder" in problems[0]
