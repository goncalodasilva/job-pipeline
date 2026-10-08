"""Live tests run only with ``-m live`` and ``JOBPIPELINE_LIVE_TESTS=1`` (M1.25; spec 4.1).

The root conftest skips every ``live`` test without the variable; this fixture also skips
the tests in this folder unless the run selects the ``live`` marker explicitly, so a plain
``pytest`` with the variable left set in the shell never makes a real call.

For every ``live`` test, all phases (setup, call, teardown) run with the app's
redaction filter, holding the live credentials, on pytest's own log handlers, so a failure's
"Captured log" sections cannot show a credential. ``live_log_redactor`` is that redactor (give it
to the wiring), and ``live_tmp`` is a private folder removed right after the test, so no file
the CLI wrote is kept by pytest's temporary-folder retention.
"""

import os
import shutil
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest

from app.domain.redaction import Redactor
from tests.live.support import LIVE_MARKER, gate_reason, live_redactor, redacted_pytest_logs

_REDACTOR_KEY = pytest.StashKey[Redactor]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    # Every live test (the marker, not the path, which a copied suite would not share).
    if item.get_closest_marker(LIVE_MARKER) is None:
        return (yield)
    redactor = live_redactor(os.environ)
    item.stash[_REDACTOR_KEY] = redactor
    with redacted_pytest_logs(item.config, redactor):
        return (yield)


@pytest.fixture(autouse=True)
def live_gate(request: pytest.FixtureRequest) -> None:
    reason = gate_reason(os.environ, str(request.config.getoption("markexpr") or ""))
    if reason is not None:
        pytest.skip(reason)


@pytest.fixture
def live_log_redactor(request: pytest.FixtureRequest) -> Redactor:
    """The redactor guarding this test's pytest log output (holds the live credentials)."""
    return request.node.stash[_REDACTOR_KEY]


@pytest.fixture
def live_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private folder for one live test, always removed afterwards."""
    folder = tmp_path_factory.mktemp("live", numbered=True)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)
