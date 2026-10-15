"""The merge screen of a draft (M3.6; AC-M3.2: merge works and is versioned).

"Merge with…" on a draft opens ``GET /facts/{draft id}/merge``, a page: a search form (plain
GET, field ``q``) and the facts the draft can be folded into (confirmed ones and other drafts;
never itself, an archived or a discarded one), each with a "Compare" link back to the page with
``?target=``. The comparison shows both wordings side by side with what made them, and a form
that posts to the same address: keep the fact's wording, use the draft's, or write one. A merge
gives the target a new version and closes the draft; "Undo merge" is offered at once and for
as long as the target reads as the merge left it (``POST /facts/{draft id}/undo-merge``). The
contract's names live in ``tests/facts_page.py``. Real SQLite in a temp folder, the frozen
clock, the fake provider, no network; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.domain.errors import FactInputInvalid, FactTransitionRefused, VersionConflict
from app.domain.facts import FactContent, FactType
from app.services.facts import DraftInput, FactInput
from tests.fact_bank import current_version_row, event_types, typed, version_rows
from tests.facts_page import (
    COMPARE,
    DRAFTS_URL,
    MERGE,
    MERGE_CANDIDATES_ID,
    MERGE_CHOICE_FIELD,
    MERGE_CHOICES,
    MERGE_COMPARE_ID,
    MERGE_CONFLICT_ID,
    MERGE_DRAFT_ID,
    MERGE_FORM_ID,
    MERGE_HEADING,
    MERGE_NOT_A_DRAFT,
    MERGE_RESULT_ID,
    MERGE_SEARCH_ID,
    MERGE_TARGET_ID,
    NEEDS_CHECK_NOTE,
    NO_MATCH,
    PAGE,
    PRACTICE_BADGE,
    RELOAD,
    SAID_MERGE_UNDONE,
    SAID_MERGED,
    SEARCH,
    UNDO_MERGE,
    FactsApp,
    FormValues,
    alert,
    announcement,
    button,
    buttons,
    by_id,
    controls,
    described_by,
    edit_url,
    fact_id_attr,
    facts_app,
    form_values,
    has_label,
    headings,
    href_parts,
    inside,
    label_of,
    links,
    merge_candidate_id,
    merge_url,
    names_fact,
    only_focus,
    page_of,
    posts_to,
    required,
    says,
    send,
    undo_merge_path,
)
from tests.integration.test_budget_warning_dialog import submitted_by
from tests.integration.test_setup_redirect import Element, outline

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
ROUTES_DRAFT = "Planned 60 delivery routes each week, leading a team of 3."
SETTLED = "Planned 60 delivery routes a week, leading a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
NEWSLETTER = "Edited the monthly staff newsletter"
STOCKTAKE = "Ran the quarterly stocktake"
HOSTILE = '"><script id="injected">window.fictional=1</script><b id="bold">Planned routes</b>'
IMPORT_A = "imp-a"


# --- helpers -----------------------------------------------------------------------------------


def seed(facts: FactsApp, *, flagged: bool = False) -> dict[str, Any]:
    """A confirmed fact the user typed, and two drafts the fake provider made: one that says
    the same thing in other words, and another."""
    target = typed(facts.bank, ROUTES, skills=("Scheduling",))
    facts.add_import(IMPORT_A)
    draft, other = facts.drafts(
        IMPORT_A,
        DraftInput(
            text=ROUTES_DRAFT,
            skills=("Route planning",),
            original_answer=ROUTES,
            needs_check=flagged,
        ),
        DraftInput(text=DEGREE, original_answer=DEGREE),
    )
    return {"target": target, "draft": draft, "other": other}


def compare(facts: FactsApp, draft_id: str, target_id: str) -> tuple[list[Element], FormValues]:
    """The comparison as the page gives it, and what its form would send."""
    elements = page_of(facts, merge_url(draft_id), target=target_id)
    form = required(elements, MERGE_FORM_ID)
    assert form.tag == "form" and posts_to(form) == merge_url(draft_id)
    return elements, form_values(elements, form)


def merge(
    facts: FactsApp, draft_id: str, target_id: str, *, htmx: bool = True, **fields: str
) -> Response:
    """Post the comparison's form as the page gave it, with ``fields`` chosen or typed."""
    _, values = compare(facts, draft_id, target_id)
    return send(facts, merge_url(draft_id), {**values, **fields}, htmx=htmx)


