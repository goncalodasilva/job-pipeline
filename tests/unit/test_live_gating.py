"""Gating and credentials of the live Claude tests (M1.25; AC-M1.9, ADR-044 item 10).

The live tests in ``tests/live/`` run only with ``-m live`` and ``JOBPIPELINE_LIVE_TESTS=1``,
take their credentials only from ``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` and
``JOBPIPELINE_LIVE_ANTHROPIC_API_KEY``, and never use the system keychain or the developer's
own ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``. Inner pytest sessions run the real
live test module with the project's real configuration; no credential is ever present, so
nothing reaches the network. Every value here is fictional.
"""

import logging
from pathlib import Path

import keyring
import pytest

from app.domain.redaction import REDACTED, Redactor
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.keyring_store import SERVICE_NAME
from tests.fakes.keyring_backends import CountingKeyring
from tests.leak_scan import TOKEN_PREFIX, Evidence, RawLogRecords, fictional_secret
from tests.live.support import (
    API_KEY_VARIABLE,
    KEYCHAIN_KEY_BY_MODE,
    LIVE_ENV,
    TOKEN_VARIABLE,
    LiveCredential,
    gate_reason,
    live_redactor,
    live_selected,
    live_wiring,
    pytest_log_handlers,
    read_credential,
    redacted_pytest_logs,
    without_error_details,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_FILES = ("conftest.py", "support.py", "test_claude_live.py")
TOKEN_TEST = "test_ac_m1_9_test_connection_with_subscription_token"  # noqa: S105 - a test name
API_KEY_TEST = "test_ac_m1_9_test_connection_with_api_key"
# Fictional values standing for the developer's own shell credentials.
SHELL_TOKEN = "fictional-shell-token-m125-" + "t" * 24
SHELL_API_KEY = "fictional-shell-api-key-m125-" + "k" * 24


def _use_real_live_suite(pytester: pytest.Pytester) -> None:
    """The real pyproject.toml, tests/conftest.py and tests/live/ files in the inner session."""
    pytester.makepyprojecttoml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tests_dir = pytester.mkdir("tests")
    root_conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (tests_dir / "conftest.py").write_text(root_conftest, encoding="utf-8")
    live_dir = tests_dir / "live"
    live_dir.mkdir()
    for name in LIVE_FILES:
        source = (REPO_ROOT / "tests" / "live" / name).read_text(encoding="utf-8")
        (live_dir / name).write_text(source, encoding="utf-8")


def _no_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a real credential in these inner runs, whatever the developer's shell holds."""
    monkeypatch.delenv(TOKEN_VARIABLE, raising=False)
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)


def _reports(recorder: pytest.HookRecorder) -> dict[str, pytest.TestReport]:
    """The deciding report per test name (call if it ran, else setup)."""
    found: dict[str, pytest.TestReport] = {}
    for report in recorder.getreports("pytest_runtest_logreport"):
        name = report.nodeid.rsplit("::", 1)[-1]
        if report.when == "call" or (report.when == "setup" and name not in found):
            found[name] = report
    return found


def _skip_reason(report: pytest.TestReport) -> str:
    assert report.skipped, report.nodeid
    longrepr = report.longrepr
    assert isinstance(longrepr, tuple)
    return str(longrepr[2])


def test_live_claude_tests_need_credentials_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_live_credentials(monkeypatch)
    _use_real_live_suite(pytester)

    # Without JOBPIPELINE_LIVE_TESTS=1, even "-m live" runs nothing.
    monkeypatch.delenv(LIVE_ENV, raising=False)
    recorder = pytester.inline_run("-m", "live")
    reports = _reports(recorder)
    assert set(reports) == {TOKEN_TEST, API_KEY_TEST}
    for report in reports.values():
        assert LIVE_ENV in _skip_reason(report)

    # With the variable but without "-m live" (a plain run), nothing runs either.
    monkeypatch.setenv(LIVE_ENV, "1")
    recorder = pytester.inline_run()
    reports = _reports(recorder)
    assert set(reports) == {TOKEN_TEST, API_KEY_TEST}
    for report in reports.values():
        assert "-m live" in _skip_reason(report)

    # The default verification deselects them.
    recorder = pytester.inline_run("-m", "not e2e and not smoke and not live")
    assert recorder.getreports("pytest_runtest_logreport") == []

    # Both gates open, no JOBPIPELINE_LIVE_* variable: the token test fails naming its
    # variable, the API-key test skips naming its variable (ADR-044 item 10). That the shell's
    # own CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY are never read is shown by
    # test_read_credential_only_from_its_variable and test_live_wiring_never_uses_...: the
    # root conftest removes those variables before any inner test runs, so setting them here
    # would prove nothing.
    recorder = pytester.inline_run("-m", "live")
    reports = _reports(recorder)
    token, api_key = reports[TOKEN_TEST], reports[API_KEY_TEST]
    assert token.failed, "the token test must not pass or skip without its variable"
    assert TOKEN_VARIABLE in token.longreprtext
    assert API_KEY_VARIABLE in _skip_reason(api_key)
    passed, _, _ = recorder.listoutcomes()
    assert passed == []


