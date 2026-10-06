"""Root test configuration shared by every test layer.

Enables the ``pytester`` plugin so tests can run inner pytest sessions
against the project's real pytest configuration, and gates live tests
behind ``JOBPIPELINE_LIVE_TESTS=1`` (spec 7.2).

Every test also runs with an in-memory keyring backend, set directly and pinned through
``PYTHON_KEYRING_BACKEND``, so no test can reach the real system keychain (M1.2, ADR-044).
Outside test bodies (collection, hooks, session fixtures) a guard backend that fails on any
use is pinned instead, so keyring never falls back to discovery or the user's keyringrc.cfg.

Every test also starts without the developer's Claude or Anthropic credential variables and
with ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1`` (the setup harness saves the fake provider), so
results don't depend on the shell the tests run in (M1.15). Tests about the flag being off
pass an explicit environment or remove it with ``monkeypatch``.

For the whole session the Claude Agent SDK's log records are reduced as they are created
(``install_sdk_record_reduction``, as the CLI's logging does), so ``caplog``, pytest's own log
capture and live tests never hold the SDK's error texts, even where ``configure_logging`` never
runs (M1.23).
"""

import os
from collections.abc import Iterator

import keyring
import keyring.core
import pytest

from app.logging_config import install_sdk_record_reduction
from tests.fakes.keyring_backends import (
    GUARD_BACKEND_NAME,
    MEMORY_BACKEND_NAME,
    GuardKeyring,
    MemoryKeyring,
)

pytest_plugins = ["pytester"]

# Set at import, before any fixture or hook can resolve a backend.
os.environ["PYTHON_KEYRING_BACKEND"] = GUARD_BACKEND_NAME
keyring.set_keyring(GuardKeyring())

LIVE_ENV = "JOBPIPELINE_LIVE_TESTS"
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
ALLOW_FAKES_VARIABLE = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 to run it")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True, scope="session")
def sdk_record_reduction() -> Iterator[None]:
    """Reduce the SDK's log records at creation for the whole session."""
    restore = install_sdk_record_reduction()
    yield
    restore()


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """Pin a fresh in-memory keyring backend for this test; the previous one comes back after."""
    backend = MemoryKeyring()
    # Recorded through monkeypatch so the backend in place before the test is restored.
    monkeypatch.setattr(keyring.core, "_keyring_backend", None, raising=False)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", MEMORY_BACKEND_NAME)
    keyring.set_keyring(backend)
    yield backend


@pytest.fixture(autouse=True)
def provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credential variables from the developer's shell; fake providers allowed."""
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ALLOW_FAKES_VARIABLE, "1")
