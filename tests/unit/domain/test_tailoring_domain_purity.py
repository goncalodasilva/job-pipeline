"""The M6.4 domain modules stay pure (spec 2.3 rule 1; M6.4): no I/O, clock, randomness or
framework imports in ``documents``, ``master_cv`` and ``cv_templates``.

``tests/unit/test_domain_purity.py`` and the import-linter contract "Domain is pure" already
cover every file in ``app/domain``; this adds a check that the three modules exist, so a
missing module fails here by name, and a stricter list for them (no ``pathlib``, ``os``,
``datetime`` or ``uuid``: ids and times come in as arguments).
"""

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[3] / "app" / "domain"
MODULES = ("documents", "master_cv", "cv_templates")
FORBIDDEN = (
    "claude_agent_sdk",
    "fastapi",
    "httpx",
    "jinja2",
    "keyring",
    "os",
    "pathlib",
    "platformdirs",
    "random",
    "shutil",
    "socket",
    "sqlite3",
    "starlette",
    "subprocess",
    "uuid",
    "yaml",
    "app.modules",
    "app.services",
    "app.api",
    "app.render",
    "app.tasks",
    "app.onboarding",
)


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", MODULES)
def test_tailoring_domain_module_exists_and_imports_nothing_impure(module: str) -> None:
    path = DOMAIN / f"{module}.py"

    assert path.is_file(), f"app/domain/{module}.py doesn't exist yet"
    bad = sorted(
        name
        for name in imported_names(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    )
    assert bad == []


@pytest.mark.parametrize("module", MODULES)
def test_tailoring_domain_module_does_not_read_the_wall_clock_or_make_ids(module: str) -> None:
    path = DOMAIN / f"{module}.py"

    assert path.is_file(), f"app/domain/{module}.py doesn't exist yet"
    text = path.read_text(encoding="utf-8")
    for call in ("datetime.now(", "date.today(", "uuid4(", "time.time("):
        assert call not in text
