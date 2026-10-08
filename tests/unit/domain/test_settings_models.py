"""Typed settings models (M1.7, AC-M1.4 part, AC-M1.6 part; spec 2.8, 3.7; ADR-044 items 8, 11).

Every value here is fictional.
"""

import types
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.domain.settings import (
    AI_PROVIDER,
    JOB_PREFERENCES,
    AIProviderSettings,
    JobPreferences,
    OptionalSettingSpec,
    Salary,
    SettingSpec,
    TierMap,
)

SECRET_WORDS = ("key", "token", "secret", "password", "credential")


def minimal_preferences(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"target_roles": ["Lighthouse Keeper"], "cv_country": "PT"}
    payload.update(overrides)
    return payload


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is not None or isinstance(annotation, types.UnionType):
        for arg in typing.get_args(annotation):
            yield from _models_in(arg)


def reachable_models(root: type[BaseModel]) -> list[type[BaseModel]]:
    seen: list[type[BaseModel]] = []
    pending = [root]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.append(model)
        for field in model.model_fields.values():
            pending.extend(_models_in(field.annotation))
    return seen


# --- AI provider settings -------------------------------------------------------


def test_provider_settings_defaults() -> None:
    settings = AIProviderSettings()

    assert settings.kind is None
    assert settings.tiers == TierMap()
    assert settings.tiers.fast is None
    assert settings.tiers.thorough is None
    assert dict(settings.task_tiers) == {}

    restored = AIProviderSettings.model_validate_json(settings.model_dump_json())
    assert restored == settings

    configured = AIProviderSettings(
        kind="claude_subscription_token",
        tiers=TierMap(fast="model-fast-fictional", thorough="model-thorough-fictional"),
        task_tiers={"assess_fit": "thorough"},
    )
    assert AIProviderSettings.model_validate_json(configured.model_dump_json()) == configured


@pytest.mark.parametrize(
    "kind", ["claude_subscription_token", "claude_existing_login", "claude_api_key", "fake"]
)
def test_provider_kind_accepts_known_kinds(kind: str) -> None:
    assert AIProviderSettings.model_validate({"kind": kind}).kind == kind


@pytest.mark.parametrize("kind", ["openai", "", "Claude_Subscription"])
def test_provider_kind_rejects_unknown_kind(kind: str) -> None:
    with pytest.raises(ValidationError):
        AIProviderSettings.model_validate({"kind": kind})


def test_tier_map_rejects_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        AIProviderSettings.model_validate({"task_tiers": {"assess_fit": "medium"}})
    with pytest.raises(ValidationError):
        TierMap.model_validate({"medium": "model-medium-fictional"})


@pytest.mark.parametrize("key", ["assess_fit", "extract_cv", "a", "tailor_cv2"])
def test_task_tier_override_validates(key: str) -> None:
    settings = AIProviderSettings.model_validate({"task_tiers": {key: "fast"}})
    assert dict(settings.task_tiers) == {key: "fast"}