@pytest.mark.parametrize(
    ("markexpr", "selected"),
    [
        ("live", True),
        (" live ", True),
        ("live or slow", True),
        ("", False),
        ("not live", False),
        ("not e2e and not smoke and not live", False),
        ("e2e", False),
        ("lively", False),
    ],
)
def test_live_selected_only_by_an_explicit_live_marker(markexpr: str, selected: bool) -> None:
    assert live_selected(markexpr) is selected


@pytest.mark.parametrize(
    ("environ", "markexpr", "needle"),
    [
        ({}, "live", LIVE_ENV),
        ({LIVE_ENV: "true"}, "live", LIVE_ENV),
        ({LIVE_ENV: "1"}, "", "-m live"),
        ({LIVE_ENV: "1"}, "not live", "-m live"),
    ],
)
def test_gate_reason_names_what_is_missing(
    environ: dict[str, str], markexpr: str, needle: str
) -> None:
    reason = gate_reason(environ, markexpr)
    assert reason is not None
    assert needle in reason


def test_gate_open_with_env_and_marker() -> None:
    assert gate_reason({LIVE_ENV: "1"}, "live") is None


def test_read_credential_only_from_its_variable() -> None:
    value = "fictional-live-credential-m125-" + "v" * 24
    shell = {"CLAUDE_CODE_OAUTH_TOKEN": SHELL_TOKEN, "ANTHROPIC_API_KEY": SHELL_API_KEY}

    assert read_credential(shell, TOKEN_VARIABLE) is None
    assert read_credential(shell, API_KEY_VARIABLE) is None
    assert read_credential({TOKEN_VARIABLE: "  "}, TOKEN_VARIABLE) is None
    credential = read_credential({TOKEN_VARIABLE: f" {value}\n"}, TOKEN_VARIABLE)
    assert isinstance(credential, LiveCredential)
    assert credential.value == value
    assert value not in repr(credential)
    assert value not in str(credential)


@pytest.mark.parametrize("mode", ["subscription_token", "api_key"])
def test_live_wiring_never_uses_system_keychain_or_shell_credentials(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SHELL_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SHELL_API_KEY)
    system_keychain = CountingKeyring()  # stands in for whatever keyring would pick
    keyring.set_keyring(system_keychain)
    value = "fictional-live-credential-m125-" + "w" * 24

    wiring, backend = live_wiring(mode, LiveCredential(value), Redactor())

    key = KEYCHAIN_KEY_BY_MODE[mode]
    other = ANTHROPIC_API_KEY if key == CLAUDE_CODE_OAUTH_TOKEN else CLAUDE_CODE_OAUTH_TOKEN
    assert wiring.environment_keys() == frozenset()
    assert wiring.secrets.get(key) == value
    assert wiring.secrets.get(other) is None  # the shell's variable is never read
    assert backend.items == {(SERVICE_NAME, key): value}
    assert system_keychain.calls == 0
    assert wiring.redactor.redact(f"x{value}x") != f"x{value}x"


def test_keychain_item_names_per_mode() -> None:
    assert SERVICE_NAME == "jobpipeline"
    assert dict(KEYCHAIN_KEY_BY_MODE) == {
        "subscription_token": "claude_code_oauth_token",
        "api_key": "anthropic_api_key",
    }


# --- a failing live test never publishes the credential ------------------------------------

PLANTED_FILE = "planted-cli-file-m125"
PLANTED_TEST_FILE = "test_planted_m125.py"
# A stand-in live test (fictional value, no provider, no network) that logs the credential,
# raises an exception carrying it and leaves a file in its folder, then fails.
PLANTED_TESTS = f"""
import logging
import os

import pytest

from tests.live.support import without_error_details

pytestmark = pytest.mark.live


def test_planted_log(live_tmp, live_log_redactor, caplog):
    value = os.environ["{API_KEY_VARIABLE}"]
    (live_tmp / "{PLANTED_FILE}").write_text("cli state", encoding="utf-8")
    logging.getLogger("planted.m125").warning("credential %s in a record", value)
    assert value not in caplog.text
    assert live_log_redactor.redact(value) != value
    raise AssertionError("planted failure")


async def test_planted_exception():
    value = os.environ["{API_KEY_VARIABLE}"]

    async def call():
        raise RuntimeError("the CLI said " + value)

    await without_error_details(call())
"""


