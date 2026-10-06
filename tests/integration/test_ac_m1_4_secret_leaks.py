"""AC-M1.4: no response, page, log or database row ever contains a saved secret (M1.23).

An automated leak scan over the setup guide's provider step and Settings > Providers. Each test
drives the real app in-process (``create_app`` with a ``TestClient``, real SQLite in
``tmp_path``) with logging set up the way the CLI does it (``configure_logging`` into
``<data>/logs`` with the wiring's redactor), then scans every surface for each secret (tools in
``tests/leak_scan.py``):

- every response of the run: body and every header, including redirects, 422 refusals, htmx
  fragments and their ``HX-Trigger`` announcements, and every page GET (each page route the app
  registers, every guide step, plain and htmx, plus ``/health`` and a 404) after saving and again
  after a restart;
- log records **before** redaction: ``logging.Logger.handle`` is wrapped so each record is
  captured before any handler filter rewrites it (raw ``getMessage()``, ``msg``, ``args``,
  extras, ``exc_info`` rendered with its causes, ``exc_text`` and ``stack_info``). This includes
  the Claude Agent SDK's own records: the SDK formats exception text into its messages
  (``"Fatal error in message reader: {e}"``), and the app reduces them to level, logger and
  exception type as they are created (``install_sdk_record_reduction``, spec 2.10), so no
  handler, not even one the app didn't install, sees that text;
- the console output (the app's stderr handler) and the JSON log files in ``<data>/logs``;
- every row of every table in ``app.db`` (``sqlite_master``);
- the raw bytes (UTF-8 and UTF-16) of every file under the data folder, the config folder and
  the whole test folder (``app.db``, ``-wal``, ``-shm``, backups, ``config.toml``, logs, the AI
  temp folder), while the app runs and after it stops.

Each check looks for the whole secret and for every 8-character window of it. Windows that lie
entirely inside the public prefix (``sk-ant-oat01-`` / ``sk-ant-api03-``) are left out: that
prefix is the published format marker every key of that kind shares, it carries none of the
key's secret part, and the app may legitimately show or match it (the redactor's token-shape
pattern, the wrong-route hints). Every window that reaches into the random part is checked.

Secrets are fictional and built at run time from parts, with a body of 48 characters drawn by a
generator seeded with the test's own label, so the literal never appears in the repository, each
test has its own values, and an 8-character window of the body is distinctive (62**8 values).
The keychain is the autouse in-memory keyring (or a failing test backend); the Claude provider
runs over the SDK's real client with the replaying transport (no CLI process, no network), or a
stand-in provider that raises. The environment secret is passed only to the test app's wiring.
"""

import logging
from pathlib import Path

import pytest
from claude_agent_sdk import (
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
)
from claude_agent_sdk._errors import MessageParseError
from httpx import Response

from app.domain.errors import (
    InvalidModelOutput,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderRequestRejected,
    UsageLimitReached,
)
from app.logging_config import SDK_LOGGER
from app.onboarding.steps.provider import (
    MSG_API_USAGE_LIMIT,
    MSG_AUTH,
    MSG_NOT_INSTALLED,
    MSG_TIMEOUT,
    NOT_CONNECTED,
)
from app.services.provider_setup import MSG_TEST_FAILED
from tests.conftest import ALLOW_FAKES_VARIABLE
from tests.fakes.claude_query import success_frames
from tests.fakes.keyring_backends import FailingKeyring, MemoryKeyring
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_provider import (
    API_KEY_KIND,
    API_KEY_VARIABLE,
    FAKE_KIND,
    TOKEN_KIND,
    TOKEN_VARIABLE,
    keychain,
    make_wiring,
)
from tests.integration.test_setup_redirect import outline
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.leak_scan import (
    API_KEY_PREFIX,
    CONNECTED,
    GUIDE,
    TOKEN_PREFIX,
    Evidence,
    RawLogRecords,
    Script,
    ScriptedFactory,
    Secret,
    Session,
    app_session,
    assert_no_leaks,
    connected,
    error_frames,
    failure_scripts,
    fictional_secret,
    find_leaks,
    scan_after_stop,
    visit_every_page,
)

pytestmark = pytest.mark.integration

GUIDE_TEST = "/setup/provider/test"
LEAVE = "/setup/leave"
SETTINGS_PROVIDERS = "/settings/providers"
SETTINGS_TEST = "/settings/providers/test"


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


