"""A fact made from an interview debrief says so in its history (M8.3 part A; ADR-124 item 9).

``FactHistory.of(fact_id)`` labels each version by its source. The new source ``debrief``
(``app.services.facts.SOURCE_DEBRIEF``) reads "From your interview debrief"; the other labels
(including the M8.2 "From your practice answer") are unchanged and an unknown source still reads
"Saved". Drafts are made through the fact service's own draft path. Fictional data.
"""

from pathlib import Path

import pytest

from app.services.facts import DraftInput, DraftOrigin
from tests.facts_page import facts_app

pytestmark = pytest.mark.integration

WORDING = "Introduced a weekly review of late routes with a team of 3."


def source_label(facts, source: str) -> str:
    origin = DraftOrigin(source=source, provider="fake", model="fake-fast")
    (draft,) = facts.bank.facts.create_drafts(
        origin, [DraftInput(text=WORDING, original_answer=WORDING)]
    )
    history = facts.run.app.state.fact_history.of(draft.id)
    (entry,) = history.versions
    return str(entry.source)


def test_debrief_source_has_its_own_history_label(tmp_path: Path) -> None:
    from app.services import facts as facts_module

    with facts_app(tmp_path) as facts:
        assert source_label(facts, "debrief") == "From your interview debrief"
        assert facts_module.SOURCE_DEBRIEF == "debrief"


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("practice", "From your practice answer"),
        ("cv_import", "From a CV import"),
        ("refine", "Refined with AI, accepted by you"),
    ],
)
def test_the_other_history_labels_are_unchanged(tmp_path: Path, source: str, label: str) -> None:
    with facts_app(tmp_path) as facts:
        assert source_label(facts, source) == label
