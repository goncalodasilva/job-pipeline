"""Choose the AI provider and save its credential (M1.17; AC-M1.4, AC-M1.5; ADR-046, ADR-047).

``save`` checks the chosen kind against ``factory.provider_choices()``, checks the pasted
credential's shape, probes the keychain in a worker thread with a timeout (a hung unlock prompt
counts as "unavailable"), writes the credential under the kind's secret key and records only
which kinds have a saved key (``saved_sign_ins``) in the AI provider setting. Switching kind
keeps the other kinds' saved keys; ``delete_credential`` removes one (user decision, M1.17).
Switching to a kind of another provider (Claude or the fake) also re-checks the tier map in the
same save: a tier keeps its model only when the new provider offers it, else it goes back to
the provider's default (M1.21).
Then it refreshes provider health. Every keychain call (probe, write, delete, lookup) runs in
one timed worker thread; a timeout or an error counts as "keychain unavailable". ``view``
reads the settings row and cached facts only, never the keychain (spec 2.9.12).

``test_connection`` (M1.18; ADR-044 item 6, ADR-058) saves like ``save``, then awaits the saved
provider's connection test with the Fast tier model and a timeout; the fake comes from the
pasted text, which only picks its outcome (ADR-054). Blocking work runs in worker threads.

The credential never reaches a result, a log line (error type names only), the database or a
repr. A credential pasted with the fake or the existing sign-in selected is ignored (ADR-054).
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, TypeVar

from app.domain.errors import (
    AppError,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
    VersionConflict,
)
from app.domain.settings import AI_PROVIDER, AIProviderSettings, ProviderKind, TierMap
from app.modules.ai import AIProviderFactory, ProviderChoice
from app.modules.secrets import (
    ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN,
    ENVIRONMENT_VARIABLES,
    SecretLookupStore,
    SecretSource,
    SecretStore,
)
from app.services.settings import SettingsService, Versioned

_log = logging.getLogger(__name__)
T = TypeVar("T")

# The keychain key of each kind whose sign-in is a pasted credential; the Claude provider
# reads the same keys (``SECRET_KEY_BY_MODE``).
SECRET_KEY_BY_KIND: Final[Mapping[str, str]] = MappingProxyType(
    {
        "claude_subscription_token": CLAUDE_CODE_OAUTH_TOKEN,
        "claude_api_key": ANTHROPIC_API_KEY,
    }
)
CREDENTIAL_NAME: Final[Mapping[str, str]] = MappingProxyType(
    {"claude_subscription_token": "token", "claude_api_key": "API key"}
)
TOKEN_VARIABLE: Final = ENVIRONMENT_VARIABLES[CLAUDE_CODE_OAUTH_TOKEN]
API_KEY_VARIABLE: Final = ENVIRONMENT_VARIABLES[ANTHROPIC_API_KEY]
# Longer than the provider's own 60 s, so the provider answers first in normal cases.
CONNECTION_TEST_TIMEOUT: Final = 90
MIN_LENGTH: Final = 20
MAX_LENGTH: Final = 1000
_API_KEY_PREFIX: Final = "sk-ant-api"
_TOKEN_PREFIX: Final = "sk-ant-oat"  # noqa: S105 - a shape check, not a secret

MSG_KIND = "Choose how you want to connect to Claude."
MSG_PASTE_TOKEN = "Paste your token. You get it by running claude setup-token in a terminal."  # noqa: S105
MSG_PASTE_API_KEY = "Paste your API key from Claude Console."
MSG_SHAPE = (
    "This doesn't look like a complete {name}. Copy it again as one unbroken piece, "
    "no line breaks or gaps, and paste it here."
)
MSG_API_KEY_AS_TOKEN = (
    "This looks like an API key, not a token. "  # noqa: S105 - user-facing copy
    'To use it, choose "Anthropic API key" instead.'
)
MSG_TOKEN_AS_API_KEY = (
    "This looks like a subscription token, not an API key. To use it, choose "  # noqa: S105
    '"Claude subscription (sign-in token)" instead.'
)
MSG_NO_KEYCHAIN = (
    "Your system keychain can't be used right now, so the {name} wasn't saved. "
    "You can unlock the keychain and try again, or set the {variable} environment variable "
    "and restart the app, then choose this option again and save with the field empty."
)
MSG_SAVED_GONE = "No saved {name} was found in your keychain. Paste it again."
MSG_NOTHING_TO_REMOVE = "There's no saved key to remove."
MSG_NO_KEYCHAIN_DELETE = (
    "Your system keychain can't be used right now, so the saved {name} wasn't removed. "
    "Unlock the keychain and try again."
)
MSG_SAVE_FAILED = "Your choice couldn't be saved. Try again."
MSG_SAVED_WRITTEN = "Saved. Your {name} is in your system keychain."
MSG_SAVED_KEPT = "Saved. The app will use the {name} saved in your system keychain."
MSG_SAVED_ENVIRONMENT = "Saved. The app will use the {variable} environment variable."
MSG_SAVED_EXISTING = "Saved. The app will use the Claude Code sign-in on this computer."
MSG_SAVED_FAKE = "Saved. The app will use the fake provider, for testing only."
MSG_NOT_KEPT = "The pasted text wasn't needed, so it wasn't kept."
MSG_REMOVED = "The saved {name} was removed."
MSG_TEST_FAILED = "The connection test didn't finish. Nothing was lost; try again."


@dataclass(frozen=True)
class ProviderSetupResult:
    ok: bool
    errors: Mapping[str, str] = field(default_factory=dict)  # field name -> plain message
    message: str | None = None  # announced after a save


# What a failed connection test was, so the step can word it for the chosen route.
TestFailure = Literal["auth", "not_installed", "usage_limit", "network", "timeout", "other"]
# ``ConnectionResult`` carries only the message (ADR-058), so its kind is recognised by the
# domain error's user message.
_FAILURE_BY_MESSAGE: Final[Mapping[str, TestFailure]] = MappingProxyType(
    {
        ProviderAuthError().user_message: "auth",
        ProviderNotInstalled().user_message: "not_installed",
        UsageLimitReached().user_message: "usage_limit",
        NetworkUnavailable().user_message: "network",
    }
)


@dataclass(frozen=True)
class ProviderTestResult:
    """A connection test: ``errors`` when the save was refused (no test ran), else ``ok``
    and the plain ``message`` (spec 2.11), with the model the provider reported, what kind
    of ``failure`` it was, and the settings ``version`` that was tested (to check nothing
    changed while the test ran)."""

    ok: bool
    message: str = ""
    errors: Mapping[str, str] = field(default_factory=dict)
    model: str | None = None
    failure: TestFailure | None = None
    version: int | None = None


@dataclass(frozen=True)
class ProviderSetupView:
    """What the provider step shows; built from the settings row and cached facts only."""

    choices: tuple[ProviderChoice, ...]
    kind: ProviderKind | None
    saved_kinds: tuple[str, ...]  # kinds with a key saved in the keychain, each removable
    saved: bool  # the chosen kind's key is saved in the keychain
    environment_variable: str | None  # the variable providing the chosen kind's key
    keychain_failed: bool  # the last lookup of the chosen kind's key couldn't read the keychain
    keychain_unavailable: bool  # the last probe or write found no usable keychain


@dataclass(frozen=True)
class _Seen:
    """Where the last lookup found a key (never the value; ADR-047)."""

    source: SecretSource | None
    keychain_failed: bool


class _KeychainUnusable(Exception):
    """A keychain call timed out, failed, or an earlier one is still stuck."""


class ProviderSetupService:
    def __init__(
        self,
        settings: SettingsService,
        secrets: SecretStore,
        factory: AIProviderFactory,
        environment_keys: frozenset[str],
        refresh_health: Callable[[], object],
        *,
        probe_timeout: float = 5.0,
        connection_timeout: float = CONNECTION_TEST_TIMEOUT,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._factory = factory
        self._environment_keys = environment_keys
        self._refresh_health = refresh_health
        self._timeout = probe_timeout
        self._connection_timeout = connection_timeout
        # Cached after POSTs and shown on GET (no keychain read on GET).
        self._keychain_unavailable = False
        self._seen: dict[str, _Seen] = {}
        self._worker_lock = threading.Lock()
        self._stuck: threading.Thread | None = None

    def view(self) -> ProviderSetupView:
        current = self._settings.load(AI_PROVIDER).value
        kind = current.kind
        key = SECRET_KEY_BY_KIND.get(kind) if kind is not None else None
        seen = self._seen.get(key) if key is not None else None
        variable = (
            ENVIRONMENT_VARIABLES[key]
            if key is not None and key in self._environment_keys
            else None
        )
        return ProviderSetupView(
            choices=self._factory.provider_choices(),
            kind=kind,
            saved_kinds=tuple(current.saved_sign_ins),
            saved=key is not None and kind in current.saved_sign_ins,
            environment_variable=variable,
            keychain_failed=seen is not None and seen.keychain_failed,
            keychain_unavailable=self._keychain_unavailable,
        )

    def save(self, kind: str, credential: str) -> ProviderSetupResult:
        chosen = next((c.kind for c in self._factory.provider_choices() if c.kind == kind), None)
        if chosen is None:
            return _refused("kind", MSG_KIND)
        loaded = self._settings.load(AI_PROVIDER)
        text = credential.strip()
        key = SECRET_KEY_BY_KIND.get(chosen)
        if key is None:  # the existing sign-in or the fake: nothing pasted is kept
            message = MSG_SAVED_EXISTING if chosen == "claude_existing_login" else MSG_SAVED_FAKE
            return self._commit(
                loaded, chosen, message=_join(message, MSG_NOT_KEPT if text else None)
            )
        name = CREDENTIAL_NAME[chosen]
        if text:
            return self._save_pasted(loaded, chosen, key, text)
        if chosen in loaded.value.saved_sign_ins:
            seen = self._remember(key)
            gone = seen.source != "keychain" and not seen.keychain_failed
            if not gone:
                return self._commit(loaded, chosen, message=MSG_SAVED_KEPT.format(name=name))
            # The saved item has gone from the keychain: forget it.
            if key in self._environment_keys:
                return self._commit(loaded, chosen, drop=chosen, message=self._env_message(key))
            self._commit(loaded, loaded.value.kind, drop=chosen, message=None)
            return _refused("credential", MSG_SAVED_GONE.format(name=name))
        if key in self._environment_keys:
            return self._commit(loaded, chosen, message=self._env_message(key))
        return _refused("credential", _paste_message(chosen))

    async def test_connection(
        self, kind: str, credential: str, *, data_dir: Path
    ) -> ProviderTestResult:
        """Save like ``save`` (a refused save runs no test), then test the saved provider.
        Never raises: a timeout reads as "no connection", anything unexpected as a generic
        message logged by error type only."""
        saved = await asyncio.to_thread(self.save, kind, credential)
        if not saved.ok:
            return ProviderTestResult(ok=False, errors=saved.errors)
        loaded = await asyncio.to_thread(self._settings.load, AI_PROVIDER)
        current, version = loaded.value, loaded.version
        try:
            provider = (
                self._factory.fake_for_key(credential or None)
                if current.kind == "fake"
                else self._factory.ai_provider(current, data_dir=data_dir)
            )
            model = self._factory.connection_model(current)
            outcome = await asyncio.wait_for(
                provider.test_connection(model), self._connection_timeout
            )
        except TimeoutError:
            _log.warning("Connection test timed out.")
            return _failed(NetworkUnavailable().user_message, "timeout", version)
        except AppError as error:  # test_connection maps these itself (ADR-058)
            _log.warning("Connection test raised %s.", type(error).__name__)
            return _failed(error.user_message, None, version)
        except Exception as error:  # its text may quote a credential: type only
            _log.warning("Connection test failed (%s).", type(error).__name__)
            return _failed(MSG_TEST_FAILED, "other", version)
        if not outcome.ok:
            return _failed(outcome.message, None, version)
        return ProviderTestResult(
            ok=True, message=outcome.message, model=outcome.model, version=version
        )

    def settings_version(self) -> int:
        """The AI provider setting's version (blocking: a database read)."""
        return self._settings.load(AI_PROVIDER).version

    def delete_credential(self, kind: str) -> ProviderSetupResult:
        loaded = self._settings.load(AI_PROVIDER)
        target = next((k for k in loaded.value.saved_sign_ins if k == kind), None)
        if target is None:
            return _refused("form", MSG_NOTHING_TO_REMOVE)
        key = SECRET_KEY_BY_KIND[target]
        name = CREDENTIAL_NAME[target]
        try:
            self._require_keychain()
            self._timed("removing a keychain item", lambda: self._secrets.delete(key))
        except _KeychainUnusable:
            self._keychain_unavailable = True
            return _refused("form", MSG_NO_KEYCHAIN_DELETE.format(name=name))
        self._seen.pop(key, None)
        return self._commit(
            loaded, loaded.value.kind, drop=target, message=MSG_REMOVED.format(name=name)
        )

    # --- helpers ---------------------------------------------------------------------------

    def _save_pasted(
        self, loaded: Versioned[AIProviderSettings], kind: ProviderKind, key: str, text: str
    ) -> ProviderSetupResult:
        problem = _shape_problem(kind, text)
        if problem is not None:
            return _refused("credential", problem)
        try:
            self._require_keychain()
            self._timed("saving a keychain item", lambda: self._secrets.set(key, text))
        except _KeychainUnusable:
            self._keychain_unavailable = True
            return _refused("credential", _no_keychain(kind, key))
        self._keychain_unavailable = False
        name = CREDENTIAL_NAME[kind]
        result = self._commit(loaded, kind, add=kind, message=MSG_SAVED_WRITTEN.format(name=name))
        if not result.ok:
            if kind not in loaded.value.saved_sign_ins:
                self._undo_write(key)
            return result
        self._remember(key)
        return result

    def _commit(
        self,
        loaded: Versioned[AIProviderSettings],
        kind: ProviderKind | None,
        *,
        message: str | None,
        add: ProviderKind | None = None,
        drop: str | None = None,
    ) -> ProviderSetupResult:
        """Save the kind and the saved-key list (one retry after a version conflict), then
        refresh provider health."""

        def change(value: AIProviderSettings) -> AIProviderSettings:
            saved: set[str] = {k for k in value.saved_sign_ins if k != drop}
            if add is not None:
                saved.add(add)  # validated as a saved sign-in kind below
            tiers = self._recheck_tiers(value, kind)
            return AIProviderSettings.model_validate(
                {
                    **value.model_dump(),
                    "kind": kind,
                    "saved_sign_ins": tuple(saved),
                    "tiers": tiers.model_dump(),
                }
            )

        if not self._update(loaded, change):
            return _refused("form", MSG_SAVE_FAILED)
        self._refresh()
        return ProviderSetupResult(ok=True, message=message)

    def _update(
        self,
        loaded: Versioned[AIProviderSettings],
        change: Callable[[AIProviderSettings], AIProviderSettings],
    ) -> bool:
        current = loaded
        for attempt in range(2):
            try:
                self._settings.save(
                    AI_PROVIDER, change(current.value), expected_version=current.version
                )
                return True
            except VersionConflict:
                if attempt:
                    break
                _log.info("AI provider setting changed elsewhere; retrying once.")
            except Exception as error:  # sqlite messages can hold paths: log the type only
                _log.warning("AI provider setting couldn't be saved (%s).", type(error).__name__)
                return False
            try:
                current = self._settings.load(AI_PROVIDER)
            except Exception as error:
                _log.warning("AI provider setting couldn't be read (%s).", type(error).__name__)
                return False
        _log.warning("AI provider setting couldn't be saved (VersionConflict).")
        return False

    def _recheck_tiers(self, value: AIProviderSettings, kind: ProviderKind | None) -> TierMap:
        """Tiers map to the active provider's models (spec 3.7; ADR-051 follow-up). When the
        provider changes (Claude and the fake offer different models; Claude's sign-in routes
        and "nothing chosen yet" share Claude's), a tier keeps its model only if the new
        provider offers it, else it goes back to the provider's default."""
        offered = self._factory.known_models(kind)
        if offered == self._factory.known_models(value.kind):
            return value.tiers
        ids = {model.id for model in offered}
        return TierMap(
            fast=value.tiers.fast if value.tiers.fast in ids else None,
            thorough=value.tiers.thorough if value.tiers.thorough in ids else None,
        )

    def _undo_write(self, key: str) -> None:
        """Best effort: remove a keychain item whose kind couldn't be recorded."""
        with contextlib.suppress(_KeychainUnusable):  # already logged by type
            self._timed("removing an unrecorded keychain item", lambda: self._secrets.delete(key))

    def _remember(self, key: str) -> _Seen:
        """Look the key up (timed) and cache where it was found, never the value."""
        try:
            found = self._timed("reading a keychain item", lambda: self._where(key))
        except _KeychainUnusable:
            found = _Seen(source=None, keychain_failed=True)
        self._seen[key] = found
        return found

    def _where(self, key: str) -> _Seen:
        if isinstance(self._secrets, SecretLookupStore):
            lookup = self._secrets.lookup(key)
            source, failed = lookup.source, lookup.keychain_failed
        else:
            source, failed = ("keychain" if self._secrets.get(key) is not None else None), False
        # A keychain that can't be used at all can't say the item has gone either.
        if source != "keychain" and not failed and not self._secrets.available:
            failed = True
        return _Seen(source, failed)

    def _require_keychain(self) -> None:
        if not self._timed("checking the keychain", lambda: bool(self._secrets.available)):
            raise _KeychainUnusable

    def _timed(self, action: str, call: Callable[[], T]) -> T:
        """Run a keychain call in a worker thread (it may wait for an unlock prompt). Raises
        ``_KeychainUnusable`` on a timeout, an error (logged by type only) or while an earlier
        call is still stuck; daemon threads, so a stuck one never blocks the app's exit."""
        with self._worker_lock:
            if self._stuck is not None and self._stuck.is_alive():
                _log.warning("Keychain is still busy; %s skipped.", action)
                raise _KeychainUnusable
            answers: list[T] = []
            failures: list[str] = []

            def run() -> None:
                try:
                    answers.append(call())
                except Exception as error:  # its text may hold the value: type only
                    failures.append(type(error).__name__)

            worker = threading.Thread(target=run, name="keychain", daemon=True)
            worker.start()
            worker.join(self._timeout)
            if worker.is_alive():
                self._stuck = worker
                _log.warning("Keychain didn't answer in time (%s).", action)
                raise _KeychainUnusable
            self._stuck = None
            if failures:
                _log.warning("Keychain failed while %s (%s).", action, failures[0])
                raise _KeychainUnusable
            return answers[0]

    def _env_message(self, key: str) -> str:
        return MSG_SAVED_ENVIRONMENT.format(variable=ENVIRONMENT_VARIABLES[key])

    def _refresh(self) -> None:
        try:
            self._refresh_health()
        except Exception as error:
            _log.warning("Provider health couldn't be refreshed (%s).", type(error).__name__)