def candidates(elements: list[Element], known: dict[str, Any]) -> set[str]:
    """Which of the seeded facts the page offers as targets."""
    listing = required(elements, MERGE_CANDIDATES_ID)
    wanted = {merge_candidate_id(view.id): name for name, view in known.items()}
    return {
        wanted[i] for e in inside(elements, listing) if (i := e.attrs.get("id") or "") in wanted
    }


def assert_result(
    facts: FactsApp, elements: list[Element], draft_id: str, target_id: str
) -> Element:
    """What a merge leaves on the page: the result, a link to the fact that holds the wording
    now, and "Undo merge" carrying both versions as they are."""
    result = required(elements, MERGE_RESULT_ID)
    assert edit_url(target_id) in [
        href_parts(a)[0] for a in inside(elements, result) if a.tag == "a"
    ], "a link to the fact the draft went into"
    undo = button(elements, result, UNDO_MERGE)
    path, values = submitted_by(elements, undo)
    assert path == undo_merge_path(draft_id)
    assert values == {
        "draft_version": str(facts.bank.facts.get(draft_id).version),
        "target_version": str(facts.bank.facts.get(target_id).version),
    }
    assert by_id(elements, MERGE_FORM_ID) is None, "nothing left to merge"
    return undo


def undo(facts: FactsApp, draft_id: str, *, htmx: bool = True) -> Response:
    draft = facts.bank.facts.get(draft_id)
    target = facts.bank.facts.get(draft.merged_into_fact_id)
    data = {"draft_version": str(draft.version), "target_version": str(target.version)}
    return facts.post(undo_merge_path(draft_id), data, htmx=htmx)


# --- finding a target --------------------------------------------------------------------------


def test_merge_page_searches_and_lists_candidates(tmp_path: Path) -> None:
    """The page lists what the draft can be merged into, confirmed facts and other drafts,
    and a search narrows it; itself, archived and discarded facts are never offered."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft = seeded["draft"]
        seeded["invoices"] = typed(facts.bank, INVOICES)
        archived = typed(facts.bank, NEWSLETTER)
        seeded["archived"] = facts.bank.facts.archive(
            archived.id, expected_version=archived.version
        )
        (gone,) = facts.drafts(IMPORT_A, STOCKTAKE)
        seeded["gone"] = facts.bank.facts.discard(gone.id, expected_version=gone.version)

        elements = page_of(facts, merge_url(draft.id))
        assert MERGE_HEADING in headings(elements, "h1")
        # The draft being merged is shown, so the user knows what they are placing.
        assert ROUTES_DRAFT in required(elements, MERGE_DRAFT_ID).text
        search = required(elements, MERGE_SEARCH_ID)
        assert search.tag == "form" and (search.attrs.get("method") or "get").lower() == "get"
        assert search.attrs.get("action") == merge_url(draft.id)
        (query,) = controls(elements, search, "q")
        assert has_label(elements, query) and button(elements, search, SEARCH)
        assert candidates(elements, seeded) == {"target", "other", "invoices"}
        assert by_id(elements, MERGE_FORM_ID) is None, "nothing is compared until one is picked"

        for name in ("target", "other", "invoices"):
            view = seeded[name]
            row = required(elements, merge_candidate_id(view.id))
            assert str(view.content.text) in row.text
            (link,) = links(elements, COMPARE, row)
            path, params, _ = href_parts(link)
            assert path == merge_url(draft.id) and params.get("target") == view.id
            assert names_fact(elements, link, str(view.content.text))
        # A draft among the candidates says it is one; the fake's work keeps its label.
        other = required(elements, merge_candidate_id(seeded["other"].id))
        assert says(elements, "Draft", other) and says(elements, PRACTICE_BADGE, other)
        assert not says(
            elements, PRACTICE_BADGE, required(elements, merge_candidate_id(seeded["target"].id))
        )

        found = page_of(facts, merge_url(draft.id), q="invoice")
        assert candidates(found, seeded) == {"invoices"}
        assert form_values(found, required(found, MERGE_SEARCH_ID))["q"] == "invoice"
        (link,) = links(found, COMPARE, required(found, MERGE_CANDIDATES_ID))
        assert href_parts(link)[1].get("target") == seeded["invoices"].id
        # Words of the draft itself, an archived or a discarded fact find nothing to merge into.
        for words in ("xylophone", "newsletter", "stocktake", "leading"):
            none = page_of(facts, merge_url(draft.id), q=words)
            assert candidates(none, seeded) == set(), words
            assert says(none, NO_MATCH, required(none, MERGE_CANDIDATES_ID)), words
        hostile = facts.run.client.get(merge_url(draft.id), params={"q": '"><b id="injected">'})
        assert hostile.status_code == 200 and by_id(outline(hostile.text), "injected") is None


def test_merge_page_only_for_a_draft(tmp_path: Path) -> None:
    """Only a draft is merged into another fact: the page of anything else says so (409) and
    offers no form."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        other = seeded["other"]
        gone = facts.bank.facts.discard(other.id, expected_version=other.version)
        before = facts.snapshot()
        for view in (seeded["target"], gone):
            for response in (
                facts.run.client.get(merge_url(view.id)),
                facts.run.client.get(merge_url(view.id), params={"target": seeded["draft"].id}),
                facts.get(merge_url(view.id)),
            ):
                assert response.status_code == 409, response.status_code
                elements = outline(response.text)
                assert says(elements, MERGE_NOT_A_DRAFT)
                assert by_id(elements, MERGE_FORM_ID) is None
                assert by_id(elements, MERGE_SEARCH_ID) is None
        assert facts.snapshot() == before


