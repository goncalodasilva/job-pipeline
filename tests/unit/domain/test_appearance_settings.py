"""The Appearance setting (M9.2 part B; ADR-131 item 6; ADR-042 follow-up; TD-085 density only).

``AppearanceSettings`` is a frozen, strict model that refuses unknown fields: ``theme`` is
``system``, ``light`` or ``dark`` (default ``system``) and ``density`` is ``comfortable`` or
``compact`` (default ``comfortable``). ``APPEARANCE`` is its ``SettingSpec`` under the key
``appearance``. Every value is fictional.
"""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.domain.settings import SettingSpec


def model() -> Any:
    from app.domain.settings import AppearanceSettings

    return AppearanceSettings


def test_appearance_defaults_follow_the_device_and_are_comfortable() -> None:
    settings = model()()
    assert (settings.theme, settings.density) == ("system", "comfortable")


def test_appearance_spec_is_the_appearance_key_with_defaults() -> None:
    from app.domain.settings import APPEARANCE

    assert isinstance(APPEARANCE, SettingSpec)
    assert APPEARANCE.key == "appearance"
    assert APPEARANCE.model is model()
    assert APPEARANCE.default == model()()
    assert issubclass(APPEARANCE.model, BaseModel)


@pytest.mark.parametrize("theme", ["system", "light", "dark"])
@pytest.mark.parametrize("density", ["comfortable", "compact"])
def test_appearance_accepts_every_listed_value(theme: str, density: str) -> None:
    settings = model()(theme=theme, density=density)
    assert (settings.theme, settings.density) == (theme, density)
    # Stored as JSON by SettingsService and read back unchanged.
    assert model().model_validate_json(settings.model_dump_json()) == settings


@pytest.mark.parametrize(
    "bad",
    [
        {"theme": "Dark"},
        {"theme": " dark"},
        {"theme": "auto"},
        {"theme": "high-contrast"},
        {"theme": ""},
        {"theme": None},
        {"theme": 1},
        {"theme": True},
        {"density": "Compact"},
        {"density": "cozy"},
        {"density": "dense"},
        {"density": ""},
        {"density": None},
        {"density": 24},
    ],
    ids=lambda bad: "-".join(f"{k}={v!r}" for k, v in bad.items()),
)
def test_appearance_rejects_values_outside_the_lists(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model()(**bad)
    with pytest.raises(ValidationError):
        model().model_validate(bad)


@pytest.mark.parametrize(
    "extra",
    [{"sidebar": "collapsed"}, {"font_size": 20}, {"color": "#000000"}, {"Theme": "dark"}],
    ids=lambda extra: next(iter(extra)),
)
def test_appearance_rejects_unknown_fields(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model().model_validate({"theme": "dark", "density": "compact", **extra})


def test_appearance_is_strict_about_types_in_the_stored_json() -> None:
    # A stored value of the wrong type is not coerced: the service falls back to the default.
    with pytest.raises(ValidationError):
        model().model_validate_json('{"theme": 1, "density": "compact"}')
    with pytest.raises(ValidationError):
        model().model_validate_json('{"theme": "dark", "density": ["compact"]}')


def test_appearance_is_frozen() -> None:
    settings = model()(theme="dark")
    with pytest.raises(ValidationError):
        settings.theme = "light"
    assert settings.theme == "dark"


def test_appearance_holds_only_the_two_fields() -> None:
    assert set(model().model_fields) == {"theme", "density"}
