"""Review fixes to the fact editor, the history and the merge screen (M3.6; AC-M3.2, AC-M3.3).

Honesty and correctness: a draft in the full editor still says it is a draft, with what made
it, and its save line never reads as a confirmation; wording typed on the merge screen is never
dropped without a word; a draft whose stored dates are reversed can still be reworded and
confirmed; "Restore draft" says where it was pressed in a hidden field; a save that finds the
fact discarded keeps what was typed, read-only. The rest is copy, focus and announcements, the
input caps and branches that had no test. The contract's names live in ``tests/facts_page.py``.
Real SQLite in a temp folder, the frozen clock, the fake provider, no network; every value is
fictional.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.domain.errors import FactInputInvalid, FactTransitionRefused, VersionConflict
from app.domain.facts import FactContent, FactType
from app.domain.partial_dates import DATE_MESSAGE
from app.modules.storage import Database
from app.services.facts import DraftInput, FactInput, NewRole
from tests.fact_bank import (
    START,
    Bank,
    assert_atomic,
    count,
    current_version_row,
    event_types,
    fact_row,
    make_bank,
    open_migrated,
    role_row,
    skill_names_of,
    typed,
    version_rows,
)
from tests.facts_page import (
    BACK_TO_DRAFTS,
    BACK_TO_LIBRARY,
    CLOSED_HEADINGS,
    DATE_ERROR,
    DATE_FIELDS,
    DATE_HINT,
    DISCARDED_LIBRARY,
    DRAFTS_URL,
    EDIT_HEADINGS,
    EDITOR_CONFLICT_ID,
    EDITOR_DRAFT_ID,
    EDITOR_ID,
    EDITOR_KEPT_ID,
    EDITOR_PAGE_ID,
    FROM_FIELD,
    HISTORY_ID,
    LIBRARY_URL,
    MAX_NAME_CHARS,
    MAX_ROLE_CHARS,
    MAX_SKILLS,
    MAX_TAGS,
    MERGE_CANDIDATES_HEADING_ID,
    MERGE_CANDIDATES_ID,
    MERGE_CAPPED,
    MERGE_CHOICE_FIELD,
    MERGE_CHOICES,
    MERGE_CONFLICT_ID,
    MERGE_CONTENT_ID,
    MERGE_FORM_ID,
    MERGE_HEADING,
    MERGE_LEGEND,
    MERGE_NOT_A_DRAFT,
    MERGE_RESULT_ID,
    MERGE_SEARCH_ID,
    MERGE_TYPED_NOT_CHOSEN,
    MERGE_UNDO_BLOCKED,
    MERGE_WORDING_NEEDED,
    NEEDS_CHECK_NOTE,
    NEW_HEADINGS,
    NEW_ROLE_FIELDS,
    NEW_SKILLS_FIELD,
    NEW_URL,
    NO_MATCH,
    PAGE,
    PAGE_SIZE,
    PRACTICE_BADGE,
    QUOTE_LABEL,
    QUOTE_LABEL_UNCHECKED,
    RELOAD,
    RESTORE_DRAFT,
    SAID_ADDED,
    SAID_DRAFT_SAVED,
    SAID_EDITED,
    SAID_RESTORED,
    SAID_SAVED,
    SAID_STORY_ADDED,
    SAID_STORY_SAVED,
    STORY_EMPTY,
    STORY_HINTS,
    STORY_INTRO,
    TAGS_FIELD,
    TITLE_SUFFIX,
    UNDO_MERGE,
    FactsApp,
    alert,
    announcement,
    archive_path,
    button,
    buttons,
    by_id,
    controls,
    described_by,
    drafts_tab_text,
    edit_confirm_path,
    edit_url,
    fact_id_attr,
    facts_app,
    form_values,
    found_said,
    headings,
    history_url,
    href_parts,
    inside,
    label_of,
    links,
    merge_candidate_id,
    merge_url,
    only_focus,
    page_of,
    required,
    restore_path,
    says,
    send,
    undo_merge_path,
    undo_path,
    version_id_attr,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_budget_warning_dialog import submitted_by
from tests.integration.test_fact_editor import (
    assert_refused,
    assert_saved,
    editor,
    newest_fact,
    open_edit,
    save_edit,
    save_new,
)
from tests.integration.test_fact_merge_screen import compare, merge, seed
from tests.integration.test_setup_redirect import Element, outline

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
REWORDED = "Planned 80 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
NEWSLETTER = "Edited the monthly staff newsletter"
SITUATION = "Invoices took nine days to process."
ACTION = "Mapped the steps and removed two approvals."
RESULT = "Processing fell to six days."
CV_LINE = "Route planning for a team of three, 60 routes weekly"
IMPORT_A = "imp-a"
TOO_LARGE = "x" * (64 * 1024 + 1)  # one byte more than a form may hold (``MAX_FORM_BYTES``)


# --- helpers -----------------------------------------------------------------------------------


def titles(elements: list[Element]) -> list[str]:
    return [e.text for e in elements if e.tag == "title"]


def hint_texts(elements: list[Element], control: Element) -> list[str]:
    return [e.text for e in described_by(elements, control)]


def back_links(elements: list[Element]) -> list[tuple[str, tuple[str, dict[str, str]]]]:
    """Every "Back to ..." link of an answer: its words and where it leads."""
    found = [e for e in elements if e.tag == "a" and e.text.startswith("Back to")]
    return [(e.text, href_parts(e)[:2]) for e in found]


def one_draft(facts: FactsApp, draft: Any = ROUTES) -> Any:
    if by_import(facts) == 0:
        facts.add_import(IMPORT_A)
    (made,) = facts.drafts(IMPORT_A, draft)
    return made


def by_import(facts: FactsApp) -> int:
    return len(facts.run.rows("SELECT id FROM cv_imports"))


def discard(facts: FactsApp, view: Any) -> Any:
    return facts.bank.facts.discard(view.id, expected_version=view.version)


def close_by_merge(facts: FactsApp, draft: Any, target: Any) -> Any:
    return facts.bank.facts.merge(
        draft.id,
        target.id,
        target.content,
        expected_draft_version=draft.version,
        expected_target_version=target.version,
    )


# ================================================================================================
# A. Honesty and correctness
# ================================================================================================


def test_draft_in_editor_says_it_is_a_draft_with_its_provenance(tmp_path: Path) -> None:
    """Item 1: under the heading a draft keeps its Draft badge, the fake's label, the
    needs-check note and the CV line it came from, with the caption the Drafts tab uses; the
    way back is the Drafts tab. A confirmed fact has none of that."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        checked, flagged = facts.drafts(
            IMPORT_A,
            DraftInput(text=ROUTES, original_answer=CV_LINE),
            DraftInput(text=DEGREE, original_answer="BSc, Example University", needs_check=True),
        )

        elements = page_of(facts, edit_url(checked.id))
        block = required(elements, EDITOR_DRAFT_ID)
        heading = next(e for e in elements if e.tag == "h1")
        form = required(elements, EDITOR_ID)
        order = [id(e) for e in elements]
        assert order.index(id(heading)) < order.index(id(block)) < order.index(id(form)), (
            "under the heading, above the form"
        )
        assert says(elements, "Draft", block), "the status, as a text badge"
        assert says(elements, PRACTICE_BADGE, block), "ADR-089 item 8: the fake made it"
        assert says(elements, QUOTE_LABEL, block) and says(elements, CV_LINE, block)
        assert not says(elements, QUOTE_LABEL_UNCHECKED, block)
        assert not says(elements, NEEDS_CHECK_NOTE, block)
        # The hint that says where a draft is confirmed sits there too, once.
        (review,) = [
            a
            for a in inside(elements, block)
            if a.tag == "a" and href_parts(a) == (PAGE, {"tab": "drafts"}, fact_id_attr(checked.id))
        ]
        assert review.text == "Drafts to review"
        hints = [e for e in elements if e.tag == "p" and e.text.startswith("This is a draft.")]
        assert len(hints) == 1 and hints[0].within(block)
        assert back_links(elements) == [(BACK_TO_DRAFTS, (PAGE, {"tab": "drafts"}))]

        # A flagged draft: the note, and the quote is what the AI said, never "From your CV".
        elements = page_of(facts, edit_url(flagged.id))
        block = required(elements, EDITOR_DRAFT_ID)
        assert says(elements, NEEDS_CHECK_NOTE, block)
        assert says(elements, QUOTE_LABEL_UNCHECKED, block)
        assert not says(elements, QUOTE_LABEL, block)
        assert says(elements, "BSc, Example University", block)

        # A draft the user's own provider path didn't make has no practice badge and no quote.
        from app.services.facts import DraftOrigin

        (plain,) = facts.bank.facts.create_drafts(
            DraftOrigin(source="cv_import"), [DraftInput(text=NEWSLETTER)]
        )
        elements = page_of(facts, edit_url(plain.id))
        block = required(elements, EDITOR_DRAFT_ID)
        assert says(elements, "Draft", block) and not says(elements, PRACTICE_BADGE, block)
        assert not [e for e in inside(elements, block) if e.tag in ("figure", "blockquote")]

        # A confirmed fact: no draft block, and the way back is the Library.
        own = typed(facts.bank, INVOICES)
        elements = page_of(facts, edit_url(own.id))
        assert by_id(elements, EDITOR_DRAFT_ID) is None
        assert back_links(elements) == [(BACK_TO_LIBRARY, (PAGE, {"tab": "library"}))]
        assert back_links(page_of(facts, NEW_URL)) == [
            (BACK_TO_LIBRARY, (PAGE, {"tab": "library"}))
        ]


