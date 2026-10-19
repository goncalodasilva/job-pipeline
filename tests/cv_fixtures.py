"""The fictional CV fixtures and their manifest (M3.1, ADR-089 item 2).

``tests/fixtures/cv/`` is written by ``scripts/make_cv_fixtures.py``. ``manifest.json`` lists
every file with what content sniffing must answer (``kind``, or null when the type is refused),
what reading it must give (``outcome``) and, for readable files, ``marker_groups``: every marker
must be in the extracted text, and the markers of one group must come in the listed order.
"""

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CV_DIR = REPO_ROOT / "tests" / "fixtures" / "cv"
MANIFEST_NAME = "manifest.json"
GENERATOR = REPO_ROOT / "scripts" / "make_cv_fixtures.py"

OUTCOMES = frozenset({"text", "unreadable", "encrypted", "no_text", "unsupported"})


def load_manifest() -> dict[str, dict[str, Any]]:
    """The ``files`` table of the manifest: file name to its entry."""
    listing = json.loads((CV_DIR / MANIFEST_NAME).read_text(encoding="utf-8"))
    files: dict[str, dict[str, Any]] = listing["files"]
    return files


def fixture_path(name: str) -> Path:
    path = CV_DIR / name
    assert path.is_file(), f"missing CV fixture: {name}"
    return path


def marker_groups(name: str) -> list[list[str]]:
    groups: list[list[str]] = load_manifest()[name]["marker_groups"]
    return groups


def one_space(text: str) -> str:
    """Whitespace runs as one space, so line breaks chosen by a library don't matter."""
    return re.sub(r"\s+", " ", text).strip()


def assert_markers_in_order(text: str, name: str) -> None:
    """Every marker of ``name`` is in ``text``; within a group they come in the listed order."""
    flat = one_space(text)
    groups = marker_groups(name)
    assert groups, f"{name} has no markers in the manifest"
    for group in groups:
        position = 0
        for marker in group:
            wanted = one_space(marker)
            found = flat.find(wanted, position)
            assert found >= 0, (
                f"{name}: marker {wanted!r} is missing or out of order in the extracted text"
            )
            position = found + len(wanted)
