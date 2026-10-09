"""Every test under ``tests/integration/failures/`` is an ``integration`` test (M2.3, spec 4.1).

Failure-injection tests use real SQLite, routes or the running app, so they carry the
``integration`` marker even when a module forgets ``pytestmark``.
"""

from pathlib import Path

import pytest

FAILURES_DIR = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)  # before "-m" selection reads the markers
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Conftest hooks see every item in the session, so only this folder's items are marked.
    for item in items:
        if item.path.resolve().is_relative_to(FAILURES_DIR):
            item.add_marker(pytest.mark.integration)