def test_draft_save_line_does_not_read_as_a_confirmation(tmp_path: Path) -> None:
    """Item 1: saving a draft in the full editor says it still needs confirming, keeps its
    provenance on the answer, and "Fact saved." stays for confirmed facts."""
    with facts_app(tmp_path) as facts:
        draft = one_draft(facts, DraftInput(text=ROUTES, original_answer=CV_LINE, needs_check=True))

        response = save_edit(facts, draft.id, text=REWORDED)

        elements = assert_saved(facts, response, draft.id, SAID_DRAFT_SAVED)
        assert "confirm" in SAID_DRAFT_SAVED and SAID_DRAFT_SAVED != SAID_SAVED
        block = required(elements, EDITOR_DRAFT_ID)
        assert says(elements, "Draft", block) and says(elements, PRACTICE_BADGE, block)
        assert says(elements, NEEDS_CHECK_NOTE, block), "an edit is not the user's check"
        assert says(elements, CV_LINE, block), "the CV line stays beside the new wording"
        assert facts.status(draft.id) == "draft"
        assert back_links(elements) == [(BACK_TO_DRAFTS, (PAGE, {"tab": "drafts"}))]
        # A details-only save of a draft says the same.
        response = save_edit(facts, draft.id, tags="planning")
        assert_saved(facts, response, draft.id, SAID_DRAFT_SAVED)

        own = typed(facts.bank, INVOICES)
        assert_saved(facts, save_edit(facts, own.id, tags="finance"), own.id, SAID_SAVED)


@pytest.mark.parametrize("choice", ["target", "draft"])
def test_merge_refuses_typed_wording_that_was_not_chosen(tmp_path: Path, choice: str) -> None:
    """Item 2: wording typed into the field while "keep" or "use the draft's" is chosen would
    be dropped without a word. It is refused instead (422), tied to the choice, with the
    typing and the choice kept, and nothing merged."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        before = facts.snapshot()
        typed_text = "Planned 60 delivery routes a week, typed by hand."

        response = merge(facts, draft.id, target.id, wording=choice, text=typed_text)

        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert "<html" not in response.text and "hx-trigger" not in response.headers
        elements = outline(response.text)
        form = required(elements, MERGE_FORM_ID)
        radios = controls(elements, form, MERGE_CHOICE_FIELD)
        assert all(r.attrs.get("aria-invalid") == "true" for r in radios)
        for radio in radios:
            assert MERGE_TYPED_NOT_CHOSEN in hint_texts(elements, radio), radio.attrs
        (chosen,) = [r for r in radios if "checked" in r.attrs]
        assert chosen.attrs.get("value") == choice, "the choice is kept"
        assert only_focus(elements) is chosen
        kept = form_values(elements, form)
        assert kept["text"] == typed_text, "what was typed is still there"
        assert kept["target_version"] == str(target.version)
        (wording,) = controls(elements, form, "text")
        assert wording.attrs.get("aria-invalid") is None, "the message is the choice's"
        assert facts.snapshot() == before, "nothing was merged"

        # Without scripts: the full page, the same refusal.
        plain = merge(facts, draft.id, target.id, htmx=False, wording=choice, text=typed_text)
        assert plain.status_code == 422 and "<html" in plain.text
        assert says(outline(plain.text), MERGE_TYPED_NOT_CHOSEN)
        assert facts.snapshot() == before

        # Choosing "Write the wording myself" uses it.
        used = merge(facts, draft.id, target.id, wording="edited", text=typed_text)
        assert used.status_code == 200
        assert current_version_row(facts.db, target.id)["text"] == typed_text


def test_merge_untouched_wording_field_is_not_a_change(tmp_path: Path) -> None:
    """Item 2: the field as the page filled it in is no typing: with a browser's CRLF line
    endings, spaces around it, or not sent at all, the chosen wording is merged. A cleared
    field is a change."""
    with facts_app(tmp_path) as facts:
        two_lines = "Planned 60 delivery routes a week.\nLed a team of 3."
        target = typed(facts.bank, two_lines)
        facts.add_import(IMPORT_A)
        first, second, third, fourth = facts.drafts(IMPORT_A, ROUTES, DEGREE, NEWSLETTER, INVOICES)

        as_posted = two_lines.replace("\n", "\r\n") + "  "
        response = merge(facts, first.id, target.id, wording="target", text=as_posted)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert facts.status(first.id) == "discarded"

        _, values = compare(facts, second.id, target.id)
        without = {name: value for name, value in values.items() if name != "text"}
        response = send(facts, merge_url(second.id), {**without, MERGE_CHOICE_FIELD: "draft"})
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert current_version_row(facts.db, target.id)["text"] == DEGREE

        before = facts.snapshot()
        cleared = merge(facts, third.id, target.id, wording="target", text="")
        assert cleared.status_code == 422
        assert says(outline(cleared.text), MERGE_TYPED_NOT_CHOSEN)
        assert facts.snapshot() == before

        # A story target: any of its parts counts.
        story = facts.bank.facts.create(
            FactInput(type=FactType.STORY, situation=SITUATION, action=ACTION, result=RESULT)
        )
        (part,) = facts.drafts(
            IMPORT_A, DraftInput(type=FactType.STORY, action="Removed two approvals.")
        )
        before = facts.snapshot()
        changed = merge(facts, part.id, story.id, wording="draft", result="Processing fell.")
        assert changed.status_code == 422
        assert says(outline(changed.text), MERGE_TYPED_NOT_CHOSEN)
        assert facts.snapshot() == before
        assert merge(facts, fourth.id, target.id, wording="target").status_code == 200


def test_merge_stale_form_with_typed_wording_is_stale_not_refused(tmp_path: Path) -> None:
    """Item 2: a form filled in from older wording proves nothing about typing. With a stale
    target version it answers 409 with the reload prompt, as before."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        _, values = compare(facts, draft.id, target.id)
        facts.bank.facts.edit(
            target.id,
            FactInput(text=REWORDED, skills=("Scheduling",)),
            expected_version=target.version,
        )
        before = facts.snapshot()
        response = send(facts, merge_url(draft.id), {**values, MERGE_CHOICE_FIELD: "target"})
        assert response.status_code == 409, (response.status_code, response.text[:300])
        prompt = alert(outline(response.text), MERGE_CONFLICT_ID)
        assert VersionConflict.message in prompt.text
        assert facts.snapshot() == before


