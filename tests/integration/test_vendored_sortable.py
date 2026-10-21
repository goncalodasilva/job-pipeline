"""Pinned SortableJS bytes and license, with no CDN or page load."""

import hashlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
VENDOR = Path(__file__).resolve().parents[2] / "app" / "static" / "vendor"


def test_vendored_sortable_matches_pinned_version_and_hash() -> None:
    readme = (VENDOR / "README.md").read_text()
    section = readme.split("## SortableJS", 1)[1]
    version = re.search(r"Version: (1\.15\.\d+)", section)
    checksum = re.search(r"SHA-256 of `Sortable.min.js`: ([0-9a-f]{64})", section)
    assert version and checksum
    content = (VENDOR / "Sortable.min.js").read_bytes()
    assert version.group(1).encode() in content[:300]
    assert hashlib.sha256(content).hexdigest() == checksum.group(1)
    assert re.search(r"npm integrity of the tarball: sha512-[A-Za-z0-9+/=]+", section)
    license_text = (VENDOR / "Sortable.LICENSE").read_text()
    assert "MIT License" in license_text and "Permission is hereby granted" in license_text
    templates = Path(__file__).resolve().parents[2] / "app" / "templates"
    # SortableJS is loaded by the board page only (ADR-102 item 2).
    loaders = {
        p.relative_to(templates).as_posix()
        for p in templates.rglob("*.html")
        if "Sortable.min.js" in p.read_text()
    }
    assert loaders == {"pages/board.html"}
