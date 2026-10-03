"""``python -m app`` runs the same command as ``jobpipeline`` (M0.16; ADR-040).

The smoke test starts the app this way, because on macOS a venv's hidden ``.pth`` files can
stop the console script from importing ``app``. Only ``--version`` runs here: it prints and
exits before any folder, socket or browser is touched.
"""

import importlib.metadata
import subprocess
import sys
from pathlib import Path

from tests.running_app import isolated_env

TIMEOUT_SECONDS = 30


def test_python_m_app_version_prints_package_version_and_exits_0(tmp_path: Path) -> None:
    expected = f"jobpipeline {importlib.metadata.version('jobpipeline')}"

    completed = subprocess.run(
        [sys.executable, "-m", "app", "--version"],
        cwd=tmp_path,
        env=isolated_env(tmp_path / "data", tmp_path / "config"),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected
