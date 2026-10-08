"""AC-M1.9 (live, opt-in, run by the user; never in CI): ``test_connection`` succeeds with a
real subscription token and with a real API key.

Run only with ``JOBPIPELINE_LIVE_TESTS=1`` and ``-m live`` (``tests/live/conftest.py``). The
token comes only from ``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` and the key only from
``JOBPIPELINE_LIVE_ANTHROPIC_API_KEY`` (ADR-044 item 10). Without its variable the token test
fails, naming it; the API-key test skips (the approved skip). One minimal call per mode.

Each test builds the real ``ClaudeSdkProvider`` through the app's wiring, with the credential
saved under the app's keychain item in an in-memory backend (never the system keychain) and
an empty environment, and the CLI version check skipped as the CLI does. It also checks the
live points ADR-045 and ADR-058 left open: the keychain item each mode reads, that the CLI's
per-call config folder (``CLAUDE_CONFIG_DIR``) and working folder start empty and are removed
afterwards, and that no file the CLI wrote there, no log record and no console output holds
the credential. The names of the files the CLI wrote are recorded (``-s`` prints them; names
only, never contents). The credential is never printed: pytest's log handlers redact it
(``tests/live/conftest.py``), an unexpected exception from the call is reduced to its type, the
leak scan's helpers never show what they hold, and the test's folder is removed afterwards.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.cli import SDK_SKIP_VERSION_CHECK
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, ClaudeAuthMode, ProviderKind
from app.modules.ai.providers import claude_sdk
from app.modules.ai.providers.claude_sdk import CLAUDE_FAST_DEFAULT, ClaudeSdkProvider
from app.modules.secrets import KNOWN_SECRET_KEYS
from app.modules.secrets.providers.keyring_store import SERVICE_NAME
from app.wiring import clear_ai_temp
from tests.leak_scan import (
    API_KEY_PREFIX,
    TOKEN_PREFIX,
    Evidence,
    RawLogRecords,
    Secret,
    find_leaks,
)
from tests.live.support import (
    API_KEY_VARIABLE,
    KEYCHAIN_KEY_BY_MODE,
    TOKEN_VARIABLE,
    LiveCredential,
    live_wiring,
    read_credential,
    without_error_details,
)

pytestmark = pytest.mark.live

KIND_BY_MODE: dict[ClaudeAuthMode, ProviderKind] = {
    "subscription_token": "claude_subscription_token",
    "api_key": "claude_api_key",
}
PREFIX_BY_MODE: dict[ClaudeAuthMode, str] = {
    "subscription_token": TOKEN_PREFIX,
    "api_key": API_KEY_PREFIX,
}


async def test_ac_m1_9_test_connection_with_subscription_token(
    live_tmp: Path,
    live_log_redactor: Redactor,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
) -> None:
    credential = read_credential(os.environ, TOKEN_VARIABLE)
    if credential is None:
        pytest.fail(
            f"{TOKEN_VARIABLE} is not set: export a token from `claude setup-token` in it "
            "to run this live test.",
            pytrace=False,
        )
    await _check_connection(
        "subscription_token",
        credential,
        _Fixtures(live_tmp, live_log_redactor, monkeypatch, capfd, record_property),
    )


async def test_ac_m1_9_test_connection_with_api_key(
    live_tmp: Path,
    live_log_redactor: Redactor,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
) -> None:
    credential = read_credential(os.environ, API_KEY_VARIABLE)
    if credential is None:
        pytest.skip(f"{API_KEY_VARIABLE} is not set (approved skip, ADR-044 item 10)")
    await _check_connection(
        "api_key",
        credential,
        _Fixtures(live_tmp, live_log_redactor, monkeypatch, capfd, record_property),
    )


@dataclass(frozen=True)
class _Fixtures:
    folder: Path  # removed after the test (``live_tmp``)
    redactor: Redactor  # the one guarding pytest's log handlers
    monkeypatch: pytest.MonkeyPatch
    capfd: pytest.CaptureFixture[str]
    record_property: Callable[[str, object], None]


class _ReadRecorder:
    """Records which keychain items the app's store reads (service, user name only)."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []


class _CallFolders:
    """What each call's private folders held when the CLI was about to start, the names of
    the files in them just before removal, and their raw bytes for the leak scan."""

    def __init__(self, evidence: Evidence) -> None:
        self._evidence = evidence
        self.at_start: list[dict[str, list[str]]] = []
        self.before_removal: list[list[str]] = []
        self.removed: list[Path] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_build = claude_sdk.build_options
        original_remove = claude_sdk._remove_folder

        def build_options(**kwargs: Any) -> Any:
            # kwargs holds the credential: emptied once the call returns or raises, so no
            # frame of this wrapper keeps it.
            try:
                self._record_start(kwargs["workdir"], kwargs.get("config_dir"))
                return original_build(**kwargs)
            finally:
                kwargs.clear()

        def remove_folder(folder: Path) -> None:
            self.removed.append(folder)
            try:
                self.before_removal.append(_names(folder) if folder.is_dir() else ["<missing>"])
                self._evidence.snapshot_files("call folder before removal", folder)
            finally:
                original_remove(folder)

        monkeypatch.setattr(claude_sdk, "build_options", build_options)
        monkeypatch.setattr(claude_sdk, "_remove_folder", remove_folder)

    def _record_start(self, workdir: Path, config_dir: Path | None) -> None:
        self.at_start.append(
            {
                "work": _names(workdir),
                "config": _names(config_dir) if config_dir is not None else ["<none>"],
            }
        )


def _names(folder: Path) -> list[str]:
    """Relative paths of everything under ``folder`` (names only)."""
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


async def _check_connection(
    mode: ClaudeAuthMode, credential: LiveCredential, fixtures: _Fixtures
) -> None:
    monkeypatch = fixtures.monkeypatch
    monkeypatch.setenv(SDK_SKIP_VERSION_CHECK, "1")  # as the CLI sets it (ADR-058)
    wiring, backend = live_wiring(mode, credential, fixtures.redactor)
    reads = _ReadRecorder()
    original_get = backend.get_password

    def get_password(service: str, username: str) -> str | None:
        reads.reads.append((service, username))
        return original_get(service, username)

    monkeypatch.setattr(backend, "get_password", get_password)
    evidence = Evidence()
    records = RawLogRecords()
    records.install(monkeypatch)
    folders = _CallFolders(evidence)
    folders.install(monkeypatch)
    data_dir = fixtures.folder / "data"
    temp_root = clear_ai_temp(data_dir)
    kind = KIND_BY_MODE[mode]
    settings = AIProviderSettings(kind=kind, saved_sign_ins=(kind,))

    provider = wiring.ai_provider(settings, data_dir=data_dir)
    assert isinstance(provider, ClaudeSdkProvider)
    model = wiring.connection_model(settings)
    assert model == CLAUDE_FAST_DEFAULT
    try:
        result = await without_error_details(provider.test_connection(model))
    finally:
        # Taken even when the call raised, so pytest never shows the raw console output.
        captured = fixtures.capfd.readouterr()
        evidence.texts.append(("stdout", captured.out))
        evidence.texts.append(("stderr", captured.err))
        del captured

    # Leak scan first, so a failing connection still proves nothing leaked.
    evidence.texts.extend(records.entries)
    evidence.texts.append(("connection result", repr(result)))
    leaks = find_leaks(_secret(mode, credential), evidence)
    assert not leaks, "the credential leaked:\n" + "\n".join(leaks)

    assert result.ok, f"connection failed: {result.failure}: {result.message}"
    assert result.message == "Connected"
    fixtures.record_property("model_reported", result.model)

    # The keychain item this mode uses: only the app's item for this mode was read, no
    # other credential item, and nothing was written.
    item = (SERVICE_NAME, KEYCHAIN_KEY_BY_MODE[mode])
    credential_reads = [read for read in reads.reads if read[1] in KNOWN_SECRET_KEYS]
    assert credential_reads == [item]
    assert all(service == SERVICE_NAME for service, _ in reads.reads)
    assert list(backend.items) == [item]

    # First start with an empty config folder: one call, its folders empty at the start,
    # then removed with nothing left behind.
    assert folders.at_start == [{"work": [], "config": []}]
    assert len(folders.removed) == 1
    assert not folders.removed[0].exists()
    assert list(temp_root.iterdir()) == []
    written = folders.before_removal[0]
    fixtures.record_property("cli_files_in_call_folder", written)
    print(f"{mode}: files in the call folder before removal: {written}")


def _secret(mode: ClaudeAuthMode, credential: LiveCredential) -> Secret:
    """The leak scanner's view of the credential (its public prefix excluded from windows)."""
    prefix = PREFIX_BY_MODE[mode]
    value = credential.value
    return Secret(
        label=f"live {mode}",
        value=value,
        public_prefix=prefix if value.startswith(prefix) else "",
    )