# --- comparing ---------------------------------------------------------------------------------


def test_merge_compare_shows_both_wordings_and_provenance(tmp_path: Path) -> None:
    """Both wordings, each under its own heading with what made it: the fake's label and the
    needs-check note stay visible on the comparison (ADR-089 item 8, ADR-091)."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts, flagged=True)
        draft, target = seeded["draft"], seeded["target"]

        elements, values = compare(facts, draft.id, target.id)

        comparison = required(elements, MERGE_COMPARE_ID)
        ours = required(elements, MERGE_DRAFT_ID)
        theirs = required(elements, MERGE_TARGET_ID)
        assert ours.within(comparison) and theirs.within(comparison)
        assert ROUTES_DRAFT in ours.text and ROUTES not in ours.text
        assert ROUTES in theirs.text and ROUTES_DRAFT not in theirs.text
        for side in (ours, theirs):
            titles = [e.text for e in inside(elements, side) if e.tag in ("h2", "h3")]
            assert len(titles) == 1 and titles[0], "each side has a heading of its own"
            named = (side.attrs.get("aria-labelledby") or "").split()
            assert named and required(elements, named[0]).text == titles[0]
        assert says(elements, PRACTICE_BADGE, ours) and says(elements, NEEDS_CHECK_NOTE, ours)
        assert not says(elements, PRACTICE_BADGE, theirs)
        assert not says(elements, NEEDS_CHECK_NOTE, theirs)

        form = required(elements, MERGE_FORM_ID)
        assert values["target"] == target.id
        assert values["draft_version"] == str(draft.version)
        assert values["target_version"] == str(target.version)
        radios = controls(elements, form, MERGE_CHOICE_FIELD)
        assert [r.attrs.get("value") for r in radios] == list(MERGE_CHOICES)
        assert all(r.attrs.get("type") == "radio" for r in radios)
        assert [label_of(elements, r) for r in radios] == list(MERGE_CHOICES.values())
        assert values[MERGE_CHOICE_FIELD] == "target", "keeping what is confirmed is the default"
        legends = [e.text for e in inside(elements, form) if e.tag == "legend"]
        assert len(legends) == 1 and legends[0], "the choices share a fieldset with a legend"
        (own_wording,) = controls(elements, form, "text")
        assert own_wording.tag == "textarea" and has_label(elements, own_wording)
        assert own_wording.text == ROUTES, "writing starts from the wording that would be kept"
        merge_button = button(elements, form, MERGE)
        assert (merge_button.attrs.get("type") or "submit") == "submit"
        assert "novalidate" in form.attrs

        # A flagged draft as the target keeps its note on its side.
        other = seeded["other"]
        elements, _ = compare(facts, other.id, draft.id)
        theirs = required(elements, MERGE_TARGET_ID)
        assert says(elements, NEEDS_CHECK_NOTE, theirs) and says(elements, PRACTICE_BADGE, theirs)
        assert says(elements, "Draft", theirs), "a draft target says it is one"
        assert not says(elements, NEEDS_CHECK_NOTE, required(elements, MERGE_DRAFT_ID))


def test_merge_compare_escapes_hostile_text_and_story_parts(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, HOSTILE)
        facts.add_import(IMPORT_A)
        story, hostile = facts.drafts(
            IMPORT_A,
            DraftInput(type=FactType.STORY, action="Cut invoice time", result="by 35%."),
            DraftInput(text=HOSTILE + " again"),
        )
        for path, params in (
            (merge_url(hostile.id), {}),
            (merge_url(hostile.id), {"q": "Planned"}),
            (merge_url(hostile.id), {"target": target.id}),
            (merge_url(story.id), {"target": hostile.id}),
        ):
            elements = page_of(facts, path, **params)
            assert by_id(elements, "injected") is None and by_id(elements, "bold") is None
            assert not [e for e in elements if e.tag == "script" and "fictional" in e.text]
        # A story against a story: its parts can be written one by one.
        (other_story,) = facts.drafts(
            IMPORT_A, DraftInput(type=FactType.STORY, action="Cut invoice time", result="by 30%.")
        )
        elements, values = compare(facts, story.id, other_story.id)
        ours = required(elements, MERGE_DRAFT_ID)
        assert says(elements, "Action", ours) and says(elements, "Result", ours)
        assert (values["action"], values["result"]) == ("Cut invoice time", "by 30%.")
        assert "text" not in values


def test_compare_refuses_a_target_that_cannot_be_merged_into(tmp_path: Path) -> None:
    """``?target=`` naming the draft itself, an archived, a discarded or an unknown fact
    shows no merge form and nothing of the request."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft = seeded["draft"]
        archived = typed(facts.bank, NEWSLETTER)
        facts.bank.facts.archive(archived.id, expected_version=archived.version)
        other = seeded["other"]
        facts.bank.facts.discard(other.id, expected_version=other.version)
        for target in (draft.id, archived.id, other.id, "nobody", '"><b id="injected">'):
            response = facts.run.client.get(merge_url(draft.id), params={"target": target})
            assert response.status_code in (200, 404, 409), (target, response.status_code)
            elements = outline(response.text)
            assert by_id(elements, MERGE_FORM_ID) is None, target
            assert by_id(elements, "injected") is None and "nobody" not in response.text


