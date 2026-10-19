"""``still_reads_as``: whether the user's wording still reads as a version a provider made
(the practice badge follows the current wording; user decision 2026-10-16). At least half of
the current wording's word tokens, all parts joined, normalised and without case, occur in the
earlier wording. All text is fictional."""

import pytest

from app.domain.facts import FactContent, still_reads_as

EARLIER = FactContent(text="Planned delivery routes weekly")


@pytest.mark.parametrize(
    ("current", "reads_as"),
    [
        pytest.param("Planned delivery routes weekly", True, id="same"),
        pytest.param("Planned delivery routes monthly", True, id="one-word-changed"),
        pytest.param("Planned delivery schedules monthly", True, id="exactly-half"),
        pytest.param("Planned freight schedules monthly", False, id="below-half"),
        pytest.param("Ran the yearly stocktake", False, id="rewritten"),
        pytest.param("PLANNED   Delivery routes\u200b weekly", True, id="case-and-normaliser"),
        pytest.param("   ", False, id="no-words"),
    ],
)
def test_still_reads_as(current: str, reads_as: bool) -> None:
    assert still_reads_as(FactContent(text=current), EARLIER) is reads_as


def test_story_parts_are_joined() -> None:
    earlier = FactContent(situation="Invoices took nine days.", action="Removed approvals.")
    current = FactContent(text="Removed approvals; invoices took six days.")
    # removed, approvals, invoices, took, days of 6 words: 5 occur.
    assert still_reads_as(current, earlier)
    assert not still_reads_as(FactContent(result="Cut freight costs sharply."), earlier)