@pytest.mark.parametrize(
    "bad_key", ["Tailor CV", "", "tailor-cv", "2tailor", "_tailor", "tailor cv", "Tailor_cv"]
)
def test_task_tier_override_rejects_bad_task_names(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        AIProviderSettings.model_validate({"task_tiers": {bad_key: "fast"}})


# --- Job preferences -----------------------------------------------------------


def test_preferences_requires_target_role_and_cv_country() -> None:
    accepted = JobPreferences.model_validate(minimal_preferences())
    assert accepted.target_roles == ("Lighthouse Keeper",)
    assert accepted.cv_country == "PT"
    assert accepted.locations == ()
    assert accepted.work_modes == ()
    assert accepted.minimum_salary is None
    assert accepted.must_haves == ()
    assert accepted.deal_breakers == ()

    rejected: list[dict[str, Any]] = [
        {"cv_country": "PT"},
        minimal_preferences(target_roles=[]),
        minimal_preferences(target_roles=["   "]),
        minimal_preferences(target_roles=["Lighthouse Keeper", ""]),
        {"target_roles": ["Lighthouse Keeper"]},
        minimal_preferences(cv_country="pt"),
        minimal_preferences(cv_country="PRT"),
        minimal_preferences(cv_country="P"),
        minimal_preferences(cv_country=""),
        minimal_preferences(cv_country="P1"),
    ]
    for payload in rejected:
        with pytest.raises(ValidationError):
            JobPreferences.model_validate(payload)


def test_preferences_accepts_every_optional_field() -> None:
    prefs = JobPreferences.model_validate(
        minimal_preferences(
            locations=["Atlantis Harbour"],
            work_modes=["remote", "hybrid", "on_site"],
            minimum_salary={"amount": 42000, "currency": "EUR"},
            must_haves=["Sea view"],
            deal_breakers=["Night shifts on land"],
        )
    )
    assert prefs.work_modes == ("remote", "hybrid", "on_site")
    assert prefs.minimum_salary == Salary(amount=42000, currency="EUR")
    assert JobPreferences.model_validate_json(prefs.model_dump_json()) == prefs


def test_preferences_rejects_unknown_work_mode() -> None:
    with pytest.raises(ValidationError):
        JobPreferences.model_validate(minimal_preferences(work_modes=["office"]))


def test_salary_non_negative() -> None:
    assert Salary(amount=0, currency="EUR").amount == 0
    assert Salary(amount=55000, currency="GBP").currency == "GBP"

    with pytest.raises(ValidationError):
        Salary.model_validate({"amount": -1, "currency": "EUR"})
    for currency in ("eur", "EU", "EURO", "", "E1R"):
        with pytest.raises(ValidationError):
            Salary.model_validate({"amount": 100, "currency": currency})
    with pytest.raises(ValidationError):
        JobPreferences.model_validate(
            minimal_preferences(minimum_salary={"amount": -5, "currency": "EUR"})
        )


TEXT_LISTS = ("target_roles", "locations", "must_haves", "deal_breakers")


@pytest.mark.parametrize("field", TEXT_LISTS)
def test_preferences_text_limits(field: str) -> None:
    stripped = JobPreferences.model_validate(minimal_preferences(**{field: ["  Tide Watcher  "]}))
    assert getattr(stripped, field) == ("Tide Watcher",)

    at_limit = JobPreferences.model_validate(minimal_preferences(**{field: ["x" * 200]}))
    assert getattr(at_limit, field) == ("x" * 200,)
    twenty = [f"Item {n}" for n in range(20)]
    assert (
        len(getattr(JobPreferences.model_validate(minimal_preferences(**{field: twenty})), field))
        == 20
    )

    for bad in (["x" * 201], [f"Item {n}" for n in range(21)], ["  "], [""]):
        with pytest.raises(ValidationError):
            JobPreferences.model_validate(minimal_preferences(**{field: bad}))


# --- Model hygiene -------------------------------------------------------------


ALL_MODELS: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (TierMap, {}),
    (AIProviderSettings, {}),
    (Salary, {"amount": 1, "currency": "EUR"}),
    (JobPreferences, minimal_preferences()),
]


