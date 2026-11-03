"""Step 2a: choose the AI provider and save its credential (M1.17; AC-M1.4, AC-M1.5; ADR-052).

A valid submit saves and stays on the step: it is done only after "Connected" (M1.18). Only
the chosen kind is shown back after a refused submit; the credential never is. Without an open
workspace (step 1 not done) the step asks the user to finish step 1 first.

The service is injected by wiring as a getter, since it needs the workspace's database.

``test`` (M1.18) saves, then tests the connection inside the POST, one test at a time (a second
one while a test runs is answered at once). "Connected" marks the step done through the
injected ``mark_done`` (which also refreshes the setup decision), but only when the settings
are still the version that was tested; it is kept in memory for this run only, so a later GET
shows it without running a test. Any change to the settings forgets it. Failures are worded
for the chosen sign-in route here; the service reports what kind of failure it was.

Removing a saved key asks first (M1.21 follow-up): ``remove=<kind>`` alone changes nothing and
answers ``StepResult.view`` ``{"confirm_remove": kind}`` (the inline question); only a submit
that also carries ``confirm=<kind>`` deletes. ``keep=<kind>`` ("Keep it") changes nothing and
answers ``{"kept": kind}``, so focus goes back to that kind's Remove button.

Settings > Providers (M1.21) uses this same handler, so saves, tests, the one-at-a-time lock
and the remembered "Connected" are shared with the step. A test from Settings passes
``mark_done=False``: it never touches the guide's state, and its "Connected" is remembered
only once the step is done (so the step never offers Continue before it is).

``save_tiers`` (Settings only, M1.22) maps the Fast and Thorough tiers to models; like any
other change to the settings, a saved map forgets the remembered "Connected" (and then asks
for a new test).

``demo`` (M2.26; wiring's ``demo_workspace``, decided at start-up) is passed on to the form:
in the demo the practice provider is preselected while nothing is saved and needs nothing
pasted, and the guide says so.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from app.onboarding.registry import ConnectionCheck, Form, StepResult
from app.services.provider_setup import (
    API_KEY_VARIABLE,
    CREDENTIAL_NAME,
    MSG_NOTHING_TO_REMOVE,
    TOKEN_VARIABLE,
    ProviderSetupResult,
    ProviderSetupService,
    ProviderTestResult,
)

TERMS_URL = "https://docs.claude.com/en/docs/agent-sdk/overview"
ACTIONS = ("save", "delete")
MSG_STEP_1_FIRST = "Finish step 1 first: choose where the app keeps your data."
MSG_UNKNOWN_ACTION = "That choice isn't available. Reload the page and try again."
MSG_NEXT = "Next, test the connection."
MSG_TEST_AGAIN = "You can test the connection again to check it."
MSG_NOT_RECORDED = "Connected, but the app couldn't record it. Try again."
MSG_BUSY = "A connection test is already running. Wait for it to finish."
MSG_CHANGED = "The provider changed while testing. Test again."
MSG_KEPT = "The saved {name} was kept."
MSG_TEST_TIERS = "Test the connection again to check them."
NOT_CONNECTED = "Not connected: "

TOKEN_KIND: Final = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
API_KEY_KIND: Final = "claude_api_key"
EXISTING_KIND: Final = "claude_existing_login"
CLAUDE_KINDS: Final = frozenset({TOKEN_KIND, API_KEY_KIND, EXISTING_KIND})
# Failures worded for the sign-in route (M1.18 review); anything else keeps the domain text.
MSG_AUTH: Final[Mapping[str, str]] = {
    TOKEN_KIND: (
        "The token was rejected or has expired. Run claude setup-token again and paste "
        "the new token."
    ),
    API_KEY_KIND: "The API key was rejected. Create a new key in Claude Console and paste it.",
    EXISTING_KIND: (
        "The Claude Code sign-in on this computer didn't work. Sign in to Claude Code again, "
        "then test again."
    ),
}
MSG_NOT_INSTALLED = (
    "Claude Code wasn't found. Reinstall the app's dependencies (uv sync), then test again."
)
MSG_API_USAGE_LIMIT = (
    "Your API usage limit or credit was reached. Check your Claude Console account, then try again."
)
MSG_TIMEOUT = "Claude didn't answer in time. Check your connection and try again."

_log = logging.getLogger(__name__)


class ProviderStep:
    echo_fields: tuple[str, ...] = ("kind",)  # never the credential

    def __init__(
        self,
        service: Callable[[], ProviderSetupService | None],
        *,
        data_dir: Callable[[], Path | None],
        mark_done: Callable[[], None],
        is_done: Callable[[], bool] = lambda: False,
        demo: bool = False,
    ) -> None:
        self._demo = demo  # fixed at start-up; never from a request or a setting
        self._service = service
        self._data_dir = data_dir
        self._mark_done = mark_done  # blocking: marks the step done, refreshes the decision
        self._is_done = is_done  # no I/O: the cached step status
        self._connected: str | None = None  # "Connected (...)" from a test in this run
        # The service (one per open database) the text above was earned with: a later workspace,
        # such as the one that opens after delete all data, starts without it.
        self._connected_for: ProviderSetupService | None = None
        self._testing = asyncio.Lock()  # one connection test at a time

    def context(self) -> Mapping[str, object]:
        common: dict[str, object] = {
            "token_variable": TOKEN_VARIABLE,
            "api_key_variable": API_KEY_VARIABLE,
            "terms_url": TERMS_URL,
            "demo": self._demo,
        }
        service = self._service()
        if service is None:
            return {**common, "workspace_open": False}
        view = service.view()
        if self._connected_for is not service:
            self._connected, self._connected_for = None, None
        return {
            **common,
            "workspace_open": True,
            "choices": view.choices,
            "kind": view.kind,
            "saved_kinds": view.saved_kinds,
            "saved": view.saved,
            "environment_variable": view.environment_variable,
            "keychain_failed": view.keychain_failed,
            "keychain_unavailable": view.keychain_unavailable,
            "connected": self._connected,
            "models": view.models,
            "tier_models": view.tier_models,
            "task_tiers": view.task_tiers,
            "settings_version": view.version,
        }

    def save_tiers(self, form: Form) -> StepResult:
        """Save the tier map from ``fast`` and ``thorough`` (Settings). Blocking."""
        service = self._service()
        if service is None:
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        before = service.settings_version()
        result = service.save_tiers(_first(form, "fast") or "", _first(form, "thorough") or "")
        forgot = False
        if service.settings_version() != before:
            forgot = self._connected is not None
            self._connected = None
        return _step_result(result, then=MSG_TEST_TIERS if forgot and result.ok else None)

    def save_task_tiers(self, version: int, chosen: Mapping[str, str]) -> StepResult:
        """Move tasks between the tiers (Settings only, M9.2). ``VersionConflict`` for a stale
        ``version``; a refused task or tier is an ``errors`` entry per name. The remembered
        "Connected" stays: the connection test uses the Fast tier model, not a task. Blocking."""
        service = self._service()
        if service is None:
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        return _step_result(service.save_task_tiers(version, chosen))

    def submit(self, form: Form) -> StepResult:
        """``action=save`` (the default) saves the choice; a ``remove=<kind>`` button (with
        no action, or ``action=delete``) asks to confirm, and removes that kind's saved key
        only with ``confirm=<kind>``; ``keep=<kind>`` answers the question. Blocking."""
        remove, keep = _first(form, "remove"), _first(form, "keep")
        action = _first(form, "action") or ("delete" if remove is not None else "save")
        if action not in ACTIONS or (action == "save" and remove is not None):
            return StepResult(ok=False, errors={"form": MSG_UNKNOWN_ACTION})
        if keep is not None and (remove is not None or "action" in form):
            return StepResult(ok=False, errors={"form": MSG_UNKNOWN_ACTION})
        service = self._service()
        if service is None:
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        if keep is not None:
            return _asked(service, keep, "kept")
        confirm = _first(form, "confirm")
        if action == "delete" and confirm is None:
            return _asked(service, remove or "", "confirm_remove")
        if action == "delete" and confirm != remove:
            return StepResult(ok=False, errors={"form": MSG_UNKNOWN_ACTION})
        before = service.settings_version()
        if action == "delete":
            result = service.delete_credential(remove or "")
            then = None
        else:
            kind = _first(form, "kind") or ""
            changed = service.view().kind != kind  # a new route needs a test, done or not
            result = service.save(kind, _first(form, "credential") or "")
            then = MSG_TEST_AGAIN if self._is_done() and not changed else MSG_NEXT
        if service.settings_version() != before:
            self._connected = None  # what was tested has changed (even on a refused save)
        if not result.ok:
            return _step_result(result)
        return _step_result(result, then=then)

    async def test(self, form: Form, *, mark_done: bool = True) -> ConnectionCheck:
        """Save, then test the connection; "Connected" marks the step done unless
        ``mark_done`` is False (Settings)."""
        if self._testing.locked():
            return ConnectionCheck(ok=False, message=MSG_BUSY)
        async with self._testing:
            return await self._test(form, mark_done=mark_done)

    async def _test(self, form: Form, *, mark_done: bool) -> ConnectionCheck:
        service, data_dir = self._service(), self._data_dir()
        if service is None or data_dir is None:
            return ConnectionCheck(ok=False, errors={"form": MSG_STEP_1_FIRST})
        kind, credential = _first(form, "kind") or "", _first(form, "credential") or ""
        result = await service.test_connection(kind, credential, data_dir=data_dir)
        if result.errors:
            return ConnectionCheck(ok=False, errors=result.errors)
        self._connected = None
        try:
            version = await asyncio.to_thread(service.settings_version)
        except Exception as error:
            _log.warning("AI provider setting couldn't be read (%s).", type(error).__name__)
            return ConnectionCheck(ok=False, message=MSG_NOT_RECORDED)
        if version != result.version:  # saved or removed elsewhere while the test ran
            return ConnectionCheck(ok=False, message=MSG_CHANGED)
        if not result.ok:
            return ConnectionCheck(ok=False, message=NOT_CONNECTED + _failure_text(kind, result))
        if mark_done:
            try:
                await asyncio.to_thread(self._mark_done)
            except Exception as error:  # sqlite messages can hold paths: type only
                _log.warning("Provider step couldn't be marked done (%s).", type(error).__name__)
                return ConnectionCheck(ok=False, message=MSG_NOT_RECORDED)
        message = f"Connected ({result.model})" if result.model else "Connected"
        if mark_done or self._is_done():
            self._connected, self._connected_for = message, service
        return ConnectionCheck(ok=True, message=message)


def _failure_text(kind: str, result: ProviderTestResult) -> str:
    """The failure worded for the sign-in route; the fake keeps the domain messages."""
    if result.failure == "timeout":
        return MSG_TIMEOUT
    if kind not in CLAUDE_KINDS:
        return result.message
    if result.failure == "auth":
        return MSG_AUTH[kind]
    if result.failure == "not_installed":
        return MSG_NOT_INSTALLED
    if result.failure == "usage_limit" and kind == API_KEY_KIND:
        return MSG_API_USAGE_LIMIT
    return result.message


def _asked(service: ProviderSetupService, kind: str, state: str) -> StepResult:
    """Nothing changes: show the removal question (``confirm_remove``) or close it
    (``kept``) for a kind that has a saved key; else there's nothing to remove."""
    if kind not in service.view().saved_kinds:
        return StepResult(ok=False, errors={"form": MSG_NOTHING_TO_REMOVE})
    message = MSG_KEPT.format(name=CREDENTIAL_NAME[kind]) if state == "kept" else None
    return StepResult(ok=True, view={state: kind}, message=message)


def _step_result(result: ProviderSetupResult, *, then: str | None = None) -> StepResult:
    if not result.ok:
        return StepResult(ok=False, errors=result.errors)
    message = " ".join(part for part in (result.message, then) if part)
    return StepResult(ok=True, stay=True, message=message or None)


def _first(form: Form, name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None
