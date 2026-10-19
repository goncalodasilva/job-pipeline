"""Review fixes to the Fact bank page (M3.5): honesty of the quote's caption and the practice
provider's warning, answers that are never cached, a batch that touches only what it
confirmed, ids and copy that tell things apart, and the refresh rules of the drafts.

The contract's names live in ``tests/facts_page.py``. Real SQLite in a temp folder, the frozen
clock, the fake provider, no network; every value is fictional.
"""

import json
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path

import pytest
from httpx import Response

from app.api.forms import MAX_FORM_BYTES
from app.domain.errors import UploadNotAccepted, UploadTooLarge, VersionConflict
from app.domain.settings import AIProviderSettings
from app.services.cv_import import EXTRACT_CV_LABEL, ImportSummary
from app.services.fact_views import FactBank, FactViews
from app.services.facts import DraftInput, FactInput
from app.services.task_views import failed_message
from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import importing, start_path
from tests.fact_bank import typed
from tests.facts_page import (
    CANCEL_HINT,
    CHOOSE_ONE,
    CONFIRM,
    CONFIRM_ALL,
    DISCARD,
    DISCARDED_LINE,
    DRAFTS_CHANGED,
    DRAFTS_HINT,
    DRAFTS_ID,
    DRAFTS_URL,
    EDIT,
    EMPTY_LIBRARY,
    EMPTY_LINK,
    FILTERS_ID,
    IMPORT_ERROR_ID,
    IMPORT_FORM_ID,
    IMPORT_ID,
    IMPORT_LIST_ID,
    IMPORT_PRACTICE_ID,
    IMPORT_PRIVACY_ID,
    IMPORT_SKIP_ID,
    IMPORT_START_ID,
    IMPORT_STATUS_ID,
    IMPORTS_URL,
    LIBRARY_URL,
    NEEDS_CHECK_NOTE,
    PAGE,
    PASTE_LABEL,
    PRACTICE_WARNING,
    QUOTE_LABEL,
    QUOTE_LABEL_UNCHECKED,
    RESULTS_ID,
    REVIEW_LINK,
    SAID_IMPORT_FAILED,
    SAID_STARTED,
    STALE_NOTE,
    STALE_NOTE_ID,
    STARTED_LINE,
    UNDO,
    FactsApp,
    announcement,
    batch_hint,
    batch_id,
    button,
    buttons,
    by_id,
    confirm_all_path,
    confirm_path,
    discard_path,
    drafts_phrase,
    edit_confirm_path,
    empty_with_drafts,
    fact_id_attr,
    facts_app,
    group_id,
    href_parts,
    inside,
    links,
    named_control,
    only_focus,
    option_values,
    required,
    says,
    skip_text,
    undo_path,
    upload_id,
    upload_task_id,
)
from tests.integration.test_budget_warning_dialog import crossing, submitted_by, the_dialog
from tests.integration.test_setup_redirect import Element, outline
from tests.integration.test_setup_reopen_banner import TOKEN_KIND
from tests.upload_bodies import FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
PDF = fixture_path("single_column.pdf").read_bytes()
ROUTES = "Planned 60 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
TRAINED = "Trained new planners on the routing tool."
IMPORT_A = "imp-a"
IMPORT_B = "imp-b"
STATIC_URL = "/static/vendor/htmx.min.js"
TASK_SENTENCE = "CV import failed. Open Tasks to see why."
MIB = 1024 * 1024


def flagged(text: str) -> DraftInput:
    return DraftInput(text=text, original_answer=text, needs_check=True)


def drafts_region(facts: FactsApp) -> list[Element]:
    return facts.fragment(DRAFTS_URL)


def triggers(response: Response) -> dict[str, object]:
    """The events an answer fires on the body (``HX-Trigger``), or none."""
    if "hx-trigger" not in response.headers:
        return {}
    fired = json.loads(response.headers["hx-trigger"])
    assert isinstance(fired, dict), fired
    return fired


def stamp(facts: FactsApp) -> str:
    """The stamp the drafts on the page were built from, as the list sends it back."""
    field = required(drafts_region(facts), "fact-drafts-stamp")
    assert field.attrs.get("name") == "drafts_seen"
    return field.attrs.get("value") or ""


def ancestors(element: Element) -> list[Element]:
    found = []
    node = element.parent
    while node is not None:
        found.append(node)
        node = node.parent
    return found


def position(elements: list[Element], element: Element) -> int:
    return next(index for index, found in enumerate(elements) if found is element)


# --- honesty -----------------------------------------------------------------------------------