def test_reversed_stored_dates_do_not_block_rewording_a_draft(tmp_path: Path) -> None:
    """Item 3: an import may store a draft whose end is before its start. The inline "Save
    and confirm" and ``edit_and_confirm`` send those dates back unseen, so they are not
    refused with a date error on the wording field; the full editor saves too while the
    dates are left alone. A save that changes the dates is still checked."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        reversed_dates = {"start_date": "2024-05", "end_date": "2022"}
        inline, service, full, redated = facts.drafts(
            IMPORT_A,
            DraftInput(text=ROUTES, original_answer=CV_LINE, **reversed_dates),
            DraftInput(text=DEGREE, **reversed_dates),
            DraftInput(text=NEWSLETTER, **reversed_dates),
            DraftInput(text=INVOICES, **reversed_dates),
        )
        for view in (inline, service, full, redated):
            row = fact_row(facts.db, view.id)
            assert (row["start_date"], row["end_date"]) == ("2024-05", "2022"), "stored reversed"

        # The inline form: reworded and confirmed, the dates as they were.
        response = facts.act(edit_confirm_path(inline.id), inline.id, text=REWORDED)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_EDITED
        assert facts.status(inline.id) == "confirmed"
        assert current_version_row(facts.db, inline.id)["text"] == REWORDED
        row = fact_row(facts.db, inline.id)
        assert (row["start_date"], row["end_date"]) == ("2024-05", "2022")
        # Unchanged wording through the same form is a plain confirm.
        untouched = facts.drafts(IMPORT_A, DraftInput(text="English", **reversed_dates))[0]
        response = facts.act(edit_confirm_path(untouched.id), untouched.id, text="English")
        assert response.status_code == 200 and facts.status(untouched.id) == "confirmed"

        # The service, as the route calls it.
        confirmed = facts.bank.facts.edit_and_confirm(
            service.id,
            FactInput(text=DEGREE + ".", **reversed_dates),
            expected_version=service.version,
        )
        assert confirmed.status.value == "confirmed"
        assert (confirmed.start_date, confirmed.end_date) == ("2024-05", "2022")

        # The full editor, dates untouched: the wording is saved.
        response = save_edit(facts, full.id, text=NEWSLETTER + ", 12 issues a year")
        assert_saved(facts, response, full.id, SAID_DRAFT_SAVED)
        # The same through ``edit``.
        current = facts.bank.facts.get(full.id)
        kept = facts.bank.facts.edit(
            full.id,
            FactInput(text=NEWSLETTER, tags=("writing",), **reversed_dates),
            expected_version=current.version,
        )
        assert kept.tags == ("writing",) and kept.end_date == "2022"

        # Changing either date while the end stays before the start is refused, on the date.
        before = facts.snapshot()
        for change in ({"end_date": "2023"}, {"start_date": "2024-06"}):
            refused = save_edit(facts, redated.id, **change)
            assert_refused(refused, "end_date")
            with pytest.raises(FactInputInvalid) as error:
                facts.bank.facts.edit_and_confirm(
                    redated.id,
                    FactInput(text=INVOICES, **{**reversed_dates, **change}),
                    expected_version=redated.version,
                )
            assert error.value.field == "end_date"
        assert facts.snapshot() == before
        # Putting them in order saves.
        fixed = save_edit(facts, redated.id, end_date="2024-09")
        assert fixed.status_code == 200
        assert fact_row(facts.db, redated.id)["end_date"] == "2024-09"


def test_restore_draft_from_library_uses_the_from_field(tmp_path: Path) -> None:
    """Item 4: the Library's "Restore draft" sends ``from=library`` (no header is read). The
    htmx answer is the Library's card with the Drafts tab's new count, and a plain request
    answers 303 back to the Discarded filter. Without the field the answers are the Drafts
    tab's, whatever ``HX-Trigger`` says."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        first, second, third = facts.drafts(IMPORT_A, ROUTES, DEGREE, NEWSLETTER)
        gone = [discard(facts, view) for view in (first, second, third)]
        listed = facts.fragment(LIBRARY_URL, status="discarded")
        block = required(listed, fact_id_attr(first.id))
        path, values = submitted_by(listed, button(listed, block, RESTORE_DRAFT))
        assert values[FROM_FIELD] == "library"

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_RESTORED
        elements = outline(response.text)
        card = required(elements, fact_id_attr(first.id))
        assert card.attrs.get("role") == "listitem" and ROUTES in card.text
        focus = only_focus(elements)
        assert focus.within(card) and focus.tag not in ("button", "a", "input")
        (review,) = [a for a in inside(elements, card) if a.tag == "a"]
        assert href_parts(review) == (PAGE, {"tab": "drafts"}, fact_id_attr(first.id))
        assert not buttons(elements, card, RESTORE_DRAFT), "nothing left to restore"
        assert not [e for e in elements if e.tag == "form"], "the card, not the Drafts block"
        (tab,) = [e for e in elements if e.attrs.get("hx-swap-oob")]
        assert tab.text == drafts_tab_text(1), "the Drafts tab's link gets its new count"

        # Plain: back to where it was pressed.
        plain = facts.post(
            undo_path(second.id),
            {"version": str(gone[1].version), FROM_FIELD: "library"},
            htmx=False,
        )
        assert plain.status_code == 303
        location = outline(f'<a href="{plain.headers["location"]}">x</a>')[0]
        assert href_parts(location)[:2] == DISCARDED_LIBRARY
        assert facts.status(second.id) == "draft"

        # No field: the Drafts tab's own block (its Undo), even if a header names the form.
        headers = {**facts.run.run.action(), "HX-Request": "true"}
        headers["HX-Trigger"] = f"restore-draft-{third.id}"
        response = facts.run.client.post(
            undo_path(third.id), data={"version": str(gone[2].version)}, headers=headers
        )
        assert response.status_code == 200
        elements = outline(response.text)
        assert [
            e
            for e in inside(elements, required(elements, fact_id_attr(third.id)))
            if e.tag == "form"
        ]
        # The field means something for "Restore draft" only.
        again = discard(facts, facts.bank.facts.get(third.id))
        plain = facts.post(undo_path(third.id), {"version": str(again.version)}, htmx=False)
        assert plain.status_code == 303 and plain.headers["location"] == f"{PAGE}?tab=drafts"
        fourth = one_draft(facts, INVOICES)
        forged = facts.post(
            f"/facts/{fourth.id}/discard",
            {"version": str(fourth.version), FROM_FIELD: "library"},
            htmx=False,
        )
        assert forged.status_code == 303 and forged.headers["location"] == f"{PAGE}?tab=drafts"