def _failed(message: str, failure: TestFailure | None, version: int) -> ProviderTestResult:
    kind = failure if failure is not None else _FAILURE_BY_MESSAGE.get(message, "other")
    return ProviderTestResult(ok=False, message=message, failure=kind, version=version)


def _refused(name: str, message: str) -> ProviderSetupResult:
    return ProviderSetupResult(ok=False, errors={name: message})


def _join(first: str, second: str | None) -> str:
    return f"{first} {second}" if second else first


def _paste_message(kind: str) -> str:
    return MSG_PASTE_TOKEN if kind == "claude_subscription_token" else MSG_PASTE_API_KEY


def _no_keychain(kind: str, key: str) -> str:
    return MSG_NO_KEYCHAIN.format(name=CREDENTIAL_NAME[kind], variable=ENVIRONMENT_VARIABLES[key])


def _shape_problem(kind: str, text: str) -> str | None:
    """Why the text can't be this kind's credential, or None. The text is never quoted."""
    if kind == "claude_subscription_token" and text.startswith(_API_KEY_PREFIX):
        return MSG_API_KEY_AS_TOKEN
    if kind == "claude_api_key" and text.startswith(_TOKEN_PREFIX):
        return MSG_TOKEN_AS_API_KEY
    printable = all("\x21" <= char <= "\x7e" for char in text)
    if not printable or not MIN_LENGTH <= len(text) <= MAX_LENGTH:
        return MSG_SHAPE.format(name=CREDENTIAL_NAME[kind])
    return None
