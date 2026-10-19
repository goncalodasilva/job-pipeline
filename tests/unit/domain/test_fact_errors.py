"""The fact bank's user-facing errors (M3.3; spec 2.11).

Four classes join ``app.domain.errors``, each an ``AppError`` with a stable ``kind`` and plain,
blame-free copy:

- ``FactNotFound``: the id names no fact (or role, or skill).
- ``FactTransitionRefused``: a status change the lifecycle doesn't allow, such as confirming a
  confirmed fact, archiving a draft or merging into an archived fact.
- ``FactInputInvalid(field, message)``: what the user typed can't be saved; ``field`` names the
  form field so M3.6 can tie the message to it.
- ``SkillAliasTaken``: the alias already names another skill.

Imports are inside the tests so each one fails on its own while a class is missing.
"""

import pytest

FIXED = [
    pytest.param(
        "FactNotFound",
        "This item wasn't found. Reload the page and try again.",
        id="not-found",
    ),
    pytest.param(
        "FactTransitionRefused",
        "This can't be done to this fact in its current state. Reload to see the latest version.",
        id="transition-refused",
    ),
    pytest.param(
        "SkillAliasTaken",
        "Another skill already uses this name. Choose a different name.",
        id="alias-taken",
    ),
]


@pytest.mark.parametrize(("name", "message"), FIXED)
def test_fact_errors_have_stable_kind_and_friendly_message(name: str, message: str) -> None:
    from app.domain import errors

    cls = getattr(errors, name)
    assert issubclass(cls, errors.AppError)
    assert cls.kind == name
    error = cls()
    assert error.user_message == message
    # The detail is for logs only.
    assert cls(detail="fact f-0001 is archived").user_message == message


def test_invalid_input_names_the_field_and_carries_its_own_message() -> None:
    from app.domain.errors import AppError, FactInputInvalid

    error = FactInputInvalid("start_date", "Use a year, a month or a full date, such as 2024-03.")
    assert isinstance(error, AppError)
    assert FactInputInvalid.kind == "FactInputInvalid"
    assert error.field == "start_date"
    assert error.user_message == "Use a year, a month or a full date, such as 2024-03."


def test_fact_errors_are_not_version_conflicts() -> None:
    from app.domain.errors import (
        FactInputInvalid,
        FactNotFound,
        FactTransitionRefused,
        SkillAliasTaken,
        VersionConflict,
    )

    for cls in (FactNotFound, FactTransitionRefused, FactInputInvalid, SkillAliasTaken):
        assert not issubclass(cls, VersionConflict)
        assert not issubclass(VersionConflict, cls)
