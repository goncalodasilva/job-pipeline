"""Typed settings stored as JSON in the ``settings`` table (spec 2.8, 3.7; ADR-044 items 8, 11).

Pydantic validates the values: it does no I/O and is the project's validation library, so it
may live in the domain. Models are frozen (mappings included) and refuse unknown fields; no
field is meant for a secret, which lives only in the keychain (spec 2.9.9).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Generic, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from app.domain.budgets import Budget
from app.domain.ids import is_safe_id
from app.domain.usage import ServiceId

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
    # The kinds whose credential is saved in the keychain (never the values): provider health
    # reads this instead of the keychain (ADR-046). Switching kind keeps the others' saved
    # keys; each is removed on its own (user decision, M1.17). Unique and sorted.
    saved_sign_ins: tuple[SavedSignInKind, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _legacy_saved_sign_in(cls, data: object) -> object:
        """Rows saved before M1.17 hold ``sign_in_saved_for`` (one kind or null)."""
        if isinstance(data, Mapping) and "sign_in_saved_for" in data:
            values = dict(data)
            legacy = values.pop("sign_in_saved_for")
            if legacy is not None and "saved_sign_ins" not in values:
                values["saved_sign_ins"] = (legacy,)
            return values
        return data

    @field_validator("saved_sign_ins", mode="after")
    @classmethod
    def _unique_sorted(cls, value: tuple[SavedSignInKind, ...]) -> tuple[SavedSignInKind, ...]:
        return tuple(sorted(set(value)))

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


class TaskRunnerSettings(_Setting):
    """How many background tasks run at once (spec 5 M2; UI in M2.22)."""

    max_concurrent: Annotated[int, Field(ge=1, le=3)] = 1


class UsageBudgets(_Setting):
    """The request budget per service (spec 3.7; ADR-076 items 4, 5). A service without an
    entry has no budget: never warned or blocked. ``Budget`` validates each entry."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    budgets: Mapping[ServiceId, Budget] = Field(default_factory=lambda: MappingProxyType({}))

    @field_validator("budgets", mode="after")
    @classmethod
    def _read_only(cls, value: Mapping[ServiceId, Budget]) -> Mapping[ServiceId, Budget]:
        return MappingProxyType(dict(value))

    @field_serializer("budgets")
    def _as_dict(self, value: Mapping[ServiceId, Budget]) -> dict[ServiceId, Budget]:
        return dict(value)


def claude_auth_mode(kind: ProviderKind | None) -> ClaudeAuthMode | None:
    """The Claude sign-in mode a provider kind stands for; None for other providers."""
    modes: Mapping[str, ClaudeAuthMode] = {
        "claude_subscription_token": "subscription_token",
        "claude_existing_login": "existing_login",
        "claude_api_key": "api_key",
    }
    return modes.get(kind) if kind is not None else None


# --- Personal details (ADR-106 items 3 and 4; spec 2.9 rule 13) ---------------------------------

# Control characters, line separators and the bidi override, isolate and mark characters, which
# can make a name or link read differently from how it is stored. U+200B (zero-width space) is fine.
_BIDI = r"\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c"
_CONTROL: Final = re.compile(rf"[\x00-\x1f\x7f-\x9f\u2028\u2029{_BIDI}]")
_PHOTO_NAME: Final = re.compile(r"photo-([A-Za-z0-9_-]{1,64})\.(?:jpg|png)")
_EMAIL_PART: Final = rf"[^\s@<>,;\"\\\x00-\x1f\x7f-\x9f{_BIDI}]+"
_EMAIL: Final = re.compile(rf"({_EMAIL_PART})@({_EMAIL_PART})")
_PHONE: Final = re.compile(r"[0-9 +().-]+")
_MAX_NAME = 100
_MAX_EMAIL = 254
_MAX_PHONE = 30
_MAX_LINKS = 3
_MAX_LINK = 300
_LINK_MESSAGE = "Write each link as an http or https address."


def has_control_characters(text: str) -> bool:
    """Whether ``text`` holds a control character (a newline, tab, NUL, ...) that would break
    a one-line value such as a name or a heading, or a bidi override, isolate or mark."""
    return _CONTROL.search(text) is not None


class PhotoRef(_Setting):
    """The photo in the data folder's ``assets/``: ``photo-<id>.jpg|png`` only, so nothing
    that names another path is accepted."""

    stored_name: str

    @field_validator("stored_name", mode="after")
    @classmethod
    def _generated_name(cls, value: str) -> str:
        match = _PHOTO_NAME.fullmatch(value)
        if match is None or not is_safe_id(match.group(1)):
            raise ValueError("Not a stored photo name.")
        return value


