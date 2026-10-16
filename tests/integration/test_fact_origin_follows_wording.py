"""The practice badge follows the current wording (user decision 2026-10-16, option B).

``FactView.origin_provider`` and ``origin_model``: the current version's provider when one
made it; for the user's own wording, the latest earlier version a provider made, as long as at
least half of the current wording's words occur in it (``still_reads_as``); else None. The
history keeps marking each version the fake made. Real SQLite in a temp folder, the frozen
clock, the fake provider, no network; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest

from app.services.facts import FactInput
from tests.fact_bank import typed
from tests.facts_page import (
    EDITOR_DRAFT_ID,
    HISTORY_ID,
    LIBRARY_URL,
    PRACTICE_BADGE,
    FactsApp,
    edit_url,
    fact_id_attr,
    facts_app,
    inside,
    page_of,
    required,
    says,
    version_id_attr,
)
from tests.integration.test_setup_redirect import Element

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
ONE_WORD = "Planned 80 delivery routes a week with a team of 3."
STOCKTAKE = "Ran the yearly stocktake for the shop"
TARGET = "Organised the depot's weekly safety briefing"
REWRITTEN = "Trained four new colleagues on the warehouse scanner"
IMPORT_A = "imp-a"
FAKE = "fake"


def edit(facts: FactsApp, view: Any, text: str) -> Any:
    current = facts.bank.facts.get(view.id)
    return facts.bank.facts.edit(
        view.id, FactInput(text=text, skills=current.skills), expected_version=current.version
    )


def merge_taking_the_draft(facts: FactsApp, draft: Any, target_id: str) -> Any:
    target = facts.bank.facts.get(target_id)
    return facts.bank.facts.merge(
        draft.id,
        target.id,
        draft.content,
        expected_draft_version=draft.version,
        expected_target_version=target.version,
    )


def badge_outside_history(elements: list[Element]) -> bool:
    """Whether the fact's page shows the practice badge anywhere but in its history."""
    history = required(elements, HISTORY_ID)
    main = required(elements, "main")
    return any(
        e.tag == "li" and PRACTICE_BADGE in e.text and not e.within(history)
        for e in inside(elements, main)
    )


def test_practice_draft_lightly_edited_still_reports_fake(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        assert draft.origin_provider == FAKE

        edited = edit(facts, draft, ONE_WORD)

        assert edited.provider is None, "the current wording is the user's"
        assert edited.origin_provider == FAKE
        elements = page_of(facts, edit_url(draft.id))
        assert says(elements, PRACTICE_BADGE, required(elements, EDITOR_DRAFT_ID))


def test_fully_rewritten_merged_practice_wording_reports_none(tmp_path: Path) -> None:
    """A typed fact took a practice draft's wording by merge, then the user rewrote it: no
    badge in the Library or on its page, while its history still marks the merged version."""
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, TARGET)
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, STOCKTAKE)
        merged = merge_taking_the_draft(facts, draft, target.id)
        assert merged.origin_provider == FAKE

        rewritten = edit(facts, target, REWRITTEN)

        assert (rewritten.origin_provider, rewritten.origin_model) == (None, None)
        assert facts.bank.facts.get(target.id).origin_provider is None
        for elements in (facts.tab("library"), facts.fragment(LIBRARY_URL)):
            item = required(elements, fact_id_attr(target.id))
            assert not says(elements, PRACTICE_BADGE, item)
        page = page_of(facts, edit_url(target.id))
        assert not badge_outside_history(page)
        assert says(page, PRACTICE_BADGE, required(page, version_id_attr(2))), "the merge"
        assert not says(page, PRACTICE_BADGE, required(page, version_id_attr(3)))
        assert not says(page, PRACTICE_BADGE, required(page, version_id_attr(1)))


@pytest.mark.parametrize(
    ("rewrite", "origin"),
    [
        # Of "planned delivery schedules monthly", "planned" and "delivery" are the draft's.
        pytest.param("Planned delivery schedules monthly", FAKE, id="exactly-half"),
        pytest.param("Planned freight schedules monthly", None, id="below-half"),
    ],
)
def test_rewrite_keeping_half_the_words_still_reports_fake(
    tmp_path: Path, rewrite: str, origin: str | None
) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, "Planned delivery routes weekly")

        assert edit(facts, draft, rewrite).origin_provider == origin


def test_typed_fact_reports_none(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        fact = typed(facts.bank, TARGET)
        assert fact.origin_provider is None
        assert edit(facts, fact, TARGET + " each Monday").origin_provider is None
        library = facts.tab("library")
        assert not says(library, PRACTICE_BADGE, required(library, fact_id_attr(fact.id)))


def test_later_merge_of_practice_wording_reports_fake_again(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, TARGET)
        facts.add_import(IMPORT_A)
        first, second = facts.drafts(IMPORT_A, STOCKTAKE, ROUTES)
        merge_taking_the_draft(facts, first, target.id)
        assert edit(facts, target, REWRITTEN).origin_provider is None

        merged = merge_taking_the_draft(facts, second, target.id)

        assert (merged.content.text, merged.origin_provider) == (ROUTES, FAKE)
        library = facts.tab("library")
        assert says(library, PRACTICE_BADGE, required(library, fact_id_attr(target.id)))