def test_restore_draft_from_library_409_links_back_to_discarded(tmp_path: Path) -> None:
    """Item 4: the draft changed since the Library listed it (restored in another tab, or a
    stale version): 409 with the reload prompt, whose link is the Discarded filter, not the
    Drafts tab. Nothing changes and nothing is announced."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        gone = discard(facts, draft)
        facts.bank.facts.undo_discard(draft.id, expected_version=gone.version)  # another tab
        before = facts.snapshot()
        sent = {"version": str(gone.version), FROM_FIELD: "library"}

        response = facts.post(undo_path(draft.id), sent)

        assert response.status_code == 409, (response.status_code, response.text[:300])
        assert "hx-trigger" not in response.headers
        elements = outline(response.text)
        prompt = alert(elements, f"{fact_id_attr(draft.id)}-conflict")
        assert prompt.within(required(elements, fact_id_attr(draft.id)))
        assert only_focus(elements) is prompt
        (reload,) = links(elements, RELOAD, prompt)
        assert href_parts(reload)[:2] == DISCARDED_LIBRARY
        # A draft that isn't discarded can't be restored: the same prompt, the same way back.
        current = facts.bank.facts.get(draft.id)
        refused = facts.post(
            undo_path(draft.id), {"version": str(current.version), FROM_FIELD: "library"}
        )
        assert refused.status_code == 409
        refused_elements = outline(refused.text)
        assert FactTransitionRefused.message in refused_elements[0].text
        (reload,) = links(refused_elements, RELOAD)
        assert href_parts(reload)[:2] == DISCARDED_LIBRARY
        # From the Drafts tab (no field) the prompt still leads to the Drafts tab.
        (reload,) = links(outline(facts.post(undo_path(draft.id), {"version": "1"}).text), RELOAD)
        assert href_parts(reload)[:2] == (PAGE, {"tab": "drafts"})
        assert facts.post(undo_path(draft.id), sent, htmx=False).status_code == 409
        assert facts.snapshot() == before


def test_save_of_a_fact_discarded_meanwhile_keeps_the_typing_read_only(tmp_path: Path) -> None:
    """Item 5: the draft was discarded in another tab while its editor was open. The save is
    refused (409) and the fact has no form any more, but what was typed is still in the
    answer, read-only and labelled, so it can be copied."""
    with facts_app(tmp_path) as facts:
        draft = one_draft(facts)
        _, values = open_edit(facts, draft.id)
        gone = discard(facts, draft)
        before = facts.snapshot()
        typed_text = "Planned delivery routes, typed before the discard."
        sent = {
            **values,
            "version": str(gone.version),
            "text": typed_text,
            TAGS_FIELD: "logistics, planning",
            NEW_SKILLS_FIELD: "Route planning",
            "end_date": "2024",
        }

        for htmx in (True, False):
            response = send(facts, edit_url(draft.id), sent, htmx=htmx)
            assert response.status_code == 409, (response.status_code, response.text[:300])
            assert "hx-trigger" not in response.headers
            elements = outline(response.text)
            prompt = alert(elements, EDITOR_CONFLICT_ID)
            assert FactTransitionRefused.message in prompt.text
            assert "What you typed is still below" in prompt.text
            assert only_focus(elements) is prompt
            assert by_id(elements, EDITOR_ID) is None, "nothing can be saved from here"
            kept = required(elements, EDITOR_KEPT_ID)
            shown = {
                label_of(elements, e).split(" (")[0]: e
                for e in inside(elements, kept)
                if e.tag == "textarea"
            }
            assert {name: e.text for name, e in shown.items()} == {
                "Wording": typed_text,
                "Add skills": "Route planning",
                "Tags": "logistics, planning",
                "End date": "2024",
            }
            assert all("readonly" in e.attrs and "name" not in e.attrs for e in shown.values())
            assert not buttons(elements, required(elements, EDITOR_PAGE_ID), RESTORE_DRAFT), (
                "the page is stale: reload first"
            )
            assert CLOSED_HEADINGS["discarded"] in headings(elements, "h1")
        assert facts.snapshot() == before

        # Hostile typing is shown as the text it is.
        hostile = '</textarea><script id="injected">window.fictional=1</script>'
        response = send(facts, edit_url(draft.id), {**sent, "text": hostile})
        elements = outline(response.text)
        assert by_id(elements, "injected") is None
        assert not [e for e in elements if e.tag == "script"]


def test_merge_undo_blocked_copy_names_a_later_merge(tmp_path: Path) -> None:
    """Item 6: while a later merge into the same fact stands, the earlier one can't be undone
    yet, and the page says how to get there instead of "any more"."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        first, second, target = seeded["draft"], seeded["other"], seeded["target"]
        assert merge(facts, first.id, target.id, wording="draft").status_code == 200
        assert merge(facts, second.id, target.id, wording="draft").status_code == 200

        page = page_of(facts, merge_url(first.id))
        result = required(page, MERGE_RESULT_ID)
        assert not buttons(page, result, UNDO_MERGE)
        assert says(page, MERGE_UNDO_BLOCKED, result)
        assert "any more" not in result.text
        # The later merge can be undone, and then the earlier one.
        later = page_of(facts, merge_url(second.id))
        path, values = submitted_by(
            later, button(later, required(later, MERGE_RESULT_ID), UNDO_MERGE)
        )
        assert facts.post(path, values).status_code == 200
        page = page_of(facts, merge_url(first.id))
        assert buttons(page, required(page, MERGE_RESULT_ID), UNDO_MERGE)
        assert not says(page, MERGE_UNDO_BLOCKED)


# ================================================================================================
# B. UX
# ================================================================================================


def test_htmx_save_answers_carry_the_new_title(tmp_path: Path) -> None:
    """Item 7: an htmx "Add" replaces the page's content with the fact's own editor, so the
    answer starts with the ``<title>`` that heading needs (htmx applies it). A refused or
    stale save carries its own. A full page has one title, in its head."""
    with facts_app(tmp_path) as facts:
        response = save_new(facts, text=ROUTES)
        elements = outline(response.text)
        assert titles(elements) == [EDIT_HEADINGS["fact"] + TITLE_SUFFIX]
        assert elements[0].tag == "title", "top level, where htmx reads it"
        assert elements[0].parent is None and required(elements, EDITOR_PAGE_ID).parent is None
        assert EDIT_HEADINGS["fact"] in headings(elements, "h1")
        fact_id = newest_fact(facts)

        story = save_new(facts, "story", action=ACTION)
        assert titles(outline(story.text)) == [EDIT_HEADINGS["story"] + TITLE_SUFFIX]
        refused = save_new(facts, "story")
        assert titles(outline(refused.text)) == ["Error: " + NEW_HEADINGS["story"] + TITLE_SUFFIX]

        saved = save_edit(facts, fact_id, text=REWORDED)
        assert titles(outline(saved.text)) == [EDIT_HEADINGS["fact"] + TITLE_SUFFIX]
        invalid = save_edit(facts, fact_id, text="")
        assert titles(outline(invalid.text)) == ["Error: " + EDIT_HEADINGS["fact"] + TITLE_SUFFIX]

        # A save that finds the draft discarded: the heading is a new one, and so is the title.
        draft = one_draft(facts, DEGREE)
        _, values = open_edit(facts, draft.id)
        gone = discard(facts, draft)
        stale = send(facts, edit_url(draft.id), {**values, "version": str(gone.version)})
        assert stale.status_code == 409
        assert titles(outline(stale.text)) == [
            "Error: " + CLOSED_HEADINGS["discarded"] + TITLE_SUFFIX
        ]

        # Full pages: one <title>, in the head; an htmx page load carries none.
        for page in (
            facts.run.client.get(edit_url(fact_id)).text,
            save_new(facts, htmx=False).text,
            send(facts, edit_url(draft.id), values, htmx=False).text,
        ):
            parsed = outline(page)
            (title,) = [e for e in parsed if e.tag == "title"]
            assert title.parent is not None and title.parent.tag == "head"
        assert titles(outline(facts.get(edit_url(fact_id)).text)) == []
        assert titles(outline(facts.get(NEW_URL).text)) == []
        # Hostile wording never reaches the title.
        assert "<script" not in "".join(titles(outline(save_new(facts, text="<script>x").text)))


