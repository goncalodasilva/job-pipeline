"""Step 6 of the setup guide: recording failures and optional step wording (M1.19 review).

``DoneStep`` (see ``test_done_step.py`` for the interface): when ``mark_done`` raises, finishing
answers the plain ``MSG_SAVE_FAILED`` form error with no redirect, and the log names the error
type only. In the summary an optional step that is neither done nor skipped reads "Optional,
not set up"; a required one reads "Not done yet".
"""

import logging
import sqlite3
from collections.abc import Mapping

import pytest

from app.onboarding.detection import StepStatus
from app.onboarding.registry import (
    DONE,
    PREFERENCES,
    PROVIDER,
    WELCOME,
    StepDefinition,
    StepRegistry,
    m1_registry,
)
from app.onboarding.steps.done import MSG_SAVE_FAILED, DoneStep

PRIVATE_PATH = "/Users/fictional-person/private/app.db"
SAMPLE_OPTIONAL = StepDefinition(
    "sample_optional", "Sample optional step", False, 45, "fragments/setup/_done.html"
)
ALL_REQUIRED_DONE: Mapping[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
    "preferences": "done",
}


def failing_mark() -> None:
    raise sqlite3.OperationalError(f"database is locked: {PRIVATE_PATH}")


def test_mark_done_failure_answers_plain_error_without_redirect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = DoneStep(m1_registry(), statuses=lambda: ALL_REQUIRED_DONE, mark_done=failing_mark)
    with caplog.at_level(logging.INFO):
        result = step.submit({"then": ["add_job"]})
    assert result.ok is False
    assert dict(result.errors) == {"form": MSG_SAVE_FAILED}
    assert result.redirect is None
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "OperationalError" in text
    assert PRIVATE_PATH not in text and "locked" not in text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(None, "Optional, not set up", id="never-visited"),
        pytest.param("pending", "Optional, not set up", id="pending"),
        pytest.param("skipped", "Skipped", id="skipped"),
        pytest.param("done", "Done", id="done"),
    ],
)
def test_optional_step_wording(status: StepStatus | None, expected: str) -> None:
    registry = StepRegistry((WELCOME, PROVIDER, SAMPLE_OPTIONAL, PREFERENCES, DONE))
    statuses: dict[str, StepStatus] = {"welcome": "done"}
    if status is not None:
        statuses[SAMPLE_OPTIONAL.id] = status
    step = DoneStep(registry, statuses=lambda: statuses, mark_done=lambda: None)
    rows = {row["id"]: row["status_text"] for row in step.context()["rows"]}  # type: ignore[attr-defined]
    assert rows == {
        "welcome": "Done",
        "provider": "Not done yet",
        "sample_optional": expected,
        "preferences": "Not done yet",
    }
