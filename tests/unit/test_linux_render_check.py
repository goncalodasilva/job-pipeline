"""The Linux renderer check, README notes and CI stance for PDF export (M6.2; ADR-107, ADR-108).

``scripts/linux_render_check.sh`` runs the renderer suite in a Docker Linux image. It is a
developer tool: not part of ``verify.sh``, CI, pre-commit or the package. These tests only read
files; they never run Docker.
"""

import os
import re
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "linux_render_check.sh"
# The renderer suite and nothing else.
RENDERER_SUITE = {"tests/contract/test_document_renderer_contract.py", "tests/unit/render"}


def _script_text() -> str:
    assert SCRIPT.is_file(), "scripts/linux_render_check.sh doesn't exist"
    return SCRIPT.read_text(encoding="utf-8")


def test_linux_render_check_script_exists_and_is_executable() -> None:
    text = _script_text()

    assert os.access(SCRIPT, os.X_OK) and SCRIPT.stat().st_mode & stat.S_IXUSR
    assert text.startswith(("#!/usr/bin/env bash", "#!/bin/bash"))
    assert "set -euo pipefail" in text, "fails fast"


def test_linux_render_check_runs_the_renderer_suite_in_docker_only() -> None:
    text = _script_text()

    assert re.search(r"(?m)^\s*docker run\b|\bdocker\s+run\b", text)
    assert "--rm" in text, "the container is removed afterwards"
    pytest_lines = [line for line in text.splitlines() if re.search(r"\bpytest\b", line)]
    assert pytest_lines, "the script runs pytest"
    named = set(re.findall(r"tests/[\w./-]+", " ".join(pytest_lines)))
    assert named == RENDERER_SUITE, f"only the renderer suite, found {sorted(named)}"
    joined = " ".join(pytest_lines)
    assert "-m e2e" not in joined and "playwright" not in joined.lower()
    for forbidden in ("git push", "--privileged", "--network host", "~/.ssh", "$HOME"):
        assert forbidden not in text, forbidden
    assert "PASS" in text, "prints a PASS line for the Evidence section"


def test_linux_render_check_container_is_locked_down() -> None:
    text = _script_text()

    assert "--cap-drop ALL" in text, "no Linux capability is kept"
    assert "--security-opt no-new-privileges" in text
    assert re.search(r"--volume\s+\"\$ROOT:/work:ro\"", text), "the repository is read-only"
    assert not re.search(r"--volume\s+(?!\"\$ROOT:/work:ro\")", text), "nothing else is mounted"
    assert "docker.sock" not in text and "--user root" not in text


def test_linux_render_check_pins_uv_and_keeps_writes_out_of_the_read_only_mount() -> None:
    text = _script_text()

    pinned = re.search(r'(?m)^UV_VERSION="(\d+\.\d+\.\d+)"', text)
    assert pinned, "UV_VERSION is a fixed x.y.z"
    assert re.search(r'pip install\b[^\n]*"uv==\$UV_VERSION"', text), "pip installs that version"
    assert not re.search(r"pip install\b[^\n]*\buv\b(?!==)", text.replace('"uv==$UV_VERSION"', ""))
    assert "--env UV_VERSION=" in text, "the container gets the pinned version"
    assert "-p no:cacheprovider" in text, "pytest writes no cache into the mount"
    assert re.search(r"--env HYPOTHESIS_STORAGE_DIRECTORY=/tmp/\S+", text)


def test_linux_render_check_fails_closed() -> None:
    text = _script_text()

    assert "set -euo pipefail" in text
    assert re.search(r"if docker run\b", text) and "|| true" not in text
    assert 'fail "the renderer tests failed' in text
    assert text.rstrip().endswith("fi"), "PASS or FAIL is the last thing printed"


def test_linux_render_check_is_dev_only() -> None:
    name = "linux_render_check"
    others = [
        REPO_ROOT / "scripts" / "verify.sh",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / ".pre-commit-config.yaml",
        *(REPO_ROOT / ".github").rglob("*.yml"),
    ]
    for path in others:
        assert path.is_file()
        assert name not in path.read_text(encoding="utf-8"), f"{path.name} refers to {name}"


def test_ci_needs_no_system_packages_for_pdf() -> None:
    """typst ships as one wheel with no system libraries, so CI installs none for PDF."""
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert not re.search(r"\b(apt-get|apt|brew)\s+install\b", text)
    assert "pango" not in text.lower() and "harfbuzz" not in text.lower()


def test_readme_has_pdf_install_notes_per_os() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "typst" in text, "the README names the PDF library"
    assert re.search(r"(?i)pdf export", text)
    assert "Word export still works" in text, "what happens when PDF export is unavailable"
    assert "macOS" in text and "Linux" in text
    assert re.search(r"(?i)no system librar", text), "no per-OS libraries to install"
