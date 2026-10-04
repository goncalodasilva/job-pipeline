"""The vendored axe-core used by the e2e accessibility helper (M0.4, ADR-022)."""

import hashlib
import re
import subprocess
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parents[1] / "e2e" / "vendor" / "axe-core"
HASH_LINE = re.compile(r"SHA-256 of `axe\.min\.js`:\s*([0-9a-f]{64})")


def test_vendored_axe_matches_recorded_hash() -> None:
    readme = (VENDOR_DIR / "README.md").read_text(encoding="utf-8")
    match = HASH_LINE.search(readme)
    assert match is not None, "README.md must record the SHA-256 of axe.min.js"

    actual = hashlib.sha256((VENDOR_DIR / "axe.min.js").read_bytes()).hexdigest()

    assert actual == match.group(1)


def test_vendored_axe_license_files_present() -> None:
    license_text = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "Mozilla Public License" in license_text
    assert (VENDOR_DIR / "LICENSE-3RD-PARTY.txt").is_file()

    with (VENDOR_DIR / "axe.min.js").open(encoding="utf-8") as handle:
        header = handle.read(200)
    assert "axe v4.13.0" in header


def test_vendored_files_are_never_converted_by_git() -> None:
    # Windows checkouts convert line endings unless the file is marked -text, which would
    # change the bytes and the hash. Fixed argv, no shell, no untrusted input.
    result = subprocess.run(
        ["git", "check-attr", "text", "--", "tests/e2e/vendor/axe-core/axe.min.js"],  # noqa: S607
        cwd=VENDOR_DIR.parents[3],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().endswith("text: unset"), result.stdout
