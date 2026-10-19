"""Refine with AI: each kind of invented claim on its own, and a story's parts (M3.8 review
fixes; AC-M3.5; spec 3.1 item 5; ADR-089 item 5; add-ai-task step 6).

Each ``adversarial_*`` fixture of ``tests/fixtures/ai/refine_fact/`` adds exactly one claim
to the fictional fact "Cut invoice processing time by 35% across 4 warehouses." (role
"Operations Analyst at Example Widgets Ltd"); the honesty check of the real service must
flag exactly that claim. The ``story`` fixture suggests a situation, an action and a result,
each with one invented claim. Real SQLite under ``tmp_path``, the real runner, the fake
provider behind the spy; no network; every value is fictional.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.facts import FactType
from tests.cv_import_harness import said
from tests.refine_harness import ACCEPTED, TARGET, claim_texts, refine_path, refining

pytestmark = pytest.mark.integration

STORY = {
    "situation": "Invoice processing was slow across 4 warehouses.",
    "action": "Rebuilt the approval queue.",
    "result": "Cut processing time by 35%.",
}
SUGGESTED_STORY = {
    "situation": "Invoice processing was slow across 14 warehouses.",
    "action": "Rebuilt the approval queue with Globex Corporation.",
    "result": "Cut processing time by 35% in 2019.",
}


def stored_claims(raw: str | None) -> list[tuple[str, str]]:
    return [(item["kind"], item["text"]) for item in json.loads(raw or "[]")]


@pytest.mark.parametrize(
    ("fixture", "claim"),
    [
        ("adversarial_inflated_number", ("number", "14")),
        ("adversarial_invented_employer", ("name", "Globex Corporation")),
        ("adversarial_invented_skill", ("skill", "sql")),
        ("adversarial_invented_title", ("name", "Finance Director")),
        ("adversarial_invented_year", ("year", "2019")),
        ("adversarial_invented_money", ("money", "€40,000")),
    ],
)
def test_each_invented_claim_kind_is_flagged_on_its_own(
    tmp_path: Path, fixture: str, claim: tuple[str, str]
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        # SQL is on the controlled list, but no fact sent names it.
        r.typed("Handled the Brightwater Trust audit.", privacy="never_use", skills=("SQL",))
        r.answer_with(fixture)
        r.started(fact.id)
        session = r.open_session(fact.id)
        before = r.fact_tables()

        assert stored_claims(session["flagged_claims"]) == [claim]
        refused = r.accept(fact.id, text=_suggested(session))
        assert refused.status_code == 422, refused.status_code
        assert claim[1] in said(refused)
        assert r.fact_tables() == before
        assert r.current(fact.id).content.text == TARGET


def _suggested(session: dict[str, Any]) -> str:
    text = json.loads(session["suggestion"])["text"]
    assert isinstance(text, str)
    return text


def test_story_suggestion_keeps_its_parts_and_each_part_is_checked(tmp_path: Path) -> None:
    from app.services.facts import FactInput

    with refining(tmp_path) as r:
        fact = r.facts.create(FactInput(type=FactType.STORY, **STORY))
        r.answer_with("story")
        r.started(fact.id)
        session = r.open_session(fact.id)

        view = r.run.app.state.fact_refine.view(fact.id)
        assert view.suggestion.text is None
        for part, wording in SUGGESTED_STORY.items():
            assert getattr(view.suggestion, part) == wording
        flagged = claim_texts(session["flagged_claims"])
        assert flagged == ["14", "2019", "Globex Corporation"], flagged
        before = r.fact_tables()

        def accept(wording: dict[str, str], acknowledged: list[str]) -> Any:
            data: dict[str, list[str]] = {"version": [str(r.current(fact.id).version)]}
            data |= {part: [text] for part, text in wording.items()}
            data["acknowledged"] = acknowledged
            return r.post(refine_path(fact.id, "accept"), data)

        # Each part is checked on its own: a claim in any of them stops the accept.
        for part in SUGGESTED_STORY:
            only_one = {**STORY, part: SUGGESTED_STORY[part]}
            refused = accept(only_one, [])
            assert refused.status_code == 422, (part, refused.status_code)
        assert r.fact_tables() == before

        saved = accept(SUGGESTED_STORY, flagged)

        assert saved.status_code in ACCEPTED, (saved.status_code, saved.text[:300])
        newest = r.versions(fact.id)[-1]
        assert newest["text"] is None
        assert {part: newest[part] for part in SUGGESTED_STORY} == SUGGESTED_STORY
        assert set(claim_texts(r.sessions(fact.id)[-1]["acknowledged_claims"])) == set(flagged)