@pytest.mark.parametrize(("model", "payload"), ALL_MODELS, ids=lambda v: getattr(v, "__name__", ""))
def test_models_are_frozen_and_forbid_extra_fields(
    model: type[BaseModel], payload: dict[str, Any]
) -> None:
    instance = model.model_validate(payload)
    first_field = next(iter(model.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, first_field, getattr(instance, first_field))

    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unexpected_field": "Harbour"})


def test_ac_m1_4_settings_models_have_no_secret_fields() -> None:
    models = reachable_models(AI_PROVIDER.model) + reachable_models(JOB_PREFERENCES.model)
    assert {AIProviderSettings, TierMap, JobPreferences, Salary} <= set(models)
    for model in models:
        for name in model.model_fields:
            lowered = name.lower()
            assert not any(word in lowered for word in SECRET_WORDS), (model.__name__, name)

    for extra in ("api_key", "token", "oauth_token", "secret", "password"):
        with pytest.raises(ValidationError):
            AIProviderSettings.model_validate({"kind": "claude_api_key", extra: "fictional"})


def test_setting_specs() -> None:
    assert isinstance(AI_PROVIDER, SettingSpec)
    assert AI_PROVIDER.key == "ai_provider"
    assert AI_PROVIDER.model is AIProviderSettings
    assert AI_PROVIDER.default == AIProviderSettings()

    # No default: preferences must be filled in, so loading may give None.
    assert isinstance(JOB_PREFERENCES, OptionalSettingSpec)
    assert JOB_PREFERENCES.key == "job_preferences"
    assert JOB_PREFERENCES.model is JobPreferences
    assert JOB_PREFERENCES.default is None


def test_task_tier_overrides_cannot_be_changed_in_place() -> None:
    settings = AIProviderSettings(task_tiers={"tailor_cv": "fast"})

    with pytest.raises(TypeError):
        settings.task_tiers["summarize_job"] = "thorough"  # type: ignore[index]
    assert settings.model_dump()["task_tiers"] == {"tailor_cv": "fast"}
    assert AIProviderSettings.model_validate_json(settings.model_dump_json()) == settings
    # The shared default can't be changed through a loaded fallback either.
    with pytest.raises(TypeError):
        AI_PROVIDER.default.task_tiers["x"] = "fast"  # type: ignore[index]


@pytest.mark.parametrize(
    "model_id",
    ["--dangerously-flag", "-x", "", " sonnet", "a" * 101, "son net", "sonnet\n"],
)
def test_tier_map_rejects_model_ids_that_are_not_names(model_id: str) -> None:
    with pytest.raises(ValidationError):
        TierMap(fast=model_id)


@pytest.mark.parametrize("model_id", ["haiku", "claude-sonnet-5", "vendor/model:1.2@v3"])
def test_tier_map_accepts_model_names(model_id: str) -> None:
    assert TierMap(thorough=model_id).thorough == model_id


def test_preferences_text_rejects_control_characters() -> None:
    with pytest.raises(ValidationError):
        JobPreferences(target_roles=("Fictional\nrole",), cv_country="PT")
    with pytest.raises(ValidationError):
        JobPreferences(target_roles=("Role",), must_haves=("tab\there",), cv_country="PT")


def test_salary_has_an_upper_bound() -> None:
    assert Salary(amount=10**9, currency="EUR").amount == 10**9
    with pytest.raises(ValidationError):
        Salary(amount=10**9 + 1, currency="EUR")


def test_task_tier_overrides_are_bounded() -> None:
    too_many = {f"task_{index}": "fast" for index in range(101)}
    with pytest.raises(ValidationError):
        AIProviderSettings(task_tiers=too_many)


def test_claude_auth_mode_matches_every_claude_kind() -> None:
    from typing import get_args

    from app.domain.settings import ClaudeAuthMode, ProviderKind, claude_auth_mode

    claude_kinds = [kind for kind in get_args(ProviderKind) if kind.startswith("claude_")]
    assert sorted(claude_auth_mode(kind) for kind in claude_kinds) == sorted(
        get_args(ClaudeAuthMode)
    )
    assert claude_auth_mode("fake") is None
    assert claude_auth_mode(None) is None


def test_saved_sign_ins_default_empty() -> None:
    """M1.15 (ADR-046), widened in M1.17 (user decision, 2026-10-05): records which kinds have
    a saved credential, never the values; switching kind keeps the others."""
    assert AIProviderSettings().saved_sign_ins == ()

    for kind in ("claude_subscription_token", "claude_api_key"):
        saved = AIProviderSettings(kind=kind, saved_sign_ins=(kind,))
        assert saved.saved_sign_ins == (kind,)
        assert AIProviderSettings.model_validate_json(saved.model_dump_json()) == saved
    both = AIProviderSettings(
        saved_sign_ins=("claude_subscription_token", "claude_api_key", "claude_api_key")
    )
    assert both.saved_sign_ins == ("claude_api_key", "claude_subscription_token"), "unique, sorted"
    older = AIProviderSettings.model_validate_json('{"kind": "claude_api_key"}')
    assert older.saved_sign_ins == (), "settings saved before the field existed still load"

    for not_a_kind in (
        "Fictional-Saved-Value-0001",
        {"value": "fictional"},
        True,
        "claude_existing_login",  # no credential is saved for the computer's own sign-in
        "fake",
    ):
        with pytest.raises(ValidationError):
            AIProviderSettings.model_validate(
                {"kind": "claude_api_key", "saved_sign_ins": (not_a_kind,)}
            )
        with pytest.raises(ValidationError):  # the M1.15 field, mapped into the list
            AIProviderSettings.model_validate(
                {"kind": "claude_api_key", "sign_in_saved_for": not_a_kind}
            )


def test_legacy_sign_in_saved_for_still_loads() -> None:
    """Rows saved by M1.15/M1.16 hold ``sign_in_saved_for`` (one kind or null)."""
    one = AIProviderSettings.model_validate_json(
        '{"kind": "claude_api_key", "sign_in_saved_for": "claude_api_key"}'
    )
    assert one.saved_sign_ins == ("claude_api_key",)
    assert "sign_in_saved_for" not in one.model_dump_json(), "written back in the new shape"
    none = AIProviderSettings.model_validate_json('{"sign_in_saved_for": null}')
    assert none.saved_sign_ins == ()
