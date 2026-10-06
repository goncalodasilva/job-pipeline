"""Gating and credential helpers for the live Claude tests (M1.25; AC-M1.9, ADR-044 item 10).

A live test runs only when both gates are open: ``JOBPIPELINE_LIVE_TESTS`` is exactly ``1``
and the run selects the ``live`` marker explicitly (``-m live``). The root conftest already
skips every ``live`` test without the variable; ``tests/live/conftest.py`` adds the marker
gate, so a plain ``pytest`` with the variable set still runs no live test.

Credentials come only from ``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` and
``JOBPIPELINE_LIVE_ANTHROPIC_API_KEY``: never from the system keychain and never from the
developer's own ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` (the root conftest removes
those for every test anyway). ``live_wiring`` builds the app's wiring the way the CLI does,
with the credential saved under the app's keychain item in an in-memory keyring backend
(as the setup guide would save it) and an empty environment, so the real keychain is never
touched. ``LiveCredential`` never shows its value in a repr, a message or a traceback.

A failing live test must not publish the credential either: ``redacted_pytest_logs`` puts the
app's ``RedactionFilter`` (holding the live credentials) on pytest's own log handlers, so the
"Captured log" sections, ``caplog`` and live logging show it redacted, and
``without_error_details`` turns an unexpected exception from a call into a bare
``AssertionError`` naming only its type, with no chained traceback.
"""

import logging
import re
from collections.abc import Awaitable, Iterator, Mapping
from contextlib import contextmanager
from typing import Final, TypeVar

import pytest

from app.domain.redaction import Redactor
from app.domain.settings import ClaudeAuthMode
from app.logging_config import RedactionFilter
from app.modules.ai.providers.claude_sdk import SECRET_KEY_BY_MODE
from app.modules.secrets.providers.keyring_store import SERVICE_NAME
from app.wiring import Wiring, build_wiring
from tests.fakes.keyring_backends import MemoryKeyring

LIVE_ENV: Final = "JOBPIPELINE_LIVE_TESTS"
LIVE_MARKER: Final = "live"
TOKEN_VARIABLE: Final = "JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN"  # noqa: S105 - a variable name
API_KEY_VARIABLE: Final = "JOBPIPELINE_LIVE_ANTHROPIC_API_KEY"

# The keychain item (user name under the ``jobpipeline`` service) each sign-in mode reads.
KEYCHAIN_KEY_BY_MODE: Final[Mapping[ClaudeAuthMode, str]] = {
    mode: key for mode, key in SECRET_KEY_BY_MODE.items() if key is not None
}

_WORD: Final = re.compile(r"[\w.]+")
# pytest's logging plugin and the handlers it keeps for the whole session (pytest 8 and 9).
LOGGING_PLUGIN: Final = "logging-plugin"
REQUIRED_LOG_HANDLERS: Final = ("caplog_handler", "report_handler")
OPTIONAL_LOG_HANDLERS: Final = ("log_cli_handler", "log_file_handler")

T = TypeVar("T")


def live_selected(markexpr: str) -> bool:
    """Whether the run selects the ``live`` marker explicitly: the word ``live`` appears in
    the ``-m`` expression and nothing is negated (``not live``, ``not e2e and not live``)."""
    words = _WORD.findall(markexpr)
    return LIVE_MARKER in words and "not" not in words


def gate_reason(environ: Mapping[str, str], markexpr: str) -> str | None:
    """Why a live test may not run now, or ``None`` when both gates are open."""
    if environ.get(LIVE_ENV) != "1":
        return f"live test: set {LIVE_ENV}=1 to run it"
    if not live_selected(markexpr):
        return f"live test: run it explicitly with -m live (and {LIVE_ENV}=1)"
    return None


class LiveCredential:
    """A real credential read from a ``JOBPIPELINE_LIVE_*`` variable; never shown."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "LiveCredential(<hidden>)"

    __str__ = __repr__


def read_credential(environ: Mapping[str, str], variable: str) -> LiveCredential | None:
    """The stripped value of ``variable`` (one of the ``JOBPIPELINE_LIVE_*`` names), or
    ``None`` when it is absent or blank. No other variable is consulted."""
    if variable not in (TOKEN_VARIABLE, API_KEY_VARIABLE):
        raise ValueError("live credentials come only from the JOBPIPELINE_LIVE_* variables")
    value = environ.get(variable, "").strip()
    return LiveCredential(value) if value else None


def live_wiring(
    mode: ClaudeAuthMode, credential: LiveCredential, redactor: Redactor
) -> tuple[Wiring, MemoryKeyring]:
    """The app's wiring (not demo) with ``credential`` saved under the app's keychain item for
    ``mode`` in an in-memory backend, and an empty environment. Returns the backend too."""
    backend = MemoryKeyring()
    backend.set_password(SERVICE_NAME, KEYCHAIN_KEY_BY_MODE[mode], credential.value)
    wiring = build_wiring(environ={}, demo=False, redactor=redactor, keyring_backend=backend)
    return wiring, backend


def live_redactor(environ: Mapping[str, str]) -> Redactor:
    """A redactor holding every live credential set in ``environ`` (none: shapes only)."""
    redactor = Redactor()
    for variable in (TOKEN_VARIABLE, API_KEY_VARIABLE):
        credential = read_credential(environ, variable)
        if credential is not None:
            redactor.register(credential.value)
    return redactor


def pytest_log_handlers(config: pytest.Config) -> list[logging.Handler]:
    """The handlers pytest's logging plugin writes reports, ``caplog``, live logging and the
    log file through. Fails closed if the plugin is active but its handlers moved."""
    plugin = config.pluginmanager.get_plugin(LOGGING_PLUGIN)
    if plugin is None:  # ``-p no:logging``: pytest captures no log records at all
        return []
    handlers: list[logging.Handler] = []
    for name in (*REQUIRED_LOG_HANDLERS, *OPTIONAL_LOG_HANDLERS):
        handler = getattr(plugin, name, None)
        if isinstance(handler, logging.Handler):
            handlers.append(handler)
        elif name in REQUIRED_LOG_HANDLERS:
            raise RuntimeError(f"pytest's logging plugin has no {name}; live logs not guarded")
    return handlers


@contextmanager
def redacted_pytest_logs(config: pytest.Config, redactor: Redactor) -> Iterator[None]:
    """While open, every record pytest's own handlers see is redacted first."""
    log_filter = RedactionFilter(redactor)
    handlers = pytest_log_handlers(config)
    for handler in handlers:
        handler.addFilter(log_filter)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)


async def without_error_details(call: Awaitable[T]) -> T:
    """Await ``call``; an exception other than a test assertion becomes an
    ``AssertionError`` naming only its type, raised outside the handler so nothing is chained
    (its message, arguments and frames may hold the credential)."""
    raised: str | None = None
    try:
        return await call
    except AssertionError:
        raise
    except Exception as error:
        raised = type(error).__name__
    raise AssertionError(f"the call raised {raised} (details hidden: they may hold a credential)")
