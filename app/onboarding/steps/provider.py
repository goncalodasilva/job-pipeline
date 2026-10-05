"""Step 2a: choose the AI provider and save its credential (M1.17; AC-M1.4, AC-M1.5; ADR-052).

A valid submit saves and stays on the step: it is done only after "Connected" (M1.18). Only
the chosen kind is shown back after a refused submit; the credential never is. Without an open
workspace (step 1 not done) the step asks the user to finish step 1 first.

The service is injected by wiring as a getter, since it needs the workspace's database.
"""

from collections.abc import Callable, Mapping

from app.onboarding.registry import Form, StepResult
from app.services.provider_setup import (
    API_KEY_VARIABLE,
    TOKEN_VARIABLE,
    ProviderSetupResult,
    ProviderSetupService,
)

TERMS_URL = "https://docs.claude.com/en/docs/agent-sdk/overview"
ACTIONS = ("save", "delete")
MSG_STEP_1_FIRST = "Finish step 1 first: choose where the app keeps your data."
MSG_UNKNOWN_ACTION = "That choice isn't available. Reload the page and try again."


class ProviderStep:
    echo_fields: tuple[str, ...] = ("kind",)  # never the credential

    def __init__(self, service: Callable[[], ProviderSetupService | None]) -> None:
        self._service = service

    def context(self) -> Mapping[str, object]:
        common: dict[str, object] = {
            "token_variable": TOKEN_VARIABLE,
            "api_key_variable": API_KEY_VARIABLE,
            "terms_url": TERMS_URL,
        }
        service = self._service()
        if service is None:
            return {**common, "workspace_open": False}
        view = service.view()
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
        }

    def submit(self, form: Form) -> StepResult:
        """``action=save`` (the default) saves the choice; a ``remove=<kind>`` button (with
        no action, or ``action=delete``) removes that kind's saved key."""
        remove = _first(form, "remove")
        action = _first(form, "action") or ("delete" if remove is not None else "save")
        if action not in ACTIONS or (action == "save" and remove is not None):
            return StepResult(ok=False, errors={"form": MSG_UNKNOWN_ACTION})
        service = self._service()
        if service is None:
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        if action == "delete":
            return _step_result(service.delete_credential(remove or ""))
        kind = _first(form, "kind") or ""
        return _step_result(service.save(kind, _first(form, "credential") or ""))


def _step_result(result: ProviderSetupResult) -> StepResult:
    if not result.ok:
        return StepResult(ok=False, errors=result.errors)
    return StepResult(ok=True, stay=True, message=result.message)


def _first(form: Form, name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None