def test_flagged_draft_quote_is_not_called_from_your_cv(tmp_path: Path) -> None:
    """A draft that needs a check shows what the AI quoted, which nothing confirmed: its
    caption never says "From your CV"."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        fine, doubtful = facts.drafts(IMPORT_A, ROUTES, flagged(TRAINED))
        for elements in (facts.tab("drafts"), drafts_region(facts)):
            block = required(elements, fact_id_attr(doubtful.id))
            assert says(elements, QUOTE_LABEL_UNCHECKED, block)
            assert not says(elements, QUOTE_LABEL, block), "an unchecked quote isn't the CV's"
            assert QUOTE_LABEL not in block.text.replace(QUOTE_LABEL_UNCHECKED, "")
            assert says(elements, NEEDS_CHECK_NOTE, block), "the note keeps its words"
            block = required(elements, fact_id_attr(fine.id))
            assert says(elements, QUOTE_LABEL, block)
            assert not says(elements, QUOTE_LABEL_UNCHECKED, block)
        assert says(facts.tab("drafts"), DRAFTS_HINT)

        # The same in the block an action answers with (undo shows the draft again).
        facts.act(discard_path(doubtful.id), doubtful.id)
        restored = outline(facts.act(undo_path(doubtful.id), doubtful.id).text)
        block = required(restored, fact_id_attr(doubtful.id))
        assert says(restored, QUOTE_LABEL_UNCHECKED, block)
        assert not says(restored, QUOTE_LABEL, block)


def test_practice_provider_warning_shown_only_for_the_fake(tmp_path: Path) -> None:
    with facts_app(tmp_path / "fake") as facts:
        assert facts.run.app.state.fact_views.practice_provider() is True
        for elements in (facts.tab("drafts"), outline(facts.run.paste(CV_TEXT, htmx=True).text)):
            section = required(elements, IMPORT_ID)
            warning = required(elements, IMPORT_PRACTICE_ID)
            assert warning.within(required(elements, IMPORT_FORM_ID))
            assert warning.text == PRACTICE_WARNING
            start = required(elements, IMPORT_START_ID)
            assert position(elements, warning) < position(elements, start), "above the button"
            described = (start.attrs.get("aria-describedby") or "").split()
            assert IMPORT_PRACTICE_ID in described and IMPORT_PRIVACY_ID in described
            assert warning.within(section)

    real = AIProviderSettings(kind=TOKEN_KIND)
    with importing(tmp_path / "real", provider=real) as run:
        assert run.app.state.fact_views.practice_provider() is False
        page = run.client.get(PAGE, params={"tab": "drafts"})
        assert page.status_code == 200, page.status_code
        elements = outline(page.text)
        assert by_id(elements, IMPORT_PRACTICE_ID) is None
        assert PRACTICE_WARNING not in page.text
        start = required(elements, IMPORT_START_ID)
        assert IMPORT_PRACTICE_ID not in (start.attrs.get("aria-describedby") or "").split()


# --- security and privacy ----------------------------------------------------------------------


def test_fact_fragments_and_answers_are_never_cached(tmp_path: Path) -> None:
    """Fragments and POST answers hold the user's own text: ``Cache-Control: no-store`` on
    each, as on the page. Static files stay cacheable."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        one, two, three = facts.drafts(IMPORT_A, ROUTES, INVOICES, DEGREE)
        answers = {
            "page": facts.get(PAGE, htmx=False, tab="drafts"),
            "page fragment": facts.get(PAGE, tab="library"),
            "library": facts.get(LIBRARY_URL, q="routes"),
            "drafts": facts.get(DRAFTS_URL),
            "imports": facts.get(IMPORTS_URL),
            "confirm": facts.act(confirm_path(one.id), one.id),
            "discard": facts.act(discard_path(two.id), two.id),
            "plain post": facts.act(confirm_path(three.id), three.id, htmx=False),
            "stale": facts.post(confirm_path(one.id), {"version": "99"}),
            "unknown": facts.post(confirm_path("nobody"), {"version": "1"}),
            "refused upload": facts.run.post_body(multipart_body(FieldPart(" ")), htmx=True),
            "no token": facts.run.client.get(DRAFTS_URL),
        }
        expected = {
            "page": 200,
            "page fragment": 200,
            "library": 200,
            "drafts": 200,
            "imports": 200,
            "confirm": 200,
            "discard": 200,
            "plain post": 303,
            "stale": 409,
            "unknown": 404,
            "refused upload": 400,
            "no token": 403,
        }
        for name, response in answers.items():
            assert response.status_code == expected[name], (name, response.status_code)
            assert response.headers.get_list("cache-control") == ["no-store"], name

        static = facts.run.client.get(STATIC_URL)
        assert static.status_code == 200
        assert "no-store" not in (static.headers.get("cache-control") or "")


