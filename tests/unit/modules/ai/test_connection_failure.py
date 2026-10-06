"""``ConnectionResult.failure`` kinds (ADR-063 follow-up): ``connection_failure`` maps each
domain error a provider's connection test reports to one ``ConnectionFailure`` value, covers
every value, and answers None for anything else."""

from typing import get_args

import pytest

from app.domain.errors import AppError, VersionConflict
from app.modules import ai
from app.modules.ai import ConnectionFailure, connection_failure
from app.modules.ai.providers import claude_sdk


def test_map_covers_exactly_the_failure_kinds() -> None:
    assert sorted(ai._FAILURE_BY_ERROR.values()) == sorted(get_args(ConnectionFailure))


@pytest.mark.parametrize("error", claude_sdk._CONNECTION_FAILURES, ids=lambda e: e.__name__)
def test_every_claude_connection_failure_has_a_kind(error: type[AppError]) -> None:
    assert connection_failure(error()) is not None


def test_unknown_error_has_no_kind() -> None:
    class FictionalError(AppError):
        pass

    assert connection_failure(FictionalError()) is None
    assert connection_failure(VersionConflict()) is None