def test_merge_choices_have_hints(tmp_path: Path) -> None:
    """Item 8: each choice is described by its one-line help, with the current words. A
    draft is merged only into a confirmed fact, so the target is always "the existing
    fact"."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        elements, _ = compare(facts, draft.id, target.id)
        form = required(elements, MERGE_FORM_ID)
        radios = controls(elements, form, MERGE_CHOICE_FIELD)
        assert [label_of(elements, r) for r in radios] == list(MERGE_CHOICES.values())
        assert [e.text for e in inside(elements, form) if e.tag == "legend"] == [MERGE_LEGEND]
        helps = []
        for radio in radios:
            (note,) = described_by(elements, radio)
            assert note.text and note.within(form) and note.tag == "p"
            assert note.within(radio.parent), "the help sits with its choice"
            helps.append(note.text)
        assert len(set(helps)) == len(radios), "each choice has its own help"
        # A refused choice is described by the error and still by its help.
        refused = merge(facts, draft.id, target.id, wording="both")
        elements = outline(refused.text)
        radios = controls(elements, required(elements, MERGE_FORM_ID), MERGE_CHOICE_FIELD)
        assert [label_of(elements, r) for r in radios] == list(MERGE_CHOICES.values())
        for radio in radios:
            assert len(described_by(elements, radio)) == 2


def test_merge_search_is_enhanced_focuses_candidates_and_announces(tmp_path: Path) -> None:
    """Item 9: the search form still works as a plain GET, and with htmx it replaces the
    screen's content, keeps the address, moves focus to the candidates' heading and says
    what was found. A page load does neither."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft = seeded["draft"]
        typed(facts.bank, INVOICES)
        path = merge_url(draft.id)

        page = page_of(facts, path)
        search = required(page, MERGE_SEARCH_ID)
        assert search.attrs.get("action") == path and search.attrs.get("hx-get") == path
        assert (search.attrs.get("method") or "get").lower() == "get"
        assert search.attrs.get("hx-push-url") == "true"
        assert search.attrs.get("hx-target") == f"#{MERGE_CONTENT_ID}"
        # Settled at once: the field is part of what is swapped, and must not get the old
        # one's attributes back for a moment while the user is typing again.
        assert search.attrs.get("hx-swap") == "outerHTML settle:0ms"
        content = required(page, MERGE_CONTENT_ID)
        assert search.within(content) and required(page, MERGE_CANDIDATES_ID).within(content)
        assert MERGE_HEADING in [e.text for e in inside(page, content) if e.tag == "h1"]
        assert not [e for e in page if "data-autofocus" in e.attrs], "a page load moves no focus"

        # Confirmed facts only: the seeded fact and the invoices, never the other draft.
        for words, total in (("invoice", 1), ("", 2), ("planned", 1), ("xylophone", 0)):
            response = facts.get(path, q=words)
            assert response.status_code == 200, (words, response.status_code)
            assert "<html" not in response.text
            elements = outline(response.text)
            assert required(elements, MERGE_CONTENT_ID) is elements[0], "what the form targets"
            focus = only_focus(elements)
            assert focus is required(elements, MERGE_CANDIDATES_HEADING_ID)
            assert focus.tag == "h2" and focus.attrs.get("tabindex") == "-1"
            assert announcement(response) == found_said(total), words
            listing = required(elements, MERGE_CANDIDATES_ID)
            rows = [
                e
                for e in inside(elements, listing)
                if (e.attrs.get("id") or "").startswith(merge_candidate_id(""))
            ]
            assert len(rows) == total
            assert form_values(elements, required(elements, MERGE_SEARCH_ID))["q"] == words
        assert found_said(0) == NO_MATCH and found_said(1) == "1 fact found."
        assert found_said(3) == "3 facts found."

        # Not a search: the htmx page without ``q``, the comparison, and any full page.
        for response in (
            facts.get(path),
            facts.get(path, target=seeded["target"].id),
            facts.get(path, q="invoice", target=seeded["target"].id),
            facts.get(path, htmx=False, q="invoice"),
            facts.run.client.get(path, params={"q": "invoice"}),
        ):
            assert response.status_code == 200
            assert "hx-trigger" not in response.headers
            assert not [e for e in outline(response.text) if "data-autofocus" in e.attrs]
        hostile = facts.get(path, q='"><b id="injected">')
        assert by_id(outline(hostile.text), "injected") is None
        assert announcement(hostile) == NO_MATCH


def test_stories_are_called_stories(tmp_path: Path) -> None:
    """Item 10: an empty story is asked for one of its parts, a saved one is a story in the
    line and the announcement, and a blank wording on the merge screen is asked for there."""
    with facts_app(tmp_path) as facts:
        assert_refused(save_new(facts, "story"), "situation", STORY_EMPTY)
        assert "fact" not in STORY_EMPTY.lower()
        with pytest.raises(FactInputInvalid) as blank:
            facts.bank.facts.create(FactInput(type=FactType.STORY, situation=" "))
        assert (blank.value.field, blank.value.user_message) == ("text", STORY_EMPTY)
        # A fact keeps its own words.
        with pytest.raises(FactInputInvalid) as blank_fact:
            facts.bank.facts.create(FactInput(text=" "))
        assert blank_fact.value.user_message not in (STORY_EMPTY, MERGE_WORDING_NEEDED)
        assert_refused(save_new(facts, text=" "), "text", blank_fact.value.user_message)

        response = save_new(facts, "story", action=ACTION)
        story_id = newest_fact(facts)
        assert_saved(facts, response, story_id, SAID_STORY_ADDED)
        response = save_edit(facts, story_id, result=RESULT)
        assert_saved(facts, response, story_id, SAID_STORY_SAVED)
        assert_refused(save_edit(facts, story_id, action="", result=""), "situation", STORY_EMPTY)
        added = save_new(facts, text=ROUTES)
        fact_id = newest_fact(facts)
        assert_saved(facts, added, fact_id, SAID_ADDED)

        # An imported story with no part at all is refused the same way.
        facts.add_import(IMPORT_A)
        with pytest.raises(FactInputInvalid) as empty_draft:
            facts.drafts(IMPORT_A, DraftInput(type=FactType.STORY, action=" "))
        assert empty_draft.value.user_message == STORY_EMPTY

        # The merge screen asks for the wording, whatever the target is.
        (draft,) = facts.drafts(IMPORT_A, DEGREE)
        for target_id, name in ((fact_id, "text"), (story_id, "action")):
            _, values = compare(facts, draft.id, target_id)
            blanked = {n: "" for n in ("text", "situation", "action", "result") if n in values}
            refused = send(
                facts, merge_url(draft.id), {**values, **blanked, MERGE_CHOICE_FIELD: "edited"}
            )
            assert refused.status_code == 422
            shown = outline(refused.text)
            form = required(shown, MERGE_FORM_ID)
            invalid = [e for e in inside(shown, form) if e.attrs.get("aria-invalid") == "true"]
            assert len(invalid) == 1 and only_focus(shown) is invalid[0]
            assert MERGE_WORDING_NEEDED in hint_texts(shown, invalid[0]), name
        with pytest.raises(FactInputInvalid) as blank_merge:
            facts.bank.facts.merge(
                draft.id,
                story_id,
                FactContent(text=" "),
                expected_draft_version=draft.version,
                expected_target_version=facts.bank.facts.get(story_id).version,
            )
        assert blank_merge.value.user_message == MERGE_WORDING_NEEDED