# --- merging -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "typed_text", "settled", "provider"),
    [
        ("target", "", ROUTES, None),
        ("draft", "", ROUTES_DRAFT, "fake"),
        ("edited", SETTLED, SETTLED, None),
    ],
    ids=["keep", "take", "edit"],
)
def test_merge_route_versions_target(
    tmp_path: Path, choice: str, typed_text: str, settled: str, provider: str | None
) -> None:
    """AC-M3.2: whichever wording is settled on, the target gets a new version (source
    ``merge``) and the draft is closed and linked to it; the first version is kept. Wording
    taken as the fake made it keeps its provider; the user's own carries none (ADR-091)."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target, other = seeded["draft"], seeded["target"], seeded["other"]
        fields = {MERGE_CHOICE_FIELD: choice}
        if typed_text:
            fields["text"] = typed_text

        response = merge(facts, draft.id, target.id, **fields)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert "<html" not in response.text and facts.token not in response.text
        assert announcement(response) == SAID_MERGED
        first, second = version_rows(facts.db, target.id)
        assert (first["version"], first["text"]) == (1, ROUTES)
        assert (second["version"], second["text"], second["source"]) == (2, settled, "merge")
        assert second["provider"] == provider
        assert current_version_row(facts.db, target.id)["id"] == second["id"]
        assert event_types(facts.db, target.id) == ["created", "merged"]
        assert facts.status(target.id) == "confirmed"
        closed = facts.bank.facts.get(draft.id)
        assert closed.status.value == "discarded" and closed.merged_into_fact_id == target.id
        assert event_types(facts.db, draft.id) == ["created", "merged_into"]
        assert len(version_rows(facts.db, draft.id)) == 1
        assert set(facts.bank.facts.get(target.id).skills) == {"Scheduling", "Route planning"}
        assert facts.status(other.id) == "draft", "no other draft is touched"

        elements = outline(response.text)
        undo_button = assert_result(facts, elements, draft.id, target.id)
        focus = only_focus(elements)
        result = required(elements, MERGE_RESULT_ID)
        assert focus is result or focus.within(result), "focus stays with the result"
        assert focus is undo_button or focus.tag not in ("button", "a", "input")
        assert settled in result.text, "the wording that was settled on is shown"
        # The draft left Drafts to review; the Library shows the fact with its wording.
        assert by_id(facts.fragment(DRAFTS_URL), fact_id_attr(draft.id)) is None
        assert settled in required(facts.tab("library"), fact_id_attr(target.id)).text


def test_merge_into_another_draft_keeps_it_a_draft(tmp_path: Path) -> None:
    """A draft can be folded into another draft: the target stays a draft, and a flagged one
    stays flagged whatever wording is settled on (ADR-091 item 4)."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts, flagged=True)
        flagged, other = seeded["draft"], seeded["other"]

        response = merge(facts, other.id, flagged.id, wording="edited", text=SETTLED)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        merged = facts.bank.facts.get(flagged.id)
        assert merged.status.value == "draft" and merged.needs_check
        assert merged.content.text == SETTLED and merged.origin_provider == "fake"
        assert facts.status(other.id) == "discarded"
        drafts = facts.fragment(DRAFTS_URL)
        block = required(drafts, fact_id_attr(flagged.id))
        assert SETTLED in block.text and says(drafts, NEEDS_CHECK_NOTE, block)