def test_oversized_start_form_answers_plain_text(tmp_path: Path) -> None:
    """The backstop for a form too large to be a start is text, never an empty HTML answer
    that could replace the import section."""
    with facts_app(tmp_path) as facts:
        facts.add_import("imp-kept", state="uploaded")
        huge = {"fictional": "x" * (MAX_FORM_BYTES + 1)}
        for htmx in (True, False):
            response = facts.post(start_path("imp-kept"), huge, htmx=htmx)
            assert response.status_code == 413, response.status_code
            assert response.headers["content-type"].startswith("text/plain")
            assert "<" not in response.text
        assert facts.run.tasks() == []


# --- correctness -------------------------------------------------------------------------------


def test_drafts_read_the_uploads_before_the_facts(tmp_path: Path) -> None:
    """An import that finishes while the drafts are being read must not be counted as shown
    without its drafts: the kept uploads (the stamp) are read first, so the worst case is
    one more refetch."""
    with facts_app(tmp_path) as facts:
        state = facts.run.app.state
        finished: list[str] = []

        def imports() -> Sequence[ImportSummary]:
            if not finished:  # the import finishes right when the uploads are first read
                finished.append(facts.add_import(IMPORT_A))
                facts.drafts(IMPORT_A, ROUTES)
            found: Sequence[ImportSummary] = state.cv_import.imports()
            return found

        views = FactViews(
            bank=lambda: FactBank(
                facts.db, state.facts, state.fact_library, state.roles, state.skills
            ),
            imports=imports,
            latest_task=lambda _import_id: None,
            provider_kind=lambda: "fake",
            import_failed_said=TASK_SENTENCE,
            tz=UTC,
        )

        shown = views.drafts()

        assert finished == [IMPORT_A]
        listed = [item.text for group in shown.groups for item in group.items]
        if shown.stamp == views.drafts_stamp():  # the page counts as up to date
            assert listed == [ROUTES], "then the finished import's drafts are on it"


def test_upload_row_task_has_an_id_of_its_own(tmp_path: Path) -> None:
    """The tasks panel shows the same task as ``task-<id>``: the upload's row must not."""
    with facts_app(tmp_path) as facts:
        run = facts.run
        run.hold.hold = True
        assert run.paste(CV_TEXT, htmx=True).status_code == 200
        task = run.only_task()
        import_id = run.only_import()["id"]
        run.wait_entered(1)
        for elements in (facts.tab("drafts"), facts.fragment(IMPORTS_URL)):
            row = required(elements, upload_id(import_id))
            status = required(elements, upload_task_id(import_id))
            assert status.within(row)
            listing = required(elements, IMPORT_LIST_ID)
            assert not [
                e for e in inside(elements, listing) if e.attrs.get("id") == f"task-{task.id}"
            ]
            # While it runs the row says how to cancel: it has no button for that.
            assert says(elements, CANCEL_HINT, row)
            assert not [e for e in inside(elements, row) if e.tag == "button"]
        run.hold.open()
        run.wait_terminal(task.id)
        done = facts.fragment(IMPORTS_URL)
        assert not says(done, CANCEL_HINT, required(done, upload_id(import_id)))