def test_story_fields_have_hints_and_an_intro(tmp_path: Path) -> None:
    """Item 11: each part of a story says in a line what to write, tied to its field, and
    the page says under its heading that not every part is needed. A fact has neither."""
    with facts_app(tmp_path) as facts:
        story = facts.bank.facts.create(FactInput(type=FactType.STORY, action=ACTION))
        refused = outline(save_new(facts, "story").text)
        for elements in (
            page_of(facts, NEW_URL, type="story"),
            page_of(facts, edit_url(story.id)),
            refused,
        ):
            form = required(elements, EDITOR_ID)
            for name, hint in STORY_HINTS.items():
                (field,) = controls(elements, form, name)
                notes = described_by(elements, field)
                assert hint in [note.text for note in notes], name
                assert all(note.within(form) for note in notes)
            intro = next(e for e in elements if e.tag == "p" and e.text == STORY_INTRO)
            heading = next(e for e in elements if e.tag == "h1")
            order = [id(e) for e in elements]
            assert order.index(id(heading)) < order.index(id(intro)) < order.index(id(form))
        # The refused part is described by its hint and by the message, and takes focus.
        form = required(refused, EDITOR_ID)
        (situation,) = controls(refused, form, "situation")
        assert hint_texts(refused, situation) == [STORY_HINTS["situation"], STORY_EMPTY]
        assert only_focus(refused) is situation

        own = typed(facts.bank, ROUTES)
        for elements in (page_of(facts, NEW_URL), page_of(facts, edit_url(own.id))):
            assert not says(elements, STORY_INTRO)
            (wording,) = controls(elements, required(elements, EDITOR_ID), "text")
            assert wording.attrs.get("aria-describedby") is None


def test_date_hint_and_error_show_each_accepted_shape(tmp_path: Path) -> None:
    """Item 12: both date fields are described by the hint that shows the three shapes, and a
    date that isn't one is told how to write it."""
    with facts_app(tmp_path) as facts:
        assert DATE_ERROR == DATE_MESSAGE
        elements = page_of(facts, NEW_URL)
        form = required(elements, EDITOR_ID)
        for name in DATE_FIELDS:
            (field,) = controls(elements, form, name)
            assert hint_texts(elements, field) == [DATE_HINT]
        assert len([e for e in elements if e.text == DATE_HINT and e.tag == "p"]) == 1
        for name in DATE_FIELDS:
            shown, field = assert_refused(
                save_new(facts, text=ROUTES, **{name: "March 2024"}), name, DATE_ERROR
            )
            assert hint_texts(shown, field) == [DATE_HINT, DATE_ERROR]


def test_history_runs_end_with_a_marker_and_never_touch(tmp_path: Path) -> None:
    """Item 13: "60" replaced by "80". Each run ends with hidden words as well as starting
    with them, and a space is written between the two, so it never reads "6080"."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        facts.bank.facts.edit(own.id, FactInput(text=REWORDED), expected_version=own.version)
        for response in (facts.get(history_url(own.id)), facts.run.client.get(edit_url(own.id))):
            assert response.status_code == 200
            elements = outline(response.text)
            entry = required(elements, version_id_attr(2))
            (removed,) = [e for e in inside(elements, entry) if e.tag == "del"]
            (added,) = [e for e in inside(elements, entry) if e.tag == "ins"]
            assert removed.text == "removed: 60 (end removed)"
            assert added.text == "added: 80 (end added)"
            for run in (removed, added):
                hidden = [e for e in inside(elements, run) if e.tag == "span"]
                assert [e.attrs.get("class") for e in hidden] == ["visually-hidden"] * 2
                assert "aria-hidden" not in "".join(str(e.attrs) for e in hidden)
            assert "6080" not in entry.text.replace("removed: ", "").replace("added: ", "")
            assert "</del> <ins>" in response.text, "a real space between the two runs"
            assert "</del><ins>" not in response.text and "</ins><del>" not in response.text
            # Only there: the wording's own spacing is not doubled anywhere else.
            words = next(
                e for e in inside(elements, entry) if e.attrs.get("class") == "version-words"
            )
            assert words.text == (
                "Planned removed: 60 (end removed) added: 80 (end added) "
                "delivery routes a week with a team of 3."
            )
        css = (Path(__file__).parents[2] / "app/static/css/app.css").read_text(encoding="utf-8")
        assert ".version-words del + ins" in css, "and room between them for the eye"


def test_non_draft_merge_page_is_a_page_with_the_way_back(tmp_path: Path) -> None:
    """Item 14: a link to the merge screen of a fact that isn't a draft any more (confirmed
    since, or discarded) answers 409 inside the app's shell, says so, and links back to the
    Drafts tab; with htmx, the same as the screen's content."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        kept = seeded["draft"]
        confirmed = facts.bank.facts.confirm(kept.id, expected_version=kept.version)
        gone = discard(facts, seeded["other"])
        before = facts.snapshot()
        for view in (confirmed, gone, seeded["target"]):
            response = facts.run.client.get(merge_url(view.id))
            assert response.status_code == 409, response.status_code
            assert "<html" in response.text and facts.token in response.text, "the app's shell"
            assert response.headers.get("cache-control") == "no-store"
            elements = outline(response.text)
            main = required(elements, "main")
            assert MERGE_HEADING in [e.text for e in inside(elements, main) if e.tag == "h1"]
            assert [e for e in elements if e.tag == "nav"], "with its navigation"
            (title,) = [e.text for e in elements if e.tag == "title"]
            assert title.startswith("Error: " + MERGE_HEADING)
            notice = required(elements, MERGE_CONFLICT_ID)
            assert notice.within(main) and says(elements, MERGE_NOT_A_DRAFT, notice)
            (back,) = [a for a in inside(elements, notice) if a.tag == "a"]
            assert back.text == BACK_TO_DRAFTS
            assert href_parts(back) == (PAGE, {"tab": "drafts"}, "")
            assert not links(elements, RELOAD), "reloading would only say it again"
            assert not [e for e in inside(elements, main) if e.tag == "form"]

            fragment = facts.get(merge_url(view.id))
            assert fragment.status_code == 409 and "<html" not in fragment.text
            shown = outline(fragment.text)
            assert required(shown, MERGE_CONTENT_ID) is shown[0]
            assert says(shown, MERGE_NOT_A_DRAFT, required(shown, MERGE_CONFLICT_ID))
            assert facts.token not in fragment.text
            assert facts.run.client.head(merge_url(view.id)).status_code == 409
        assert facts.snapshot() == before