def test_merge_edited_wording_refused_422_and_bad_choice(tmp_path: Path) -> None:
    """Writing the wording and leaving it blank is refused on that field, with the choice
    kept; a choice that isn't one is refused too. Nothing is merged."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        with pytest.raises(FactInputInvalid) as blank:
            facts.bank.facts.merge(
                draft.id,
                target.id,
                FactContent(text="  "),
                expected_draft_version=draft.version,
                expected_target_version=target.version,
            )
        before = facts.snapshot()

        for typed_text in ("", "   ", "\r\n"):
            response = merge(facts, draft.id, target.id, wording="edited", text=typed_text)
            assert response.status_code == 422, (typed_text, response.status_code)
            assert "<html" not in response.text and "hx-trigger" not in response.headers
            elements = outline(response.text)
            form = required(elements, MERGE_FORM_ID)
            assert posts_to(form) == merge_url(draft.id), "the form can be sent again"
            (field,) = controls(elements, form, "text")
            assert field.attrs.get("aria-invalid") == "true"
            assert blank.value.user_message in [e.text for e in described_by(elements, field)]
            assert only_focus(elements) is field
            kept = form_values(elements, form)
            assert kept[MERGE_CHOICE_FIELD] == "edited" and kept["target"] == target.id
            assert kept["draft_version"] == str(draft.version)
            # Both wordings are still there to write from.
            assert ROUTES_DRAFT in required(elements, MERGE_DRAFT_ID).text
            assert ROUTES in required(elements, MERGE_TARGET_ID).text
        plain = merge(facts, draft.id, target.id, htmx=False, wording="edited", text=" ")
        assert plain.status_code == 422 and "<html" in plain.text
        assert blank.value.user_message in plain.text

        for choice in ("", "both", '"><b id="injected">'):
            response = merge(facts, draft.id, target.id, wording=choice)
            assert response.status_code == 422, (choice, response.status_code)
            assert by_id(outline(response.text), "injected") is None
        # Wording typed while another choice is made is never dropped without a word.
        dropped = merge(facts, draft.id, target.id, wording="target", text="Forged wording")
        assert dropped.status_code == 422
        assert facts.snapshot() == before, "nothing was merged"
        # Keeping a wording with the field as the page filled it in merges.
        kept = merge(facts, draft.id, target.id, wording="target")
        assert kept.status_code == 200
        assert current_version_row(facts.db, target.id)["text"] == ROUTES


def test_merge_stale_versions_show_reload_prompt(tmp_path: Path) -> None:
    """Either side changed since the comparison was shown: 409 with the reload prompt, which
    leads back to the same comparison, and nothing merged."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        _, values = compare(facts, draft.id, target.id)
        facts.bank.facts.edit(
            target.id,
            FactInput(text=ROUTES + " Daily.", skills=("Scheduling",)),
            expected_version=target.version,
        )
        before = facts.snapshot()
        choice = {**values, MERGE_CHOICE_FIELD: "draft"}

        response = send(facts, merge_url(draft.id), choice)

        assert response.status_code == 409, (response.status_code, response.text[:300])
        assert "<html" not in response.text and "hx-trigger" not in response.headers
        elements = outline(response.text)
        prompt = alert(elements, MERGE_CONFLICT_ID)
        assert says(elements, VersionConflict.message, prompt)
        (reload,) = links(elements, RELOAD, prompt)
        path, params, _ = href_parts(reload)
        assert (path, params) == (merge_url(draft.id), {"target": target.id})
        assert only_focus(elements) is prompt
        assert send(facts, merge_url(draft.id), choice, htmx=False).status_code == 409
        for name in ("draft_version", "target_version"):
            for version in ("", "one", "99", "-1", '"><b id="injected">'):
                current, _ = compare(facts, draft.id, target.id)
                fresh = form_values(current, required(current, MERGE_FORM_ID))
                response = send(facts, merge_url(draft.id), {**fresh, name: version})
                assert response.status_code == 409, (name, version, response.status_code)
                assert by_id(outline(response.text), "injected") is None
        assert facts.snapshot() == before, "nothing was merged"
        assert facts.status(draft.id) == "draft"
        # After the reload the comparison shows the target as it is now, and merges.
        elements, values = compare(facts, draft.id, target.id)
        assert ROUTES + " Daily." in required(elements, MERGE_TARGET_ID).text
        assert send(facts, merge_url(draft.id), values).status_code == 200
        assert facts.status(draft.id) == "discarded"