def test_reword_ignores_fields_the_draft_does_not_show(tmp_path: Path) -> None:
    """A fact's inline form has ``text`` only: a forged story part is not saved with it."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        shown = facts.tab("drafts")
        block = required(shown, fact_id_attr(draft.id))
        areas = {e.attrs.get("name") for e in inside(shown, block) if e.tag == "textarea"}
        assert areas == {"text"}

        response = facts.act(
            edit_confirm_path(draft.id),
            draft.id,
            text="Planned 60 delivery routes a week.",
            situation="Forged situation",
            action="Forged action",
            result="Forged result",
        )

        assert response.status_code in (200, 422), response.status_code
        saved = facts.bank.facts.get(draft.id)
        content = saved.content
        assert (content.situation, content.action, content.result) == (None, None, None)
        assert "Forged" not in facts.snapshot()
        if response.status_code == 200:
            assert content.text == "Planned 60 delivery routes a week."
            assert facts.status(draft.id) == "confirmed"
        else:
            assert content.text == ROUTES and facts.status(draft.id) == "draft"


def test_batch_answer_touches_only_what_it_confirmed(tmp_path: Path) -> None:
    """The batch's answer is its own wrapper plus the removal of the drafts it confirmed: a
    discarded draft of the same import (its Undo is on the page) and a draft that needs a
    check are not in it, so the page keeps them as they are."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        routes, invoices, degree = facts.drafts(IMPORT_A, ROUTES, INVOICES, DEGREE)
        (doubtful,) = facts.drafts(IMPORT_A, flagged(TRAINED))
        discarded = outline(facts.act(discard_path(degree.id), degree.id).text)
        undo = button(discarded, required(discarded, fact_id_attr(degree.id)), UNDO)
        undo_form = next(e for e in ancestors(undo) if e.tag == "form")
        assert "data-holds-refresh" in undo_form.attrs, "app.js holds the refresh for it"

        shown = facts.tab("drafts")
        group = required(shown, group_id(IMPORT_A))
        confirm_all = button(shown, group, CONFIRM_ALL)
        form = next(e for e in ancestors(confirm_all) if e.tag == "form")
        assert form.attrs.get("hx-target") == f"#{batch_id(IMPORT_A)}"
        assert "data-skips-open-edits" in form.attrs
        # A field that would confirm a draft names its block, so app.js can leave out one
        # that is being reworded; a flagged draft's field names none (it is never confirmed).
        names = {
            e.attrs.get("name"): e.attrs.get("data-confirms")
            for e in inside(shown, form)
            if e.tag == "input"
        }
        assert names == {
            f"version:{routes.id}": fact_id_attr(routes.id),
            f"version:{invoices.id}": fact_id_attr(invoices.id),
            f"version:{doubtful.id}": None,
        }

        response = facts.post(*submitted_by(shown, confirm_all))

        assert response.status_code == 200
        answer = outline(response.text)
        assert required(answer, batch_id(IMPORT_A)) is answer[0]
        blocks = {
            e.attrs.get("id"): e.attrs.get("hx-swap-oob")
            for e in answer
            if (e.attrs.get("id") or "").startswith("fact-")
        }
        assert blocks == {fact_id_attr(routes.id): "delete", fact_id_attr(invoices.id): "delete"}
        assert not buttons(answer, answer[0], UNDO)
        assert facts.status(degree.id) == "discarded" and facts.status(doubtful.id) == "draft"
        # The Undo the page still shows works after the batch.
        assert facts.post(*submitted_by(discarded, undo)).status_code == 200
        assert facts.status(degree.id) == "draft"

        # A draft left out of the request (app.js does that for one being reworded) is
        # left a draft: the batch confirms exactly what it was sent.
        late_a, late_b = facts.drafts(IMPORT_A, "English", "Portuguese")
        response = facts.confirm_all(IMPORT_A, [late_a.id])
        assert response.status_code == 200
        assert facts.status(late_a.id) == "confirmed" and facts.status(late_b.id) == "draft"
        # The wrapper that came back still offers the rest.
        answer = outline(response.text)
        again = button(answer, required(answer, batch_id(IMPORT_A)), CONFIRM_ALL)
        assert f"version:{late_b.id}" in submitted_by(answer, again)[1]