def test_refused_merge_actions_link_back_to_drafts_not_to_the_same_page(tmp_path: Path) -> None:
    """Item 14: a merge or an undo that can't be done (not a stale version) would be refused
    again after a reload, so its prompt leads to the Drafts tab. A stale version still
    offers the reload of the same comparison."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target, other = seeded["draft"], seeded["target"], seeded["other"]
        gone = discard(facts, other)
        before = facts.snapshot()

        def post(source: Any, into: Any) -> Response:
            data = {
                "target": into.id,
                "draft_version": str(source.version),
                "target_version": str(into.version),
                MERGE_CHOICE_FIELD: "target",
            }
            return facts.post(merge_url(source.id), data)

        for source, into in ((draft, draft), (draft, gone), (target, draft), (gone, target)):
            response = post(source, into)
            assert response.status_code == 409
            elements = outline(response.text)
            prompt = alert(elements, MERGE_CONFLICT_ID)
            assert FactTransitionRefused.message in prompt.text
            assert only_focus(elements) is prompt
            (back,) = [a for a in inside(elements, prompt) if a.tag == "a"]
            assert back.text == BACK_TO_DRAFTS
            assert href_parts(back) == (PAGE, {"tab": "drafts"}, "")
            assert not links(elements, RELOAD)
        # An undo with nothing to take back.
        refused = facts.post(
            undo_merge_path(draft.id), {"draft_version": str(draft.version), "target_version": "1"}
        )
        assert refused.status_code == 409
        elements = outline(refused.text)
        assert [
            a.text for a in inside(elements, alert(elements, MERGE_CONFLICT_ID)) if a.tag == "a"
        ] == [BACK_TO_DRAFTS]
        # Stale: the reload, to the same comparison.
        stale = facts.post(
            merge_url(draft.id),
            {
                "target": target.id,
                "draft_version": str(draft.version + 5),
                "target_version": str(target.version),
                MERGE_CHOICE_FIELD: "target",
            },
        )
        assert stale.status_code == 409
        elements = outline(stale.text)
        (reload,) = links(elements, RELOAD, alert(elements, MERGE_CONFLICT_ID))
        assert href_parts(reload)[:2] == (merge_url(draft.id), {"target": target.id})
        assert not links(elements, BACK_TO_DRAFTS)
        assert facts.snapshot() == before


def test_closed_drafts_have_their_own_heading_and_restore(tmp_path: Path) -> None:
    """Item 15: a discarded or merged draft has nothing to edit, and its page says what it is
    instead of "Edit fact". The discarded one offers "Restore draft" itself."""
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, INVOICES)
        facts.add_import(IMPORT_A)
        gone, merged, story = facts.drafts(
            IMPORT_A, ROUTES, DEGREE, DraftInput(type=FactType.STORY, action=ACTION)
        )
        discard(facts, gone)
        discard(facts, story)
        close_by_merge(facts, merged, target)

        for view, kind in ((gone, "discarded"), (story, "discarded"), (merged, "merged")):
            for response in (facts.run.client.get(edit_url(view.id)), facts.get(edit_url(view.id))):
                assert response.status_code == 200
                elements = outline(response.text)
                assert headings(elements, "h1") == [CLOSED_HEADINGS[kind]]
                assert not set(headings(elements, "h1")) & set(EDIT_HEADINGS.values())
                assert by_id(elements, EDITOR_ID) is None
                assert required(elements, HISTORY_ID), "its history can still be read"
            (title,) = titles(outline(facts.run.client.get(edit_url(view.id)).text))
            assert title == CLOSED_HEADINGS[kind] + TITLE_SUFFIX

        # The merged one leads to its merge; it has no Restore draft.
        elements = page_of(facts, edit_url(merged.id))
        content = required(elements, EDITOR_PAGE_ID)
        assert not buttons(elements, content, RESTORE_DRAFT)
        assert merge_url(merged.id) in [href_parts(a)[0] for a in inside(elements, content)]

        # The discarded one is restored from its own page.
        elements = page_of(facts, edit_url(gone.id))
        content = required(elements, EDITOR_PAGE_ID)
        restore = button(elements, content, RESTORE_DRAFT)
        assert [e.text for e in described_by(elements, restore)], "it says what it restores"
        path, values = submitted_by(elements, restore)
        assert path == undo_path(gone.id)
        assert values == {
            "version": str(facts.bank.facts.get(gone.id).version),
            FROM_FIELD: "editor",
        }
        form = restore.parent
        assert form is not None and form.attrs.get("hx-post") == path
        swapped = required(elements, (form.attrs.get("hx-target") or "#").lstrip("#"))
        assert swapped.within(content) and form.within(swapped)

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_RESTORED and facts.status(gone.id) == "draft"
        answer = outline(response.text)
        card = required(answer, swapped.attrs.get("id") or "")
        assert card is answer[0] and card.attrs.get("role") is None, "not a list item here"
        assert only_focus(answer).within(card) and ROUTES in card.text
        assert not [e for e in answer if e.attrs.get("hx-swap-oob")], "this page has no tabs"
        assert not buttons(answer, card, RESTORE_DRAFT)
        assert [href_parts(a) for a in inside(answer, card) if a.tag == "a"] == [
            (PAGE, {"tab": "drafts"}, fact_id_attr(gone.id))
        ]
        assert required(facts.fragment(DRAFTS_URL), fact_id_attr(gone.id))
        assert EDIT_HEADINGS["fact"] in headings(page_of(facts, edit_url(gone.id)), "h1")

        # Stale (restored elsewhere meanwhile) and plain requests lead back to this page.
        current = facts.bank.facts.get(story.id)
        sent = {"version": str(current.version + 1), FROM_FIELD: "editor"}
        stale = facts.post(undo_path(story.id), sent)
        assert stale.status_code == 409
        (reload,) = links(outline(stale.text), RELOAD)
        assert href_parts(reload) == (edit_url(story.id), {}, "")
        plain = facts.post(
            undo_path(story.id), {**sent, "version": str(current.version)}, htmx=False
        )
        assert plain.status_code == 303 and plain.headers["location"] == edit_url(story.id)
        assert facts.status(story.id) == "draft"


def test_merge_candidates_cap_hint(tmp_path: Path) -> None:
    """Item 16: more facts than the list holds: the page says that not every one is listed
    and that a search finds the rest. A short list says nothing."""
    with facts_app(tmp_path) as facts:
        draft = one_draft(facts, DEGREE)
        for number in range(PAGE_SIZE):
            typed(facts.bank, f"Planned delivery route number {number + 1}")
        listing_of = lambda page: required(page, MERGE_CANDIDATES_ID)  # noqa: E731
        page = page_of(facts, merge_url(draft.id))
        assert not says(page, MERGE_CAPPED, listing_of(page))
        typed(facts.bank, INVOICES)
        page = page_of(facts, merge_url(draft.id))
        assert says(page, MERGE_CAPPED, listing_of(page))
        assert "narrow" not in listing_of(page).text
        # The search does find it.
        found = page_of(facts, merge_url(draft.id), q="invoice")
        assert INVOICES in listing_of(found).text and not says(found, MERGE_CAPPED)


# ================================================================================================
# C. Branches that had no test
# ================================================================================================


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = open_migrated(tmp_path / "data" / "app.db", tmp_path / "backups")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def bank(db: Database, clock: FrozenClock) -> Bank:
    return make_bank(db, clock, SequentialIdFactory("fb"))


def test_create_with_new_role_skills_and_privacy_is_one_transaction(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    """Item 21: the editor's "Add" stores a role, skills, the fact, its version and its event.
    Whichever id fails, nothing of it is left behind."""
    bank.skills.add("Scheduling")
    data = FactInput(
        text=ROUTES,
        new_role=NewRole("Sample Logistics SA", "Junior Planner"),
        skills=("Scheduling", "Route planning", "Excel"),
        tags=("logistics",),
        privacy="never_use",
    )

    assert_atomic(db, clock, lambda fresh: fresh.facts.create(data))

    assert (count(db, "facts"), count(db, "roles"), count(db, "skills")) == (1, 1, 3)
    (row,) = db.connection.execute("SELECT id, role_id, privacy, status FROM facts").fetchall()
    fact_id, role_id, privacy, status = row
    assert (privacy, status) == ("never_use", "confirmed")
    assert role_row(db, role_id)["status"] == "confirmed"
    assert skill_names_of(db, fact_id) == {"Scheduling", "Route planning", "Excel"}
    assert event_types(db, fact_id) == ["created"]


def test_edit_with_new_role_skills_and_privacy_is_one_transaction(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    """Item 21: one save that rewords, types a new role, adds and removes skills and changes
    the privacy choice is all there or not at all."""
    own = typed(bank, ROUTES, skills=("Scheduling", "Excel"))
    data = FactInput(
        text=REWORDED,
        new_role=NewRole("Example Freight Lda", "Dispatcher"),
        skills=("Scheduling", "Route planning"),
        tags=("planning",),
        start_date="2022",
        privacy="generalize",
    )

    assert_atomic(
        db, clock, lambda fresh: fresh.facts.edit(own.id, data, expected_version=own.version)
    )

    row = fact_row(db, own.id)
    assert (row["privacy"], row["start_date"], row["version"]) == (
        "generalize",
        "2022",
        own.version + 1,
    )
    assert role_row(db, row["role_id"])["employer"] == "Example Freight Lda"
    assert count(db, "roles") == 1, "no role of a failed attempt is left"
    assert skill_names_of(db, own.id) == {"Scheduling", "Route planning"}
    assert [v["text"] for v in version_rows(db, own.id)] == [ROUTES, REWORDED]
    assert sorted(event_types(db, own.id)) == sorted(
        ["created", "edited", "details_changed", "privacy_changed"]
    )


def test_editor_and_merge_forms_too_large_answer_413_as_plain_text(tmp_path: Path) -> None:
    """Item 22: no form of these routes is larger than ``MAX_FORM_BYTES``. One that is
    answers 413 as plain text (nothing htmx would swap in), repeats nothing and changes
    nothing."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        archived = typed(facts.bank, NEWSLETTER)
        archived = facts.bank.facts.archive(archived.id, expected_version=archived.version)
        before = facts.snapshot()
        posts = {
            NEW_URL: {"type": "fact", "text": TOO_LARGE},
            edit_url(target.id): {"version": str(target.version), "text": TOO_LARGE},
            archive_path(target.id): {"version": str(target.version), "padding": TOO_LARGE},
            restore_path(archived.id): {"version": str(archived.version), "padding": TOO_LARGE},
            merge_url(draft.id): {
                "target": target.id,
                "draft_version": str(draft.version),
                "target_version": str(target.version),
                MERGE_CHOICE_FIELD: "edited",
                "text": TOO_LARGE,
            },
            undo_merge_path(draft.id): {"draft_version": "1", "padding": TOO_LARGE},
        }
        for path, data in posts.items():
            for htmx in (True, False):
                response = facts.post(path, data, htmx=htmx)
                assert response.status_code == 413, (path, htmx, response.status_code)
                assert response.headers["content-type"].startswith("text/plain"), path
                assert response.text.strip() and "<" not in response.text
                assert "xxxx" not in response.text, "nothing of the request is repeated"
                assert "hx-trigger" not in response.headers
        assert facts.snapshot() == before
        assert facts.status(target.id) == "confirmed" and facts.status(draft.id) == "draft"


