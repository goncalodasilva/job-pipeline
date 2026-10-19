"""Possible duplicates on the Drafts tab (M3.7; ADR-089 item 7, ADR-094 and its 2026-10-16
update: a draft merges only into a confirmed fact).

The contract is in ``tests/facts_page.py`` under "M3.7". ``GET /facts/duplicates`` lists each
pair of a draft and a confirmed fact whose wordings are alike by the mechanical similarity of
``app.domain.similarity`` (no AI call, no task), each opening the M3.6 merge screen or
dismissed and remembered in ``fact_pair_dismissals``. A finished setup with the fake provider
saved; a real SQLite database under ``tmp_path``; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.fact_bank import typed
from tests.facts_page import (
    COMPARE_AND_MERGE,
    DISMISS_URL,
    DUPLICATES_ID,
    DUPLICATES_URL,
    FIND_DUPLICATES,
    FIND_DUPLICATES_ID,
    MERGE_COMPARE_ID,
    MERGE_DRAFT_ID,
    MERGE_TARGET_ID,
    NO_DUPLICATES,
    NOT_A_DUPLICATE,
    SAID_DISMISSED,
    FactsApp,
    announcement,
    button,
    by_id,
    duplicate_id,
    facts_app,
    form_values,
    href_parts,
    links,
    page_of,
    posts_to,
    required,
)
from tests.integration.test_setup_redirect import BASE_URL, Element, outline

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
ROUTES_OTHER_CASE = "planned 60 DELIVERY routes a week with a team of 3"
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
INVOICES_LONGER = "Cut the invoice processing time by 35% across 4 warehouses."
NEWSLETTER = "Edited the monthly staff newsletter."
STOCKTAKE = "Ran the quarterly stocktake."
ENGLISH = "Fluent English, used daily with suppliers."
TRAINED = "Trained new planners on the routing tool."
IMPORT_ID = "imp-dup"
PAIR_CHANGED = "That pair has changed. The list is updated."


# --- helpers -----------------------------------------------------------------------------------


def region(facts: FactsApp) -> list[Element]:
    elements = facts.fragment(DUPLICATES_URL)
    required(elements, DUPLICATES_ID)
    return elements


def pairs_shown(elements: list[Element]) -> set[tuple[str, str]]:
    """``(draft id, fact id)`` of every listed pair, read from its "Compare and merge" link."""
    found: set[tuple[str, str]] = set()
    inside = required(elements, DUPLICATES_ID)
    for link in links(elements, COMPARE_AND_MERGE, inside):
        path, query, _ = href_parts(link)
        parts = path.strip("/").split("/")
        assert len(parts) == 3 and parts[0] == "facts" and parts[2] == "merge", path
        found.add((parts[1], query["target"]))
    return found


def dismissals(facts: FactsApp) -> list[tuple[Any, Any]]:
    rows = facts.run.rows("SELECT fact_a_id, fact_b_id FROM fact_pair_dismissals ORDER BY rowid")
    return [(row["fact_a_id"], row["fact_b_id"]) for row in rows]


def version(facts: FactsApp, fact_id: str) -> int:
    return int(facts.bank.facts.get(fact_id).version)


def seed_bank(facts: FactsApp) -> dict[str, str]:
    """Confirmed facts and drafts, some alike; returns their ids by name."""
    routes = typed(facts.bank, ROUTES)
    invoices = typed(facts.bank, INVOICES)
    newsletter = typed(facts.bank, NEWSLETTER)
    english = typed(facts.bank, ENGLISH)
    english_again = typed(facts.bank, ENGLISH)
    facts.add_import(IMPORT_ID)
    made = facts.drafts(
        IMPORT_ID,
        ROUTES_OTHER_CASE,  # alike to a confirmed fact: suggested
        INVOICES_LONGER,  # alike to a confirmed fact: suggested
        ROUTES,  # alike, but discarded below: never suggested
        NEWSLETTER,  # alike to a fact archived below: never suggested
        STOCKTAKE,  # alike only to another draft: no merge target
        STOCKTAKE,
        TRAINED,  # alike to nothing
    )
    ids = {
        "routes": routes.id,
        "invoices": invoices.id,
        "newsletter": newsletter.id,
        "english": english.id,
        "english_again": english_again.id,
        "routes_draft": made[0].id,
        "invoices_draft": made[1].id,
        "discarded": made[2].id,
        "newsletter_draft": made[3].id,
        "stocktake_a": made[4].id,
        "stocktake_b": made[5].id,
        "trained": made[6].id,
    }
    facts.bank.facts.discard(ids["discarded"], expected_version=version(facts, ids["discarded"]))
    facts.bank.facts.archive(ids["newsletter"], expected_version=version(facts, ids["newsletter"]))
    return ids


# --- tests -------------------------------------------------------------------------------------


def test_drafts_tab_offers_find_possible_duplicates(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        elements = facts.tab("drafts")
        found = required(elements, FIND_DUPLICATES_ID)
        assert found.tag == "button" and found.text == FIND_DUPLICATES
        assert found.attrs.get("hx-get") == DUPLICATES_URL, found.attrs
        assert found.attrs.get("hx-target") == f"#{DUPLICATES_ID}", found.attrs
        required(elements, DUPLICATES_ID)


def test_no_pairs_says_so(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        typed(facts.bank, ROUTES)
        facts.add_import(IMPORT_ID)
        facts.drafts(IMPORT_ID, TRAINED)
        elements = region(facts)
        assert pairs_shown(elements) == set()
        assert NO_DUPLICATES in required(elements, DUPLICATES_ID).text


def test_suggestions_skip_discarded_and_dismissed_pairs(tmp_path: Path) -> None:
    """Only a draft and a confirmed fact make a pair (the merge screen's rule); discarded
    drafts, archived facts, draft-draft and fact-fact pairs are never listed; a dismissed pair
    is remembered. Nothing calls the AI provider or starts a task."""
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        routes_pair = (ids["routes_draft"], ids["routes"])
        invoices_pair = (ids["invoices_draft"], ids["invoices"])

        elements = region(facts)
        assert pairs_shown(elements) == {routes_pair, invoices_pair}
        block = required(elements, duplicate_id(*invoices_pair))
        assert INVOICES in block.text and INVOICES_LONGER in block.text, "both wordings shown"

        # Dismiss through the pair's own form, as the page sends it.
        dismiss = button(elements, block, NOT_A_DUPLICATE)
        form = dismiss.parent
        while form is not None and form.tag != "form":
            form = form.parent
        assert form is not None and posts_to(form) == DISMISS_URL
        sent = form_values(elements, form)
        assert (sent["draft_id"], sent["fact_id"]) == invoices_pair
        before = {key: facts.status(value) for key, value in ids.items()}

        response = facts.post(
            DISMISS_URL, {"draft_id": ids["invoices_draft"], "fact_id": ids["invoices"]}
        )

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_DISMISSED
        answered = outline(response.text)
        assert pairs_shown(answered) == {routes_pair}
        assert by_id(answered, duplicate_id(*invoices_pair)) is None
        assert dismissals(facts) == [tuple(sorted(invoices_pair))], "stored in sorted order"
        assert {key: facts.status(value) for key, value in ids.items()} == before

        # Remembered: asking again leaves it out; dismissing again stores nothing new.
        assert pairs_shown(region(facts)) == {routes_pair}
        facts.post(DISMISS_URL, {"draft_id": ids["invoices_draft"], "fact_id": ids["invoices"]})
        assert dismissals(facts) == [tuple(sorted(invoices_pair))]

        # A draft discarded later drops out too.
        draft = ids["routes_draft"]
        facts.bank.facts.discard(draft, expected_version=version(facts, draft))
        assert pairs_shown(region(facts)) == set()

        assert facts.run.spy.requests == [], "no AI call"
        assert facts.run.tasks() == []


def test_duplicate_pair_opens_the_merge_screen(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        elements = region(facts)
        block = required(elements, duplicate_id(ids["routes_draft"], ids["routes"]))
        (link,) = links(elements, COMPARE_AND_MERGE, block)
        path, query, _ = href_parts(link)
        assert (path, query) == (f"/facts/{ids['routes_draft']}/merge", {"target": ids["routes"]})

        merge = page_of(facts, path, **query)
        required(merge, MERGE_COMPARE_ID)
        assert ROUTES_OTHER_CASE in required(merge, MERGE_DRAFT_ID).text
        assert ROUTES in required(merge, MERGE_TARGET_ID).text


def test_dismiss_refuses_what_is_not_a_listed_pair(tmp_path: Path) -> None:
    """Only a draft and a confirmed fact can be dismissed as a pair; unknown ids are a fixed
    404 that repeats nothing; nothing is stored."""
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        cases = [
            {"draft_id": "no-such-fact", "fact_id": ids["routes"]},
            {"draft_id": ids["routes_draft"], "fact_id": "no-such-fact"},
        ]
        for data in cases:
            response = facts.post(DISMISS_URL, data)
            assert response.status_code == 404, (data, response.status_code)
            assert "no-such-fact" not in response.text
        swapped = facts.post(DISMISS_URL, {"draft_id": ids["routes"], "fact_id": ids["english"]})
        assert swapped.status_code in (400, 409, 422), swapped.status_code
        # htmx swaps a 409 into the region: the answer is the region as it is now, saying
        # so, with focus on that line; never text that would replace the region.
        assert swapped.status_code == 409
        assert announcement(swapped) == PAIR_CHANGED
        answered = outline(swapped.text)
        required(answered, DUPLICATES_ID)
        status = required(answered, "fact-duplicates-status")
        assert status.text == PAIR_CHANGED and "data-autofocus" in status.attrs
        assert pairs_shown(answered) == {
            (ids["routes_draft"], ids["routes"]),
            (ids["invoices_draft"], ids["invoices"]),
        }
        assert dismissals(facts) == []


def test_duplicate_routes_need_token_and_origin(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        client = facts.run.client
        assert client.get(DUPLICATES_URL).status_code == 403, "a fragment needs the token"
        data = {"draft_id": ids["routes_draft"], "fact_id": ids["routes"]}
        assert client.post(DISMISS_URL, data=data, headers={"origin": BASE_URL}).status_code == 403
        bad = client.post(
            DISMISS_URL, data=data, headers=facts.run.run.action(origin="http://evil.example")
        )
        assert bad.status_code == 403
        assert dismissals(facts) == []