@pytest.fixture
def raw_records(monkeypatch: pytest.MonkeyPatch) -> RawLogRecords:
    records = RawLogRecords()
    records.install(monkeypatch)
    return records


# --- flows ---------------------------------------------------------------------------------


def connection_result(response: Response) -> str:
    assert response.status_code == 200, (response.status_code, response.text[:300])
    result = [e for e in outline(response.text) if e.attrs.get("id") == "connection-result"]
    assert len(result) == 1, "one connection result"
    return result[0].text


def run_test(
    session: Session,
    factory: ScriptedFactory,
    path: str,
    kind: str,
    credential: str,
    script: Script,
    *,
    htmx: bool,
) -> str:
    """Post "Test connection" with ``script`` as the provider's behaviour; checks the test
    ran (the provider was built) and shows "Connected" or the message for the script's
    failure kind."""
    factory.script = script
    before = len(factory.runs)
    response = session.post(path, {"kind": kind, "credential": credential}, htmx=htmx)
    text = connection_result(response)
    assert len(factory.runs) == before + 1, f"{script.name}: the connection test ran"
    if script.failure is None:
        assert text.startswith(CONNECTED), (script.name, text)
    else:
        assert text == failure_message(kind, script.failure), (script.name, text)
    return text


def failure_message(kind: str, failure: str) -> str:
    """What the guide and Settings show for a failed test of ``kind`` (ADR-068 point 5)."""
    worded = {
        "auth": MSG_AUTH[kind],
        "not_installed": MSG_NOT_INSTALLED,
        "timeout": MSG_TIMEOUT,
        "other": MSG_TEST_FAILED,
        "usage_limit": (
            MSG_API_USAGE_LIMIT if kind == API_KEY_KIND else UsageLimitReached().user_message
        ),
        "network": NetworkUnavailable().user_message,
        "rejected": ProviderRequestRejected().user_message,
        "invalid_output": InvalidModelOutput().user_message,
    }
    return NOT_CONNECTED + worded[failure]


def save(session: Session, path: str, kind: str, credential: str, *, htmx: bool) -> Response:
    response = session.post(path, {"kind": kind, "credential": credential}, htmx=htmx)
    assert response.status_code in (200, 303), (response.status_code, response.text[:300])
    return response


def remove_with_confirm(session: Session, path: str, kind: str, *, htmx: bool) -> None:
    """Ask, keep, ask again, then confirm (ADR-068 point 4)."""
    asked = session.post(path, {"remove": kind}, htmx=htmx)
    assert asked.status_code == 200, (asked.status_code, asked.text[:300])
    kept = session.post(path, {"keep": kind}, htmx=htmx)
    assert kept.status_code in (200, 303), (kept.status_code, kept.text[:300])
    asked_again = session.post(path, {"remove": kind}, htmx=htmx)
    assert asked_again.status_code == 200, (asked_again.status_code, asked_again.text[:300])
    removed = session.post(path, {"remove": kind, "confirm": kind}, htmx=htmx)
    assert removed.status_code in (200, 303), (removed.status_code, removed.text[:300])


def provider_flows(
    session: Session,
    factory: ScriptedFactory,
    save_path: str,
    test_path: str,
    *,
    token: Secret,
    replacement: Secret,
    api_key: Secret,
    htmx: bool,
) -> None:
    """Save, replace, test (success and every failure kind, pasted and saved), refuse a key
    pasted under the wrong route, remove with confirm, and end with ``replacement`` saved."""
    save(session, save_path, TOKEN_KIND, token.value, htmx=htmx)
    save(session, save_path, API_KEY_KIND, api_key.value, htmx=htmx)
    run_test(session, factory, test_path, API_KEY_KIND, api_key.value, connected(), htmx=htmx)
    run_test(session, factory, test_path, TOKEN_KIND, token.value, connected(), htmx=htmx)
    # Replace the token, then test with the new one pasted and with the saved one.
    save(session, save_path, TOKEN_KIND, replacement.value, htmx=htmx)
    run_test(session, factory, test_path, TOKEN_KIND, replacement.value, connected(), htmx=htmx)
    for script in failure_scripts("Fictional: the request failed."):
        run_test(session, factory, test_path, TOKEN_KIND, replacement.value, script, htmx=htmx)
        run_test(session, factory, test_path, TOKEN_KIND, "", script, htmx=htmx)
        run_test(session, factory, test_path, API_KEY_KIND, "", script, htmx=htmx)
    # Pasted under the wrong route: refused (422) and never echoed.
    for kind, value in ((API_KEY_KIND, replacement.value), (TOKEN_KIND, api_key.value)):
        refused = session.post(save_path, {"kind": kind, "credential": value}, htmx=htmx)
        assert refused.status_code == 422, (refused.status_code, refused.text[:300])
        tried = session.post(test_path, {"kind": kind, "credential": value}, htmx=htmx)
        assert tried.status_code == 422, (tried.status_code, tried.text[:300])
    # The fake chosen with a key pasted: the key is not kept.
    save(session, save_path, FAKE_KIND, token.value, htmx=htmx)
    remove_with_confirm(session, save_path, API_KEY_KIND, htmx=htmx)
    remove_with_confirm(session, save_path, TOKEN_KIND, htmx=htmx)
    save(session, save_path, TOKEN_KIND, replacement.value, htmx=htmx)
    run_test(session, factory, test_path, TOKEN_KIND, "", connected(), htmx=htmx)


