"""Typed ``TaskRunnerSettings`` (M2.8; spec 5 M2; UI in M2.22): how many background tasks run
at once, 1 to 3, default 1, stored under its own settings key like the other typed settings."""

from typing import Any

import pytest
from pydantic import ValidationError


def _model() -> Any:
    from app.domain.settings import TaskRunnerSettings

    return TaskRunnerSettings


def test_task_runner_settings_default_is_one_at_a_time() -> None:
    assert _model()().max_concurrent == 1


@pytest.mark.parametrize("value", [1, 2, 3])
def test_task_runner_settings_accepts_one_to_three(value: int) -> None:
    assert _model()(max_concurrent=value).max_concurrent == value


@pytest.mark.parametrize("value", [0, 4, -1, 2.5, None, "many"])
def test_task_runner_settings_rejects_out_of_range(value: object) -> None:
    with pytest.raises(ValidationError):
        _model()(max_concurrent=value)


def test_task_runner_settings_frozen_and_forbid_extra_fields() -> None:
    model = _model()
    settings = model()
    with pytest.raises(ValidationError):
        settings.max_concurrent = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        model(max_concurrent=1, poll_seconds=5)


def test_task_runner_setting_spec() -> None:
    from app.domain.settings import TASK_RUNNER, SettingSpec

    assert isinstance(TASK_RUNNER, SettingSpec)
    assert TASK_RUNNER.key == "task_runner"
    assert TASK_RUNNER.model is _model()
    assert TASK_RUNNER.default == _model()()


def test_task_runner_settings_round_trip_json() -> None:
    model = _model()
    value = model(max_concurrent=3)
    assert model.model_validate_json(value.model_dump_json()) == value
