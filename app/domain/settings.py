"""Typed settings stored as JSON in the ``settings`` table (spec 2.8, 3.7; ADR-044 items 8, 11).

Pydantic validates the values: it does no I/O and is the project's validation library, so it
may live in the domain. Models are frozen (mappings included) and refuse unknown fields; no
field is meant for a secret, which lives only in the keychain (spec 2.9.9).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

Tier = Literal["fast", "thorough"]
# How the Claude provider signs in; the Claude provider uses these names too.
ClaudeAuthMode = Literal["subscription_token", "existing_login", "api_key"]
ProviderKind = Literal[
    "claude_subscription_token", "claude_existing_login", "claude_api_key", "fake"
]
# The kinds whose sign-in is a credential saved in the keychain.
SavedSignInKind = Literal["claude_subscription_token", "claude_api_key"]
WorkMode = Literal["remote", "hybrid", "on_site"]

_MAX_ITEMS = 20
_MAX_TASK_OVERRIDES = 100
_MAX_SALARY = 10**9
TaskName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
# Passed to the provider as a command-line value: never empty, never starting with "-".
ModelId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$", max_length=100)
]
# No control characters: these values reach prompts and pages.
Text = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"
    ),
]
TextList = Annotated[tuple[Text, ...], Field(max_length=_MAX_ITEMS)]


class _Setting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TierMap(_Setting):
    """Model ids per tier; ``None`` means the provider's default model."""

    fast: ModelId | None = None
    thorough: ModelId | None = None


class AIProviderSettings(_Setting):
    kind: ProviderKind | None = None  # None until the setup guide's provider step
    tiers: TierMap = TierMap()
    task_tiers: Annotated[Mapping[TaskName, Tier], Field(max_length=_MAX_TASK_OVERRIDES)] = Field(
        default_factory=lambda: MappingProxyType({})
    )  # per-task overrides
    # The kind whose credential was saved in the keychain (never the value): provider health
    # reads this instead of the keychain (ADR-046), and a switch of mode needs a new sign-in.
    sign_in_saved_for: SavedSignInKind | None = None

    @field_validator("task_tiers", mode="after")
    @classmethod
    def _read_only(cls, value: Mapping[str, Tier]) -> Mapping[str, Tier]:
        return MappingProxyType(dict(value))  # frozen=True doesn't reach inside a dict

    @field_serializer("task_tiers")
    def _as_dict(self, value: Mapping[str, Tier]) -> dict[str, Tier]:
        return dict(value)


class Salary(_Setting):
    amount: Annotated[int, Field(ge=0, le=_MAX_SALARY)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]  # ISO 4217 shape


class JobPreferences(_Setting):
    target_roles: Annotated[tuple[Text, ...], Field(min_length=1, max_length=_MAX_ITEMS)]
    locations: TextList = ()
    work_modes: tuple[WorkMode, ...] = ()
    minimum_salary: Salary | None = None
    must_haves: TextList = ()
    deal_breakers: TextList = ()
    cv_country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]  # ISO 3166-1 alpha-2


def claude_auth_mode(kind: ProviderKind | None) -> ClaudeAuthMode | None:
    """The Claude sign-in mode a provider kind stands for; None for other providers."""
    modes: Mapping[str, ClaudeAuthMode] = {
        "claude_subscription_token": "subscription_token",
        "claude_existing_login": "existing_login",
        "claude_api_key": "api_key",
    }
    return modes.get(kind) if kind is not None else None


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class SettingSpec(Generic[T]):
    """A setting with a default value: loading always gives a value."""

    key: str
    model: type[T]
    default: T


@dataclass(frozen=True)
class OptionalSettingSpec(Generic[T]):
    """A setting with no default (it must be filled in): loading may give None."""

    key: str
    model: type[T]
    default: None = None


AI_PROVIDER = SettingSpec("ai_provider", AIProviderSettings, AIProviderSettings())
JOB_PREFERENCES = OptionalSettingSpec("job_preferences", JobPreferences)