def test_batch_conflict_offers_no_confirm_all(tmp_path: Path) -> None:
    """A refused batch shows the reload prompt alone: the form beside it would send the same
    stale versions again."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        routes, _invoices = facts.drafts(IMPORT_A, ROUTES, INVOICES)
        shown = facts.tab("drafts")
        batch = submitted_by(shown, button(shown, required(shown, group_id(IMPORT_A)), CONFIRM_ALL))
        facts.bank.facts.edit(
            routes.id, FactInput(text="Planned routes."), expected_version=routes.version
        )

        response = facts.post(*batch)

        assert response.status_code == 409
        answer = outline(response.text)
        wrapper = required(answer, batch_id(IMPORT_A))
        assert wrapper is answer[0], "the prompt takes the form's place"
        (prompt,) = [e for e in answer if e.attrs.get("role") == "alert"]
        assert prompt.within(wrapper) and VersionConflict.message in prompt.text
        assert only_focus(answer) is prompt
        assert not [e for e in answer if e.tag in ("button", "form")]
        assert not [e for e in answer if e.attrs.get("hx-swap-oob") == "delete"]


# --- UX ----------------------------------------------------------------------------------------


def test_confirm_all_follows_the_drafts_and_says_what_it_confirms(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        facts.add_import(IMPORT_B)
        in_a = facts.drafts(IMPORT_A, ROUTES, INVOICES, flagged(TRAINED))
        (only,) = facts.drafts(IMPORT_B, DEGREE)

        for elements in (facts.tab("drafts"), drafts_region(facts)):
            for import_id, drafts, count in ((IMPORT_A, in_a, 2), (IMPORT_B, [only], 1)):
                group = required(elements, group_id(import_id))
                confirm_all = button(elements, group, CONFIRM_ALL)
                last = max(
                    position(elements, required(elements, fact_id_attr(view.id))) for view in drafts
                )
                assert position(elements, confirm_all) > last, "after the group's drafts"
                described = [
                    required(elements, i)
                    for i in (confirm_all.attrs.get("aria-describedby") or "").split()
                ]
                hint = batch_hint(count)
                assert hint in [e.text for e in described], [e.text for e in described]
                sentence = next(e for e in described if e.text == hint)
                assert sentence.within(group)
                assert position(elements, sentence) < position(elements, confirm_all)
        assert batch_hint(1) == (
            "Confirms the 1 draft in this import that needs no check. "
            "Drafts marked 'Needs a check' are left for you."
        )
        assert batch_hint(4).startswith("Confirms the 4 drafts in this import that need no check.")

        # Nothing it would confirm: no button and no sentence, in the page and in the answer
        # to the action that left it so.
        answer = outline(facts.act(confirm_path(only.id), only.id).text)
        wrapper = required(answer, batch_id(IMPORT_B))
        assert wrapper.attrs.get("hx-swap-oob") == "true"
        assert not buttons(answer, wrapper, CONFIRM_ALL) and "Confirms" not in wrapper.text
        for view in in_a[:2]:
            facts.act(confirm_path(view.id), view.id)
        elements = drafts_region(facts)
        group = required(elements, group_id(IMPORT_A))
        assert not buttons(elements, group, CONFIRM_ALL), "only a flagged draft is left"
        assert "Confirms" not in group.text
        button(elements, group, CONFIRM)


def test_group_headings_count_drafts_and_same_minute_uploads_differ(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        # Two uploads in the same minute, a third in another.
        for import_id, moment in (
            ("imp-one", "2026-03-05T09:15:07+00:00"),
            ("imp-two", "2026-03-05T09:15:42+00:00"),
            ("imp-three", "2026-03-05T09:20:00+00:00"),
        ):
            facts.add_import(import_id)
            facts.run.execute(
                "UPDATE cv_imports SET created_at = ? WHERE id = ?", (moment, import_id)
            )
        one = facts.drafts("imp-one", ROUTES, INVOICES, DEGREE, "English")
        facts.drafts("imp-two", TRAINED)
        facts.drafts("imp-three", "Portuguese")

        elements = facts.tab("drafts")
        headings = {
            name: required(elements, f"{group_id(name)}-heading").text
            for name in ("imp-one", "imp-two", "imp-three")
        }
        labels = {
            name: required(elements, f"{upload_id(name)}-label").text
            for name in ("imp-one", "imp-two", "imp-three")
        }
        assert len(set(labels.values())) == 3, labels
        assert ":07" in labels["imp-one"] and ":42" in labels["imp-two"], labels
        assert not labels["imp-three"].endswith(":00"), "seconds only where they are needed"
        assert headings["imp-one"] == f"{labels['imp-one']} · {drafts_phrase(4)}"
        assert headings["imp-two"] == f"{labels['imp-two']} · 1 draft"
        assert headings["imp-three"] == f"{labels['imp-three']} · 1 draft"

        # The count follows an action on one draft (the heading is swapped out of band).
        answer = outline(facts.act(confirm_path(one[0].id), one[0].id).text)
        heading = required(answer, f"{group_id('imp-one')}-heading")
        assert heading.attrs.get("hx-swap-oob") == "true"
        assert heading.text == f"{labels['imp-one']} · {drafts_phrase(3)}"
        # And the batch.
        answer = outline(facts.confirm_all("imp-one", [view.id for view in one[1:]]).text)
        heading = required(answer, f"{group_id('imp-one')}-heading")
        assert heading.attrs.get("hx-swap-oob") == "true"
        assert heading.text == f"{labels['imp-one']} · {drafts_phrase(0)}"


def test_started_line_says_where_the_result_will_be(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.run.hold.hold = True
        answer = facts.run.paste(CV_TEXT, htmx=True)
        assert answer.status_code == 200
        elements = outline(answer.text)
        line = required(elements, IMPORT_STATUS_ID)
        assert line.text == STARTED_LINE and only_focus(elements) is line
        assert announcement(answer) == SAID_STARTED, "the live region gets the short sentence"
        facts.run.hold.open()
        facts.run.wait_terminal(facts.run.only_task().id)

        # A failure's reason is in the upload's row, so the page says that instead of the
        # task indicator's "Open Tasks to see why": both sentences are fixed, from the server.
        for shown in (facts.tab("drafts"), elements):
            section = required(shown, IMPORT_ID)
            assert section.attrs.get("data-announce-instead-of") == TASK_SENTENCE
            assert section.attrs.get("data-announce-instead") == SAID_IMPORT_FAILED
        assert "Open Tasks" not in SAID_IMPORT_FAILED
        # The sentence the page replaces is the one the indicator sends for a failed import.
        assert failed_message(EXTRACT_CV_LABEL) == TASK_SENTENCE


def test_library_empty_state_with_drafts_links_to_them(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        facts.drafts(IMPORT_A, ROUTES)
        for count in (1, 2):
            if count == 2:
                facts.drafts(IMPORT_A, INVOICES)
            sentence = empty_with_drafts(count)
            for elements in (facts.tab("library"), facts.fragment(LIBRARY_URL)):
                results = required(elements, RESULTS_ID)
                assert says(elements, sentence, results)
                assert not says(elements, EMPTY_LIBRARY, results)
                assert not links(elements, EMPTY_LINK, results)
                (link,) = [e for e in inside(elements, results) if e.tag == "a"]
                path, query, _ = href_parts(link)
                assert (path, query) == (PAGE, {"tab": "drafts"})
            assert announcement(facts.get(LIBRARY_URL)) == sentence
        assert empty_with_drafts(1) == "No confirmed facts yet. You have 1 draft to review."
        assert empty_with_drafts(2) == "No confirmed facts yet. You have 2 drafts to review."
        # The form sends the default status with every search: that is no filter, so the
        # empty state stays the same.
        elements = facts.fragment(LIBRARY_URL, status="confirmed")
        assert says(elements, empty_with_drafts(2), required(elements, RESULTS_ID))


def test_file_and_text_together_refused_in_the_pages_words(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        run = facts.run
        both = multipart_body(FilePart("cv.pdf", PDF), FieldPart(CV_TEXT))
        response = run.post_body(both, htmx=True)
        assert response.status_code == 400
        elements = outline(response.text)
        assert required(elements, IMPORT_ID) is elements[0]
        error = required(elements, IMPORT_ERROR_ID)
        assert error.text == CHOOSE_ONE and only_focus(elements) is error
        assert UploadNotAccepted.message not in response.text
        run.assert_nothing_stored()
        # Any other refusal keeps the error's own message, and so does a plain request.
        two = multipart_body(FilePart("a.pdf", PDF), FilePart("b.pdf", PDF))
        answer = outline(run.post_body(two, htmx=True).text)
        assert required(answer, IMPORT_ERROR_ID).text == UploadNotAccepted.message
        plain = run.post_body(both)
        assert plain.status_code == 400 and plain.text == UploadNotAccepted.message
        run.assert_nothing_stored()

        # The paste field's label says to leave the file empty.
        elements = facts.tab("drafts")
        text = named_control(elements, required(elements, IMPORT_FORM_ID), "text")
        (label,) = [
            e for e in elements if e.tag == "label" and e.attrs.get("for") == text.attrs.get("id")
        ]
        assert label.text == PASTE_LABEL


def test_edit_sits_with_confirm_and_discard_and_names_its_draft(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        for elements in (facts.tab("drafts"), drafts_region(facts)):
            block = required(elements, fact_id_attr(draft.id))
            (edit,) = [e for e in inside(elements, block) if e.tag == "summary" and e.text == EDIT]
            (wording_id,) = (edit.attrs.get("aria-describedby") or "").split()
            wording = required(elements, wording_id)
            assert wording.within(block) and ROUTES in wording.text
            rows = [
                {id(node) for node in ancestors(control) if node.within(block)}
                for control in (
                    edit,
                    button(elements, block, CONFIRM),
                    button(elements, block, DISCARD),
                )
            ]
            assert rows[0] & rows[1] & rows[2], "one row holds Confirm, Discard and Edit"


def test_skip_to_drafts_link_under_the_import_heading(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        elements = facts.tab("drafts")
        assert not [e for e in elements if e.tag == "a" and e.text.startswith("Skip to drafts")]
        skip = required(elements, IMPORT_SKIP_ID)
        assert "hidden" in skip.attrs and not skip.text

        facts.add_import(IMPORT_A)
        one, two = facts.drafts(IMPORT_A, ROUTES, INVOICES)
        elements = facts.tab("drafts")
        section = required(elements, IMPORT_ID)
        (link,) = links(elements, skip_text(2), section)
        assert link.attrs.get("href") == f"#{DRAFTS_ID}"
        heading = required(elements, (section.attrs.get("aria-labelledby") or "").split()[0])
        form = required(elements, IMPORT_FORM_ID)
        assert position(elements, heading) < position(elements, link) < position(elements, form)
        assert "hidden" not in required(elements, IMPORT_SKIP_ID).attrs

        # The count follows the drafts: an action's answer replaces the link out of band.
        answer = outline(facts.act(confirm_path(one.id), one.id).text)
        skip = required(answer, IMPORT_SKIP_ID)
        assert skip.attrs.get("hx-swap-oob") == "true" and skip.text == skip_text(1)
        answer = outline(facts.confirm_all(IMPORT_A, [two.id]).text)
        skip = required(answer, IMPORT_SKIP_ID)
        assert skip.attrs.get("hx-swap-oob") == "true"
        assert "hidden" in skip.attrs and not skip.text
        answer = facts.fragment(DRAFTS_URL)
        assert required(answer, IMPORT_SKIP_ID).attrs.get("hx-swap-oob") == "true"


def test_status_filter_has_no_duplicate_and_library_draft_leads_to_review(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        typed(facts.bank, ROUTES)
        facts.add_import(IMPORT_A)
        fine, doubtful = facts.drafts(IMPORT_A, DEGREE, flagged(TRAINED))

        elements = facts.tab("library")
        select = named_control(elements, required(elements, FILTERS_ID), "status")
        statuses = option_values(elements, select)
        assert "" not in statuses, "no second option for what Confirmed lists"
        assert statuses["confirmed"] == "Confirmed"
        assert len(set(statuses.values())) == len(statuses)
        chosen = [
            e.attrs.get("value")
            for e in inside(elements, select)
            if e.tag == "option" and "selected" in e.attrs
        ]
        assert chosen == ["confirmed"], "Confirmed is what the Library shows by default"
        results = required(elements, RESULTS_ID)
        assert ROUTES in results.text and DEGREE not in results.text

        for elements in (
            facts.tab("library", status="draft"),
            facts.fragment(LIBRARY_URL, status="draft"),
        ):
            for view, noted in ((fine, False), (doubtful, True)):
                block = required(elements, fact_id_attr(view.id))
                assert says(elements, NEEDS_CHECK_NOTE, block) is noted
                (link,) = links(elements, REVIEW_LINK, block)
                path, query, anchor = href_parts(link)
                assert (path, query, anchor) == (PAGE, {"tab": "drafts"}, fact_id_attr(view.id))
                assert not [e for e in inside(elements, block) if e.tag == "button"]
        # A confirmed fact has neither.
        elements = facts.tab("library")
        assert not links(elements, REVIEW_LINK) and not says(elements, NEEDS_CHECK_NOTE)


def test_discarded_line_and_stale_note_copy(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        for elements in (facts.tab("drafts"), drafts_region(facts)):
            note = required(elements, STALE_NOTE_ID)
            assert note.text == STALE_NOTE
            assert "hidden" in note.attrs and "data-stale-note" in note.attrs
            (reload,) = [e for e in inside(elements, note) if e.tag == "a"]
            assert href_parts(reload)[:2] == (PAGE, {"tab": "drafts"})
        answer = outline(facts.act(discard_path(draft.id), draft.id).text)
        assert says(answer, DISCARDED_LINE, required(answer, fact_id_attr(draft.id)))


# --- behaviour that was untested ---------------------------------------------------------------


def test_imports_list_fires_drafts_changed_only_when_the_page_is_behind(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        facts.drafts(IMPORT_A, ROUTES)
        old = stamp(facts)
        region = required(drafts_region(facts), DRAFTS_ID)
        assert region.attrs.get("hx-trigger") == f"{DRAFTS_CHANGED} from:body"
        listing = required(facts.fragment(IMPORTS_URL), IMPORT_LIST_ID)
        assert listing.attrs.get("hx-include") == "#fact-drafts-stamp"

        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL, drafts_seen=old))
        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL)), "no stamp, nothing to say"

        # Another import finishes: the page that still has the old stamp is told, once it
        # has the new one it isn't.
        facts.add_import(IMPORT_B)
        facts.drafts(IMPORT_B, DEGREE)
        behind = facts.get(IMPORTS_URL, drafts_seen=old)
        assert behind.status_code == 200 and DRAFTS_CHANGED in triggers(behind)
        assert "announce" not in triggers(behind), "a refresh says nothing"
        current = stamp(facts)
        assert current != old
        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL, drafts_seen=current))
        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL))
        # A stamp that never was one is an old one.
        assert DRAFTS_CHANGED in triggers(facts.get(IMPORTS_URL, drafts_seen="fictional"))
        # An upload still to be imported changes no drafts.
        facts.add_import("imp-kept", state="uploaded")
        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL, drafts_seen=current))

        # A removal fires it itself, and leaves the page behind until it refetches.
        removed = facts.post(f"/facts/import/{IMPORT_B}/remove")
        assert removed.status_code == 200 and DRAFTS_CHANGED in triggers(removed)
        assert DRAFTS_CHANGED in triggers(facts.get(IMPORTS_URL, drafts_seen=current))
        assert DRAFTS_CHANGED not in triggers(facts.get(IMPORTS_URL, drafts_seen=stamp(facts)))


def test_refused_upload_answers_the_section_with_the_reason_focused(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        run = facts.run
        refusals = {
            400: (multipart_body(FieldPart("fictional", name="other")), UploadNotAccepted.message),
            413: (
                multipart_body(FilePart("cv.pdf", b"%PDF-1.4\n" + b"x" * (11 * MIB))),
                UploadTooLarge.message,
            ),
        }
        for status, (body, message) in refusals.items():
            response = run.post_body(body, htmx=True)
            assert response.status_code == status, (status, response.status_code)
            assert response.headers["content-type"].startswith("text/html")
            assert "<html" not in response.text and facts.token not in response.text
            elements = outline(response.text)
            section = required(elements, IMPORT_ID)
            assert section is elements[0], "the answer is the import section"
            assert "data-shows-refusals" in section.attrs, "app.js lets it be shown"
            error = required(elements, IMPORT_ERROR_ID)
            assert error.text == message and error.within(section)
            assert only_focus(elements) is error and error.attrs.get("tabindex") == "-1"
            form = required(elements, IMPORT_FORM_ID)
            for name in ("file", "text"):
                control = named_control(elements, form, name)
                assert IMPORT_ERROR_ID in (control.attrs.get("aria-describedby") or "").split()
            assert "hx-trigger" not in response.headers, "nothing started"
            run.assert_nothing_stored()


def test_budget_warning_answers_the_section_with_form_and_dialog(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        run = facts.run
        crossing(run, 9)  # the next request reaches the first warning

        shown = run.paste(CV_TEXT, htmx=True)

        elements, dialog = the_dialog(shown)
        section = required(elements, IMPORT_ID)
        assert section is elements[0], "the answer replaces #cv-import"
        assert dialog.within(section), "the dialog comes inside the section"
        form = required(elements, IMPORT_FORM_ID)
        assert form.within(section) and not dialog.within(form)
        assert required(elements, IMPORT_START_ID).within(form), "the form is still there"
        assert dialog.attrs.get("data-return-focus") == f"#{IMPORT_START_ID}"
        import_id = run.only_import()["id"]
        assert required(elements, upload_id(import_id)).within(section), "the upload is kept"
        assert run.tasks() == [] and run.spy.requests == [], "a warning starts nothing"
        assert "announce" not in triggers(shown)


def test_fact_routes_without_an_open_workspace_answer_cleanly(tmp_path: Path) -> None:
    """Before setup there is no workspace: every fact route, token and origin included,
    gets what the setup guard gives, never a server error."""
    with importing(tmp_path, seed=False) as run:
        gets = (PAGE, LIBRARY_URL, DRAFTS_URL, IMPORTS_URL)
        posts = (
            confirm_path("nobody"),
            edit_confirm_path("nobody"),
            discard_path("nobody"),
            undo_path("nobody"),
            confirm_all_path("imp-nobody"),
            start_path("imp-nobody"),
            "/facts/import/imp-nobody/remove",
        )
        for path in gets:
            for headers in (run.run.with_token(), run.run.htmx()):
                response = run.client.get(path, headers=headers)
                assert response.status_code < 500, (path, response.status_code)
                assert "Traceback" not in response.text
        for path in posts:
            for headers in (run.run.action(), run.run.action(**{"HX-Request": "true"})):
                response = run.client.post(path, data={"version": "1"}, headers=headers)
                assert response.status_code < 500, (path, response.status_code)
                assert "Traceback" not in response.text
        upload = run.paste(CV_TEXT, htmx=True)
        assert upload.status_code < 500, upload.status_code
        assert run.stored_files() == []
