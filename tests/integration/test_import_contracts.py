"""Import-linter contracts and their self-test (M0.2, AC-M0.6, spec 2.3).

``uv run lint-imports`` must pass on the repository, and each contract must
actually catch a violation: a deliberately violating module (source text in
``tests/fixtures/import_violations/<case>.py.txt``) is dropped into a temporary
copy of ``app/`` and lint-imports must report the expected contract as broken.
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "import_violations"
TIMEOUT_SECONDS = 120

LAYERING = "Layering (spec 2.3)"
DOMAIN_PURE = "Domain is pure (spec 2.3 rule 1)"
SERVICES = "Services use module interfaces only and no web frameworks (spec 2.3 rule 4)"
API = "API does not reach modules or SQLite (spec 2.3 rule 5)"
TASKS = "Task handlers stay thin (spec 2.3 rule 6)"
CLAUDE_SDK = "claude_agent_sdk only in AI providers (spec 2.3 rule 3)"
KEYRING = "keyring only in secrets providers (spec 2.3 rule 3)"
HTTPX = "httpx only in the fetch module (spec 2.3 rule 3)"
PROVIDER_SELECTION = "Providers are chosen only in wiring (spec 2.3 rule 7)"

EXPECTED_CONTRACTS = frozenset(
    {LAYERING, DOMAIN_PURE, SERVICES, API, TASKS, CLAUDE_SDK, KEYRING, HTTPX, PROVIDER_SELECTION}
)


@dataclass(frozen=True)
class ViolationCase:
    destination: str  # path relative to the copied tree, e.g. "app/domain/_violation.py"
    contract: str

    @property
    def module(self) -> str:
        return self.destination.removesuffix(".py").replace("/", ".")


VIOLATION_CASES: dict[str, ViolationCase] = {
    "layers_modules_import_services": ViolationCase("app/modules/storage/_violation.py", LAYERING),
    "layers_domain_imports_composition_root": ViolationCase("app/domain/_violation.py", LAYERING),
    "domain_imports_sqlite3": ViolationCase("app/domain/_violation.py", DOMAIN_PURE),
    "domain_imports_fastapi": ViolationCase("app/domain/_violation.py", DOMAIN_PURE),
    "services_import_provider": ViolationCase("app/services/_violation.py", SERVICES),
    "services_import_jinja2": ViolationCase("app/services/_violation.py", SERVICES),
    "api_imports_module": ViolationCase("app/api/_violation.py", API),
    "api_imports_sqlite3": ViolationCase("app/api/_violation.py", API),
    "tasks_import_module": ViolationCase("app/tasks/_violation.py", TASKS),
    "claude_sdk_outside_ai_providers": ViolationCase(
        "app/modules/storage/_violation.py", CLAUDE_SDK
    ),
    "keyring_outside_secrets_providers": ViolationCase("app/services/_violation.py", KEYRING),
    "keyring_in_secrets_module_outside_providers": ViolationCase(
        "app/modules/secrets/_violation.py", KEYRING
    ),
    "claude_sdk_in_ai_module_outside_providers": ViolationCase(
        "app/modules/ai/_violation.py", CLAUDE_SDK
    ),
    "httpx_outside_fetch": ViolationCase("app/modules/ai/_violation.py", HTTPX),
    "onboarding_imports_provider": ViolationCase(
        "app/onboarding/_violation.py", PROVIDER_SELECTION
    ),
}


def _lint_imports_executable() -> str:
    found = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert found is not None, "lint-imports is not installed next to the test interpreter"
    return found


def _run_lint_imports(
    cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # Fixed argv to the venv's own lint-imports; no shell and no untrusted input.
    return subprocess.run(  # noqa: S603
        [_lint_imports_executable(), "--no-cache"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def _contracts_with_status(output: str, status: str) -> set[str]:
    # lint-imports may append a note, e.g. "KEPT (1 ignored import)".
    pattern = re.compile(rf"^(?P<name>.+?)\s+{status}(?:\s+\(.*\))?\s*$", re.MULTILINE)
    return {match.group("name").strip() for match in pattern.finditer(output)}


def test_ac_m0_6_lint_imports_passes_on_repo() -> None:
    result = _run_lint_imports(REPO_ROOT)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert _contracts_with_status(output, "KEPT") == EXPECTED_CONTRACTS, output
    assert re.search(r"\b0 broken\b", output), output


def _lint_copy_with(tmp_path: Path, files: dict[str, str]) -> tuple[int, str]:
    """Run lint-imports on a temporary copy of ``app/`` with extra modules written into it."""
    shutil.copytree(
        REPO_ROOT / "app",
        tmp_path / "app",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for destination, source in files.items():
        target = tmp_path / destination
        assert target.parent.is_dir(), f"destination package missing: {destination}"
        target.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(tmp_path) if not existing else os.pathsep.join([str(tmp_path), existing])
    )
    result = _run_lint_imports(tmp_path, env=env)
    return result.returncode, result.stdout + result.stderr


@pytest.mark.parametrize("case_id", sorted(VIOLATION_CASES))
def test_ac_m0_6_violation_detected(case_id: str, tmp_path: Path) -> None:
    case = VIOLATION_CASES[case_id]
    source = (FIXTURES_DIR / f"{case_id}.py.txt").read_text(encoding="utf-8")

    returncode, output = _lint_copy_with(tmp_path, {case.destination: source})

    assert returncode != 0, output
    assert case.contract in _contracts_with_status(output, "BROKEN"), output
    assert case.module in output, output


def test_vendor_sdks_allowed_inside_their_provider_packages(tmp_path: Path) -> None:
    returncode, output = _lint_copy_with(
        tmp_path,
        {
            "app/modules/ai/providers/claude_sdk.py": "import claude_agent_sdk\n",
            "app/modules/secrets/providers/keyring_store.py": "import keyring\n",
            "app/modules/fetch/guard.py": "import httpx\n",
        },
    )

    assert returncode == 0, output
    assert _contracts_with_status(output, "KEPT") == EXPECTED_CONTRACTS, output


# The import chains the architecture expects once real code exists (spec 2.3, 2.5).
LEGAL_CHAIN = {
    "app/modules/storage/db.py": "import sqlite3\n",
    "app/services/jobs.py": "import app.modules.storage.db\n",
    "app/api/routes.py": "import fastapi\n\nimport app.services.jobs\n",
    "app/tasks/runner.py": "import app.services.jobs\n",
    "app/onboarding/redirect.py": "import starlette\n\nimport app.services.jobs\n",
    "app/domain/normalize.py": "from http import HTTPStatus\nfrom urllib.parse import urlsplit\n",
    "app/wiring.py": "import app.modules.secrets.providers\nimport app.services.jobs\n",
    "app/main.py": "import app.api.routes\nimport app.tasks.runner\nimport app.wiring\n",
}


def test_legal_layered_import_chain_keeps_every_contract(tmp_path: Path) -> None:
    returncode, output = _lint_copy_with(tmp_path, LEGAL_CHAIN)

    assert returncode == 0, output
    assert _contracts_with_status(output, "KEPT") == EXPECTED_CONTRACTS, output


def _forbidden_entries() -> list[tuple[str, str, str]]:
    """(contract name, source package, forbidden module) for every forbidden-contract entry."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entries = []
    for contract in config["tool"]["importlinter"]["contracts"]:
        if contract["type"] != "forbidden":
            continue
        source = contract["source_modules"][0]
        if source == "app":
            source = "app.services"
        for forbidden in contract["forbidden_modules"]:
            entries.append((contract["name"], source, forbidden.replace("*", "ai")))
    return entries


@pytest.mark.parametrize(
    ("contract", "source", "forbidden"),
    _forbidden_entries(),
    ids=lambda value: value.split(" (spec")[0].replace(" ", "_"),
)
def test_every_forbidden_entry_is_enforced(
    contract: str, source: str, forbidden: str, tmp_path: Path
) -> None:
    destination = source.replace(".", "/") + "/_violation.py"

    returncode, output = _lint_copy_with(tmp_path, {destination: f"import {forbidden}\n"})

    assert returncode != 0, output
    assert contract in _contracts_with_status(output, "BROKEN"), output