def test_merge_refused_into_archived_discarded_or_itself(tmp_path: Path) -> None:
    """The route refuses what the page never offers: the draft itself, an archived or a
    discarded target (409), a target that doesn't exist (404), and a merge of something that
    isn't a draft. Nothing changes."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target, other = seeded["draft"], seeded["target"], seeded["other"]
        archived = typed(facts.bank, NEWSLETTER)
        archived = facts.bank.facts.archive(archived.id, expected_version=archived.version)
        gone = facts.bank.facts.discard(other.id, expected_version=other.version)
        before = facts.snapshot()

        def post(draft_view: Any, target_view: Any, *, htmx: bool = True) -> Response:
            data = {
                "target": target_view.id,
                "draft_version": str(draft_view.version),
                "target_version": str(target_view.version),
                MERGE_CHOICE_FIELD: "draft",
            }
            return facts.post(merge_url(draft_view.id), data, htmx=htmx)

        refused = {
            "itself": (draft, draft),
            "an archived fact": (draft, archived),
            "a discarded draft": (draft, gone),
            "a confirmed fact is not a draft": (target, draft),
            "a discarded draft is not a draft": (gone, target),
        }
        for label, (source, into) in refused.items():
            response = post(source, into)
            assert response.status_code == 409, (label, response.status_code)
            assert "hx-trigger" not in response.headers, label
            prompt = alert(outline(response.text), MERGE_CONFLICT_ID)
            assert FactTransitionRefused.message in prompt.text, label
            assert post(source, into, htmx=False).status_code == 409, label
        for missing in ("nobody", "", "fictional<b>id"):
            data = {
                "target": missing,
                "draft_version": str(draft.version),
                "target_version": "1",
                MERGE_CHOICE_FIELD: "draft",
            }
            response = facts.post(merge_url(draft.id), data)
            assert response.status_code == 404, (missing, response.status_code)
            assert "nobody" not in response.text and "fictional" not in response.text
        no_target = {"draft_version": str(draft.version), MERGE_CHOICE_FIELD: "draft"}
        assert facts.post(merge_url(draft.id), no_target).status_code == 404
        assert facts.snapshot() == before, "nothing was merged"
        assert facts.status(draft.id) == "draft" and facts.status(target.id) == "confirmed"


# --- undo --------------------------------------------------------------------------------------


def test_merge_undo_route_and_refusal_after_reword(tmp_path: Path) -> None:
    """Undo right after a merge: the target reads as before (a new version, source
    ``merge_undo``), loses the skill the merge added, and the draft is a draft again. Once
    the target is reworded the undo is refused and no longer offered."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        merged = merge(facts, draft.id, target.id, wording="draft")
        answer = outline(merged.text)
        path, values = submitted_by(answer, assert_result(facts, answer, draft.id, target.id))

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert "<html" not in response.text and announcement(response) == SAID_MERGE_UNDONE
        versions = version_rows(facts.db, target.id)
        assert [(v["version"], v["text"], v["source"]) for v in versions] == [
            (1, ROUTES, "manual"),
            (2, ROUTES_DRAFT, "merge"),
            (3, ROUTES, "merge_undo"),
        ]
        assert event_types(facts.db, target.id) == ["created", "merged", "merge_undone"]
        assert facts.bank.facts.get(target.id).skills == ("Scheduling",)
        restored = facts.bank.facts.get(draft.id)
        assert restored.status.value == "draft" and restored.merged_into_fact_id is None
        assert restored.content.text == ROUTES_DRAFT
        elements = outline(response.text)
        assert not [e for e in elements if e.tag == "button" and e.text == UNDO_MERGE]
        review = [
            a for a in elements if a.tag == "a" and href_parts(a)[:2] == (PAGE, {"tab": "drafts"})
        ]
        assert review, "a link back to Drafts to review, where the draft is again"
        assert len(only_focus(elements).text) > 0
        assert required(facts.fragment(DRAFTS_URL), fact_id_attr(draft.id))
        # The same Undo twice is refused: there is no merge to take back.
        again = facts.post(path, values)
        assert again.status_code == 409
        assert facts.status(draft.id) == "draft"

        # Merge again, then reword the target: the undo is refused and nothing changes.
        assert merge(facts, draft.id, target.id, wording="target").status_code == 200
        page = page_of(facts, merge_url(draft.id))
        offered = assert_result(facts, page, draft.id, target.id)
        path, _ = submitted_by(page, offered)
        current = facts.bank.facts.get(target.id)
        facts.bank.facts.edit(
            target.id,
            FactInput(text=SETTLED, skills=current.skills),
            expected_version=current.version,
        )
        before = facts.snapshot()
        refused = undo(facts, draft.id)
        assert refused.status_code == 409, (refused.status_code, refused.text[:300])
        assert "hx-trigger" not in refused.headers
        prompt = alert(outline(refused.text), MERGE_CONFLICT_ID)
        assert FactTransitionRefused.message in prompt.text
        assert undo(facts, draft.id, htmx=False).status_code == 409
        assert facts.snapshot() == before
        assert facts.status(draft.id) == "discarded"
        # The draft's merge page says where it went, with no Undo that would be refused.
        page = page_of(facts, merge_url(draft.id))
        result = required(page, MERGE_RESULT_ID)
        assert not buttons(page, result, UNDO_MERGE)
        assert edit_url(target.id) in [
            href_parts(a)[0] for a in inside(page, result) if a.tag == "a"
        ]