def _blank_is_none(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _one_line(value: str | None) -> str | None:
    if value is not None and has_control_characters(value):
        raise ValueError("Use one line of text, without control characters.")
    return value


def _valid_email(value: str | None) -> str | None:
    if value is None:
        return None
    match = _EMAIL.fullmatch(value)
    domain = match.group(2) if match else ""
    dotted = "." in domain.strip(".") and not domain.startswith(".") and not domain.endswith(".")
    if match is None or not dotted or ".." in domain:
        raise ValueError("Write the email as name@example.com.")
    return value


def _valid_phone(value: str | None) -> str | None:
    if value is None:
        return None
    digits = sum(char in "0123456789" for char in value)
    if _PHONE.fullmatch(value) is None or not 6 <= digits <= 15:
        raise ValueError("Write 6 to 15 digits; spaces, + - ( ) and . are fine.")
    return value


def _valid_link(link: str) -> str:
    cleaned = link.strip()
    try:
        parts = urlsplit(cleaned)
        host = parts.hostname
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise ValueError(_LINK_MESSAGE) from None
    bad = (
        parts.scheme not in ("http", "https")
        or not host
        or port == 0
        or "@" in parts.netloc
        or len(cleaned) > _MAX_LINK
        or any(char.isspace() for char in cleaned)
        or has_control_characters(cleaned)
    )
    if bad:
        raise ValueError(_LINK_MESSAGE)
    return cleaned


class PersonalDetails(_Setting):
    """The contact details on the CV header and letter sign-off. All optional so a half-filled
    form can be saved. There is no date of birth, nationality or marital status (ADR-106
    item 3): unknown fields are refused."""

    name: Annotated[str, StringConstraints(max_length=_MAX_NAME)] | None = None
    email: Annotated[str, StringConstraints(max_length=_MAX_EMAIL)] | None = None
    phone: Annotated[str, StringConstraints(max_length=_MAX_PHONE)] | None = None
    location: Annotated[str, StringConstraints(max_length=_MAX_NAME)] | None = None
    links: Annotated[tuple[str, ...], Field(max_length=_MAX_LINKS)] = ()
    photo: PhotoRef | None = None

    @field_validator("name", "email", "phone", "location", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return _blank_is_none(value)

    @field_validator("name", "location", mode="after")
    @classmethod
    def _single_line(cls, value: str | None) -> str | None:
        return _one_line(value)

    @field_validator("email", mode="after")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return _valid_email(value)

    @field_validator("phone", mode="after")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _valid_phone(value)

    @field_validator("links", mode="after")
    @classmethod
    def _links(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_valid_link(link) for link in value)

    @property
    def can_export(self) -> bool:
        """The CV header and the letter sign-off need a name."""
        return self.name is not None


class ShortcutSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    enabled: bool = True


class SessionSettings(BaseModel):
    """Session defaults; persistence uses the existing versioned setting service."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    frequency: Literal["never", "weekly", "twice_weekly", "after_application"] = "weekly"
    length: Literal[3, 5] = 3
    ask_later_default: Literal["next_session", "week", "month"] = "week"
    paused: bool = False

    @field_validator("length", mode="before")
    @classmethod
    def _whole_length(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Choose 3 or 5 questions.")
        return value


class AppearanceSettings(BaseModel):
    """How the pages look (M9.2; ADR-131 item 6; ADR-042 follow-up; TD-085 density only).
    ``theme`` follows the device (``system``) or is ``light`` or ``dark``; ``density`` is the
    spacing of the pages. Strict: a stored value of the wrong type is not coerced."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    theme: Literal["system", "light", "dark"] = "system"
    density: Literal["comfortable", "compact"] = "comfortable"


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
TASK_RUNNER = SettingSpec("task_runner", TaskRunnerSettings, TaskRunnerSettings())
USAGE_BUDGETS = SettingSpec("usage_budgets", UsageBudgets, UsageBudgets())
SHORTCUTS = SettingSpec("shortcuts", ShortcutSettings, ShortcutSettings())
PERSONAL_DETAILS = SettingSpec("personal_details", PersonalDetails, PersonalDetails())
SESSIONS = SettingSpec("sessions", SessionSettings, SessionSettings())
APPEARANCE = SettingSpec("appearance", AppearanceSettings, AppearanceSettings())