def test_refused_merge_form_of_a_fact_that_is_no_draft_answers_409(tmp_path: Path) -> None:
    """Item 22: a merge form refused for its input (422) is shown again as the comparison,
    but a fact that isn't a draft has no comparison: the refusal becomes the 409, with the
    way back to the Drafts tab. Nothing changes."""
    with facts_app(tmp_path) as facts:
        seeded = seed(facts)
        draft, target = seeded["draft"], seeded["target"]
        gone = discard(facts, seeded["other"])
        before = facts.snapshot()
        for source in (target, gone):
            data = {
                "target": draft.id,
                "draft_version": str(source.version),
                "target_version": str(draft.version),
                MERGE_CHOICE_FIELD: "neither",
            }
            response = facts.post(merge_url(source.id), data)
            assert response.status_code == 409, (response.status_code, response.text[:300])
            assert "<html" not in response.text and "hx-trigger" not in response.headers
            elements = outline(response.text)
            prompt = alert(elements, MERGE_CONFLICT_ID)
            assert FactTransitionRefused.message in prompt.text
            assert by_id(elements, MERGE_FORM_ID) is None
            assert [a.text for a in inside(elements, prompt) if a.tag == "a"] == [BACK_TO_DRAFTS]
            plain = facts.post(merge_url(source.id), data, htmx=False)
            assert plain.status_code == 409 and "<html" in plain.text
            assert says(outline(plain.text), FactTransitionRefused.message)
            # The typed-but-not-chosen refusal takes the same way out.
            typed_in = {**data, MERGE_CHOICE_FIELD: "target", "text": "Typed wording"}
            assert facts.post(merge_url(source.id), typed_in).status_code == 409
        assert facts.snapshot() == before


# ================================================================================================
# D. Input caps
# ================================================================================================


def test_editor_caps_names_and_lists_with_friendly_refusals(tmp_path: Path) -> None:
    """Item 24: a tag or a skill name of up to 80 characters, an employer or a title of up to
    200, at most 30 tags and 60 skills. More is refused (422) on the field it was typed in,
    with what was typed kept and nothing saved; the limits themselves are accepted."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        name, role = "n" * MAX_NAME_CHARS, "r" * MAX_ROLE_CHARS
        many_tags = ", ".join(f"tag{number}" for number in range(MAX_TAGS))
        many_skills = ", ".join(f"Skill {number}" for number in range(MAX_SKILLS))
        too_much = (
            (TAGS_FIELD, name + "x"),
            (TAGS_FIELD, f"planning, {name}x"),
            (TAGS_FIELD, many_tags + ", one-more"),
            (NEW_SKILLS_FIELD, name + "x"),
            (NEW_SKILLS_FIELD, many_skills + ", One more"),
            (NEW_ROLE_FIELDS[0], role + "x"),
            (NEW_ROLE_FIELDS[1], role + "x"),
        )
        before = facts.snapshot()
        for field, value in too_much:
            for response in (
                save_new(facts, text=INVOICES, **{field: value}),
                save_edit(facts, own.id, **{field: value}),
            ):
                elements, control = assert_refused(response, field)
                assert control.attrs.get("name") == field
                messages = [
                    e
                    for e in described_by(elements, control)
                    if "field-error" in (e.attrs.get("class") or "")
                ]
                assert len(messages) == 1 and messages[0].text, field
                assert messages[0].within(editor(elements))
                said = messages[0].text
                assert any(
                    str(limit) in said
                    for limit in (MAX_NAME_CHARS, MAX_ROLE_CHARS, MAX_TAGS, MAX_SKILLS)
                )
                assert form_values(elements, editor(elements))[field] == value, "kept as typed"
                assert value not in said, "the message repeats nothing that was typed"
        assert facts.snapshot() == before, "nothing refused was saved"
        assert count_rows(facts, "skills") == 0 and count_rows(facts, "roles") == 0

        # Ticked skills count towards the 60 too: 59 new ones with one ticked is the limit.
        typed(facts.bank, NEWSLETTER, skills=("Scheduling",))
        fifty_nine = ", ".join(f"Skill {number}" for number in range(MAX_SKILLS - 1))
        over = save_edit(facts, own.id, skills=["Scheduling"], new_skills=fifty_nine + ", Extra")
        assert_refused(over, NEW_SKILLS_FIELD)

        # The limits themselves are accepted.
        response = save_edit(
            facts,
            own.id,
            tags=many_tags.replace("tag0,", name + ","),
            skills=["Scheduling"],
            new_skills=fifty_nine.replace("Skill 0,", name + ","),
            new_role_employer=f"  {role}  ",
            new_role_title=role,
        )
        assert response.status_code == 200, (response.status_code, response.text[:300])
        saved = facts.bank.facts.get(own.id)
        assert len(saved.tags) == MAX_TAGS and name in saved.tags
        assert len(saved.skills) == MAX_SKILLS
        assert role_row(facts.db, saved.role_id)["employer"] == role
        # What is saved can be saved again as the page gives it back.
        assert save_edit(facts, own.id).status_code == 200
        # The error is tied to its field on a full page as well.
        plain = save_new(facts, htmx=False, text=INVOICES, tags=name + "x")
        assert plain.status_code == 422 and "<html" in plain.text
        page = outline(plain.text)
        (marked,) = [e for e in page if e.attrs.get("aria-invalid") == "true"]
        assert marked.attrs.get("name") == TAGS_FIELD


def count_rows(facts: FactsApp, table: str) -> int:
    assert table in ("skills", "roles")
    return int(facts.run.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


def test_fact_pages_refuse_ids_of_another_shape_before_any_lookup(tmp_path: Path) -> None:
    """Item 23: a page's id is one segment of the safe shape (``is_safe_id``); anything else
    is the fixed 404 and repeats nothing."""
    with facts_app(tmp_path) as facts:
        typed(facts.bank, ROUTES)
        for unsafe in ("a.b", "x" * 65, "fictional%3Cb%3E", "seed 1", "tâche"):
            for path in (edit_url(unsafe), merge_url(unsafe)):
                for response in (facts.run.client.get(path), facts.get(path)):
                    assert response.status_code == 404, (path, response.status_code)
                    assert "fictional" not in response.text and "<html" not in response.text
        assert facts.run.client.get(edit_url("x" * 64)).status_code == 404  # safe, but unknown