def test_merge_plain_posts_redirect_and_undo_survives_a_reload(tmp_path: Path) -> None:
    """Without scripts: a merge answers 303 to the draft's merge page, which shows the result
    with "Undo merge" for as long as it can be taken back; the undo answers 303 to the
    Drafts tab."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]

        response = merge(facts, draft.id, target.id, htmx=False, wording="edited", text=SETTLED)

        assert response.status_code == 303, (response.status_code, response.text[:300])
        assert response.headers["location"] == merge_url(draft.id)
        assert "hx-trigger" not in response.headers
        assert current_version_row(facts.db, target.id)["text"] == SETTLED
        before = facts.snapshot()
        for _ in range(2):  # the page is a GET: it can be loaded again and changes nothing
            page = page_of(facts, merge_url(draft.id))
            assert MERGE_HEADING in headings(page, "h1")
            offered = assert_result(facts, page, draft.id, target.id)
            assert by_id(page, MERGE_SEARCH_ID) is None, "a closed draft has nothing to search for"
        assert facts.snapshot() == before
        path, values = submitted_by(page, offered)

        undone = facts.post(path, values, htmx=False)

        assert undone.status_code == 303, (undone.status_code, undone.text[:300])
        assert undone.headers["location"] == f"{PAGE}?tab=drafts"
        assert facts.status(draft.id) == "draft"
        assert current_version_row(facts.db, target.id)["text"] == ROUTES
        # A draft again: its page is the search once more.
        page = page_of(facts, merge_url(draft.id))
        assert required(page, MERGE_SEARCH_ID) and by_id(page, MERGE_RESULT_ID) is None