# --- AC-M1.4 ---------------------------------------------------------------------------------


def test_ac_m1_4_saved_secret_never_in_responses_pages_logs_or_db(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    raw_records: RawLogRecords,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guide (reopened) and Settings: save, replace, test with every outcome, wrong-route
    refusals and confirmed removal; every page after saving and after a restart; a run whose
    keychain fails. No surface holds any of the three keys or any 8-character window of
    them."""
    token = fictional_secret(TOKEN_PREFIX, "saved-token")
    replacement = fictional_secret(TOKEN_PREFIX, "replacement-token")
    api_key = fictional_secret(API_KEY_PREFIX, "saved-api-key")
    secrets = (token, replacement, api_key)
    finished_guide(tmp_path)
    evidence = Evidence()

    factory = ScriptedFactory(make_wiring())
    with app_session(tmp_path, evidence, "guide reopened", wiring=factory, setup=True) as session:
        for htmx in (True, False):
            provider_flows(
                session,
                factory,
                GUIDE,
                GUIDE_TEST,
                token=token,
                replacement=replacement,
                api_key=api_key,
                htmx=htmx,
            )
        visit_every_page(session)
        left = session.post(LEAVE, {}, htmx=False)
        assert left.status_code == 303, left.status_code
        for htmx in (True, False):
            provider_flows(
                session,
                factory,
                SETTINGS_PROVIDERS,
                SETTINGS_TEST,
                token=token,
                replacement=replacement,
                api_key=api_key,
                htmx=htmx,
            )
        visit_every_page(session)
        assert factory.credentials_sent, "the provider received the saved key"
        assert set(factory.credentials_sent) <= {s.value for s in secrets} | {None}
    assert keychain(memory_keyring) == {"claude_code_oauth_token": replacement.value}

    # Restart: pages and a test with the saved key.
    factory = ScriptedFactory(make_wiring())
    with app_session(tmp_path, evidence, "restarted", wiring=factory) as session:
        visit_every_page(session)
        for script in [connected(), *failure_scripts("Fictional: failed after restart.")]:
            run_test(session, factory, SETTINGS_TEST, TOKEN_KIND, "", script, htmx=True)
        visit_every_page(session)
    assert factory.credentials_sent, "the provider was called after the restart"
    assert set(factory.credentials_sent) == {replacement.value}, "the saved key reached it"

    # A keychain that fails: pasted keys are refused, and still never shown or written.
    failing = make_wiring(backend=FailingKeyring())
    with app_session(tmp_path, evidence, "keychain failing", wiring=failing) as session:
        for path, test_path in ((SETTINGS_PROVIDERS, SETTINGS_TEST), (GUIDE, GUIDE_TEST)):
            for htmx in (True, False):
                for kind, secret in ((TOKEN_KIND, token), (API_KEY_KIND, api_key)):
                    form = {"kind": kind, "credential": secret.value}
                    saved = session.post(path, form, htmx=htmx)
                    assert saved.status_code == 422, (path, kind, saved.status_code)
                    tried = session.post(test_path, form, htmx=htmx)
                    assert tried.status_code == 422, (test_path, kind, tried.status_code)
                confirm = {"remove": TOKEN_KIND, "confirm": TOKEN_KIND}
                removed = session.post(path, confirm, htmx=htmx)
                assert removed.status_code == 422, (path, removed.status_code)
        visit_every_page(session)

    scan_after_stop(tmp_path, evidence, capsys)
    evidence.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    assert_no_leaks(secrets, evidence)


def test_ac_m1_4_no_partial_secret_substring_in_responses(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    raw_records: RawLogRecords,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Near-miss pastes (split by a space or a line break, a non-ASCII character, too long,
    the wrong route) are refused without echoing any piece of the key; a saved key is shown
    with no characters, so neither its first nor its last 6 characters appear either (no
    "...abc123"-style mask)."""
    token = fictional_secret(TOKEN_PREFIX, "partial-token")
    api_key = fictional_secret(API_KEY_PREFIX, "partial-api-key")
    finished_guide(tmp_path)
    evidence = Evidence()
    half = len(token.value) // 2
    near_misses = [
        (TOKEN_KIND, token.value[:half] + " " + token.value[half:]),
        (TOKEN_KIND, token.value[:half] + "\n" + token.value[half:]),
        (TOKEN_KIND, token.value + "é"),
        (TOKEN_KIND, token.value * 25),  # over the 1000-character limit
        (API_KEY_KIND, token.value),
        (TOKEN_KIND, api_key.value),
        (API_KEY_KIND, api_key.value[:half] + "\t" + api_key.value[half:]),
    ]

    factory = ScriptedFactory(make_wiring())
    with app_session(tmp_path, evidence, "near misses", wiring=factory) as session:
        for path, test_path in ((SETTINGS_PROVIDERS, SETTINGS_TEST), (GUIDE, GUIDE_TEST)):
            for htmx in (True, False):
                for kind, pasted in near_misses:
                    refused = session.post(path, {"kind": kind, "credential": pasted}, htmx=htmx)
                    assert refused.status_code == 422, (kind, refused.status_code)
                    tried = session.post(test_path, {"kind": kind, "credential": pasted}, htmx=htmx)
                    assert tried.status_code == 422, (kind, tried.status_code)
                save(session, path, API_KEY_KIND, api_key.value, htmx=htmx)
                save(session, path, TOKEN_KIND, token.value, htmx=htmx)
                run_test(session, factory, test_path, TOKEN_KIND, "", connected(), htmx=htmx)
        visit_every_page(session)
    assert keychain(memory_keyring) == {
        "claude_code_oauth_token": token.value,
        "anthropic_api_key": api_key.value,
    }
    with app_session(
        tmp_path, evidence, "restarted", wiring=ScriptedFactory(make_wiring())
    ) as session:
        visit_every_page(session)

    scan_after_stop(tmp_path, evidence, capsys)
    evidence.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    for secret in (token, api_key):
        assert find_leaks(secret, evidence) == []
        body = secret.value[len(secret.public_prefix) :]
        for piece in (body[:6], body[-6:]):
            shown = [where for where, text in evidence.response_texts() if piece in text]
            assert shown == [], f"{secret.label}: a 6-character end of the key is shown: {shown}"


def test_ac_m1_4_env_secret_never_in_outputs(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    raw_records: RawLogRecords,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Credentials from the environment fallback (``CLAUDE_CODE_OAUTH_TOKEN`` and
    ``ANTHROPIC_API_KEY``, ADR-044 item 7, ADR-062), given only to the test app's wiring: the
    provider receives them, and no output holds them."""
    env_token = fictional_secret(TOKEN_PREFIX, "environment-token")
    env_api_key = fictional_secret(API_KEY_PREFIX, "environment-api-key")
    environ = {
        ALLOW_FAKES_VARIABLE: "1",
        TOKEN_VARIABLE: env_token.value,
        API_KEY_VARIABLE: env_api_key.value,
    }
    finished_guide(tmp_path)
    evidence = Evidence()

    for label, setup, paths in (
        ("settings", False, (SETTINGS_PROVIDERS, SETTINGS_TEST)),
        ("guide reopened", True, (GUIDE, GUIDE_TEST)),
    ):
        factory = ScriptedFactory(make_wiring(environ))
        with app_session(tmp_path, evidence, label, wiring=factory, setup=setup) as session:
            save_path, test_path = paths
            for htmx in (True, False):
                for kind in (TOKEN_KIND, API_KEY_KIND):
                    saved = save(session, save_path, kind, "", htmx=htmx)
                    assert saved.status_code in (200, 303)
                    for script in [connected(), *failure_scripts("Fictional: env failure.")]:
                        run_test(session, factory, test_path, kind, "", script, htmx=htmx)
            visit_every_page(session)
        sent = {value for value in factory.credentials_sent if value is not None}
        assert sent == {env_token.value, env_api_key.value}, "the provider used the variables"
    assert keychain(memory_keyring) == {}, "environment credentials are never copied"

    scan_after_stop(tmp_path, evidence, capsys)
    evidence.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    assert_no_leaks((env_token, env_api_key), evidence)


def _secret_echo_scripts(value: str) -> list[Script]:
    """Provider failures whose error text, stderr or exception details quote ``value``."""
    said = f"Fictional: invalid credential {value} was rejected"
    stderr = (f"error: token {value} is invalid", f"debug: CLAUDE_CODE_OAUTH_TOKEN={value}")
    no_object = success_frames(None)
    no_object[2]["result"] = said

    def wrapped() -> BaseException:
        try:
            raise ValueError(said)
        except ValueError as cause:
            error = RuntimeError("Fictional: wrapper without the value")
            error.__cause__ = cause
            return error

    return [
        Script(
            "auth echoes the key",
            error_frames(status=401, assistant_error="authentication_failed", text=said),
            stderr=stderr,
            failure="auth",
        ),
        Script(
            "usage limit echoes the key",
            error_frames(status=429, assistant_error="rate_limit", text=said, stderr_echo=said),
            stderr=stderr,
            failure="usage_limit",
        ),
        Script(
            "rejected echoes the key",
            error_frames(status=400, assistant_error="invalid_request", text=said),
            failure="rejected",
        ),
        Script(
            "network echoes the key",
            error_frames(status=529, assistant_error="server_error", text=said),
            failure="network",
        ),
        Script("invalid output echoes the key", no_object, stderr=stderr, failure="invalid_output"),
        Script(
            "process error with stderr",
            stderr=stderr,
            transport_error=lambda: ProcessError(said, exit_code=1, stderr="\n".join(stderr)),
            failure="network",
        ),
        Script(
            "connection error",
            transport_error=lambda: CLIConnectionError(said),
            failure="network",
        ),
        Script(
            "undecodable output",
            transport_error=lambda: CLIJSONDecodeError(said, ValueError(said)),
            failure="invalid_output",
        ),
        Script(
            "unparsable message",
            transport_error=lambda: MessageParseError(said, {"type": "x", "text": said}),
            failure="invalid_output",
        ),
        Script(
            "not installed",
            transport_error=lambda: CLINotFoundError(said, cli_path=f"/fictional/{value}"),
            failure="not_installed",
        ),
        Script("timeout", provider_error=lambda: TimeoutError(said), failure="timeout"),
        Script("other", provider_error=lambda: RuntimeError(said), failure="other"),
        Script("other with cause", provider_error=wrapped, failure="other"),
        Script(
            "app error detail",
            provider_error=lambda: ProviderAuthError(detail=said),
            failure="auth",
        ),
    ]


def test_ac_m1_4_provider_error_with_secret_in_stderr_not_leaked(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    raw_records: RawLogRecords,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provider failure whose stderr, error frames, exception message, cause or detail
    quotes the saved key: the test reads as a mapped failure, and the key reaches no
    response, log record (before redaction), log file, console, database row or file."""
    token = fictional_secret(TOKEN_PREFIX, "echoed-token")
    api_key = fictional_secret(API_KEY_PREFIX, "echoed-api-key")
    finished_guide(tmp_path)
    evidence = Evidence()

    factory = ScriptedFactory(make_wiring())
    with app_session(tmp_path, evidence, "provider errors", wiring=factory) as session:
        for kind, secret in ((TOKEN_KIND, token), (API_KEY_KIND, api_key)):
            save(session, SETTINGS_PROVIDERS, kind, secret.value, htmx=True)
            for script in _secret_echo_scripts(secret.value):
                for path in (SETTINGS_TEST, GUIDE_TEST):
                    for htmx in (True, False):
                        run_test(session, factory, path, kind, "", script, htmx=htmx)
                run_test(session, factory, SETTINGS_TEST, kind, secret.value, script, htmx=True)
        visit_every_page(session)
    sent = {value for value in factory.credentials_sent if value is not None}
    assert sent == {token.value, api_key.value}, "the failing provider had the saved keys"
    sdk_errors = [
        name
        for name, level in raw_records.sources
        if name.startswith(SDK_LOGGER + ".") and level >= logging.ERROR
    ]
    assert sdk_errors, "control: the SDK logged its errors (with the key in their text)"

    scan_after_stop(tmp_path, evidence, capsys)
    evidence.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    assert_no_leaks((token, api_key), evidence)