def test_failing_live_test_shows_no_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_live_credentials(monkeypatch)
    _use_real_live_suite(pytester)
    planted = pytester.path / "tests" / "live" / PLANTED_TEST_FILE
    planted.write_text(PLANTED_TESTS, encoding="utf-8")
    value = fictional_secret(TOKEN_PREFIX, "live log guard").value
    monkeypatch.setenv(LIVE_ENV, "1")
    monkeypatch.setenv(API_KEY_VARIABLE, value)

    recorder = pytester.inline_run("-m", "live", str(planted))

    reports = _reports(recorder)
    log_report, error_report = reports["test_planted_log"], reports["test_planted_exception"]
    assert log_report.failed and error_report.failed
    for report in recorder.getreports("pytest_runtest_logreport"):
        assert value not in report.longreprtext, report.nodeid
        for title, content in report.sections:
            assert value not in content, (report.nodeid, title)
    logs = dict(log_report.sections)["Captured log call"]
    assert "credential" in logs and REDACTED in logs
    assert "planted failure" in log_report.longreprtext
    # Only the exception's type: no message, no chained traceback.
    assert "the call raised RuntimeError" in error_report.longreprtext
    assert "RuntimeError: " not in error_report.longreprtext  # the original is never shown
    assert "During handling" not in error_report.longreprtext
    assert "direct cause" not in error_report.longreprtext
    # The live test's folder is gone, whatever pytest's retention keeps.
    assert list(pytester.path.rglob(PLANTED_FILE)) == []


def test_redacted_pytest_logs_filters_while_open(
    pytestconfig: pytest.Config, caplog: pytest.LogCaptureFixture
) -> None:
    value = fictional_secret(TOKEN_PREFIX, "live log filter").value[len(TOKEN_PREFIX) :]
    redactor = Redactor()
    redactor.register(value)
    handlers = pytest_log_handlers(pytestconfig)
    assert caplog.handler in handlers
    before = [list(handler.filters) for handler in handlers]
    logger = logging.getLogger("tests.live_gating.m125")

    with redacted_pytest_logs(pytestconfig, redactor):
        logger.warning("guarded %s", value)
    assert value not in caplog.text
    assert f"guarded {REDACTED}" in caplog.text
    after = [list(handler.filters) for handler in handlers]
    assert after == before, "the filter is removed on exit"


def test_live_redactor_holds_both_live_variables_only() -> None:
    token = "-".join(("fictional", "live", "token", "m125", "a" * 24))
    api_value = "-".join(("fictional", "live", "apikey", "m125", "b" * 24))
    environ = {
        TOKEN_VARIABLE: token,
        API_KEY_VARIABLE: api_value,
        "ANTHROPIC_API_KEY": SHELL_API_KEY,
    }

    redactor = live_redactor(environ)

    assert redactor.redact(f"{token} {api_value}") == f"{REDACTED} {REDACTED}"
    assert redactor.redact(SHELL_API_KEY) == SHELL_API_KEY


async def test_without_error_details_hides_message_and_chain() -> None:
    value = "fictional-live-credential-m125-" + "e" * 24

    async def failing() -> int:
        raise ValueError(f"bad credential {value}")

    with pytest.raises(AssertionError) as raised:
        await without_error_details(failing())
    assert "ValueError" in str(raised.value)
    assert value not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_without_error_details_passes_results_and_assertions() -> None:
    async def answer() -> int:
        return 42

    async def asserting() -> None:
        raise AssertionError("expected check")

    assert await without_error_details(answer()) == 42
    with pytest.raises(AssertionError, match="expected check"):
        await without_error_details(asserting())


def test_scan_helpers_never_show_what_they_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = fictional_secret(TOKEN_PREFIX, "live repr")
    evidence = Evidence()
    evidence.texts.append(("stdout", secret.value))
    evidence.blobs.append(("file", secret.value.encode("utf-8")))
    records = RawLogRecords()
    records.install(monkeypatch)
    logging.getLogger("tests.live_gating.repr").warning("held %s", secret.value)
    credential = LiveCredential(secret.value)

    assert records.entries, "the record was captured"
    for shown in (evidence, records, secret, credential):
        assert secret.value not in repr(shown), type(shown).__name__
        assert secret.value[len(TOKEN_PREFIX) :] not in str(shown), type(shown).__name__
    assert "1 texts" in repr(evidence) and "1 blobs" in repr(evidence)
