"""The domain does no network I/O (spec 2.3 rule 1).

import-linter cannot forbid submodules of external packages, so the network parts of
``http`` and ``urllib`` are checked here, while pure parts such as ``urllib.parse`` stay
allowed. The other forbidden imports are import-linter contracts (ADR-033).
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NETWORK_MODULES = ("http.client", "http.cookiejar", "http.server", "urllib.request")


def network_imports(package_dir: Path) -> list[str]:
    """Return "<file>:<line> <module>" for every network-module import under package_dir."""
    found = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            hits = [
                n for n in names if any(n == m or n.startswith(m + ".") for m in NETWORK_MODULES)
            ]
            if hits:
                found.append(f"{path.relative_to(package_dir)}:{node.lineno} {hits[0]}")
    return found


def test_domain_has_no_network_imports() -> None:
    assert network_imports(REPO_ROOT / "app" / "domain") == []


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request",
        "from urllib.request import urlopen",
        "from urllib import request",
        "import http.client as client",
        "from http.server import HTTPServer",
        "from http import cookiejar",
    ],
)
def test_network_import_is_detected(source: str, tmp_path: Path) -> None:
    (tmp_path / "violation.py").write_text(source + "\n", encoding="utf-8")

    assert len(network_imports(tmp_path)) == 1


@pytest.mark.parametrize(
    "source",
    ["from urllib.parse import urlsplit", "from http import HTTPStatus", "import urllib.parse"],
)
def test_pure_stdlib_import_is_allowed(source: str, tmp_path: Path) -> None:
    (tmp_path / "pure.py").write_text(source + "\n", encoding="utf-8")

    assert network_imports(tmp_path) == []
