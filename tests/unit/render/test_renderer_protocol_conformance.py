"""``WordRenderer`` conforms to the ``DocumentRenderer`` protocol under mypy --strict (M6.1).

The contract tests reach the renderer through ``Any``, so they cannot see protocol drift. This
follows the pattern of ``test_protocol_conformance`` in the module-interface tests: a fixed
snippet is type-checked in a subprocess, and a deliberately wrong renderer must be rejected.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

CONFORMING = """
from datetime import UTC, datetime

from app.domain.clock import Clock
from app.render import DocumentRenderer
from app.render.providers.word import WordRenderer


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 24, tzinfo=UTC)


clock: Clock = FixedClock()
renderer: DocumentRenderer = WordRenderer(clock)
"""

WRONG_RENDER_SIGNATURE = """
from app.domain.rendering import RenderDocument
from app.render import DocumentRenderer


class Wrong:
    format = "pdf"
    media_type = "application/pdf"

    def render(self, document: RenderDocument, extra: int) -> bytes:
        return b""


renderer: DocumentRenderer = Wrong()
"""


def _mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    sample = tmp_path / "conformance.py"
    sample.write_text(source, encoding="utf-8")
    env = {**os.environ, "MYPYPATH": str(REPO_ROOT)}
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_word_renderer_is_a_document_renderer(tmp_path: Path) -> None:
    result = _mypy(tmp_path, CONFORMING)

    assert result.returncode == 0, result.stdout + result.stderr


def test_renderer_with_wrong_render_signature_is_rejected(tmp_path: Path) -> None:
    result = _mypy(tmp_path, WRONG_RENDER_SIGNATURE)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "DocumentRenderer" in result.stdout
    assert "Incompatible types in assignment" in result.stdout
