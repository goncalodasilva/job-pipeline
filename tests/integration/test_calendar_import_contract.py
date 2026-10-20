"""The calendar SDK belongs only to the ICS adapter, including submodule imports."""

from pathlib import Path

import pytest

from tests.integration.test_import_contracts import _contracts_with_status, _lint_copy_with

pytestmark = pytest.mark.integration
CONTRACT = "icalendar only in the ICS provider (spec 2.3 rule 3)"


@pytest.mark.parametrize(
    "destination",
    [
        "app/services/_violation.py",
        "app/modules/calendar/_violation.py",
        "app/modules/ai/providers/_violation.py",
    ],
)
def test_icalendar_outside_calendar_providers_fails(tmp_path: Path, destination: str) -> None:
    code, output = _lint_copy_with(tmp_path, {destination: "from icalendar import Calendar\n"})
    assert code != 0, output
    assert CONTRACT in _contracts_with_status(output, "BROKEN"), output
