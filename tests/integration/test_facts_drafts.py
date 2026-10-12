"""The Fact bank's Drafts to review tab and the CV import form (M3.5; AC-M3.1, AC-M3.2).

The tab holds the import section (``#cv-import``: file or pasted text, the notice that the CV's
text goes to the chosen AI provider, and the kept uploads with Start or Try again and Remove,
each with its task's status) and the drafts (``#fact-drafts``), grouped by import with the CV
quote beside each one. A draft is confirmed, reworded then confirmed, or discarded with undo;
"Confirm all from this import" leaves the drafts that need a check and says how many. A stale
draft answers 409 with the reload prompt. Errors: ``FactNotFound`` 404, ``VersionConflict``
and ``FactTransitionRefused`` 409, ``FactInputInvalid`` 422 tied to its field. The contract's
names live in ``tests/facts_page.py``. Real SQLite in a temp folder, the frozen clock, the fake
provider, no network; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.api.facts_import import IMPORT_FOCUS, IMPORT_TARGET, MSG_REMOVED
from app.domain.errors import (
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    InvalidModelOutput,
    VersionConflict,
)
from app.domain.facts import FactType
from app.modules.ai.providers import fake_scripts
from app.services.facts import DraftInput, DraftRole, FactInput
from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import remove_path, start_path
from tests.fact_bank import current_version_row, event_types, typed, version_rows
from tests.facts_page import (
    CONFIRM,
    CONFIRM_ALL,
    DISCARD,
    DRAFTS_ID,
    DRAFTS_URL,
    EDIT,
    IMPORT,
    IMPORT_FORM_ID,
    IMPORT_ID,
    IMPORT_LIST_ID,
    IMPORT_PRIVACY_ID,
    IMPORT_START_ID,
    IMPORT_URL,
    IMPORTS_URL,
    NEEDS_CHECK_NOTE,
    NO_DRAFTS,
    PAGE,
    PRACTICE_BADGE,
    QUOTE_LABEL,
    QUOTE_LABEL_UNCHECKED,
    RELOAD,
    REMOVE,
    SAID_CONFIRMED,
    SAID_DISCARDED,
    SAID_EDITED,
    SAID_RESTORED,
    SAID_STARTED,
    SAVE_CONFIRM,
    START,
    TRY_AGAIN,
    UNDO,
    FactsApp,
    announcement,
    batch_field,
    batch_id,
    batch_said,
    button,
    buttons,
    by_id,
    confirm_all_path,
    confirm_path,
    discard_path,
    drafts_tab_text,
    edit_confirm_path,
    fact_id_attr,
    fact_ids_shown,
    facts_app,
    group_id,
    has_label,
    href_parts,
    inside,
    links,
    named_control,
    only_focus,
    required,
    says,
    undo_path,
    upload_id,
    upload_task_id,
)
from tests.integration.test_budget_warning_dialog import submitted_by
from tests.integration.test_setup_redirect import Element, outline

pytestmark = pytest.mark.integration

CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
ROUTES = "Planned 60 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
TRAINED = "Trained new planners on the routing tool."
REWORDED = "Planned 60 delivery routes a week."
SCRIPT_QUOTE = "<script>window.fictional=1</script> Planned 60 delivery routes a week"
SCRIPT_TEXT = '<img src=x onerror="window.fictional=2"> Planned 60 delivery routes'
IMPORT_A = "imp-a"
IMPORT_B = "imp-b"


# --- helpers -----------------------------------------------------------------------------------


def drafts_region(facts: FactsApp) -> list[Element]:
    elements = facts.fragment(DRAFTS_URL)
    assert required(elements, DRAFTS_ID) is elements[0], "the fragment is #fact-drafts"
    return elements


def assert_draft_answer(
    facts: FactsApp, response: Response, fact_id: str, said: str, drafts_left: int
) -> tuple[list[Element], Element]:
    """An htmx answer to a draft action: a fragment with the fact's block, one focus target
    inside it, the announcement and the tab link with the new count."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "<html" not in response.text, "an htmx answer is a fragment"
    assert facts.token not in response.text
    elements = outline(response.text)
    block = required(elements, fact_id_attr(fact_id))
    focus = only_focus(elements)
    assert focus is block or focus.within(block), "focus stays with the draft acted on"
    assert announcement(response) == said
    assert links(elements, drafts_tab_text(drafts_left)), "the tab link carries the new count"
    return elements, block


def assert_reload_prompt(response: Response, prompt_id: str, message: str) -> None:
    """A 409 inside the block it belongs to: one alert with the message and a plain link
    that loads the Drafts tab again; the alert takes focus."""
    assert response.status_code == 409, (response.status_code, response.text[:300])
    assert "<html" not in response.text
    elements = outline(response.text)
    alerts = [e for e in elements if e.attrs.get("role") == "alert"]
    assert len(alerts) == 1, f"expected one alert, found {len(alerts)}"
    prompt = alerts[0]
    assert prompt.attrs.get("id") == prompt_id, prompt.attrs
    assert says(elements, message, prompt), prompt.text
    (reload,) = links(elements, RELOAD, prompt)
    path, query, _ = href_parts(reload)
    assert (path, query.get("tab")) == (PAGE, "drafts")
    assert only_focus(elements) is prompt
    assert "hx-trigger" not in response.headers, "nothing happened, so nothing is announced"


def seed_two_imports(facts: FactsApp) -> dict[str, Any]:
    """Import A: a role, a fact, a story and a draft that needs a check. Import B: one fact."""
    facts.add_import(IMPORT_A)
    facts.add_import(IMPORT_B)
    role = DraftRole(key="r1", employer="Sample Logistics SA", title="Junior Planner")
    routes, story, flagged = facts.drafts(
        IMPORT_A,
        DraftInput(text=ROUTES, role_key="r1", skills=("Scheduling",), original_answer=ROUTES),
        DraftInput(
            type=FactType.STORY,
            action="Cut invoice processing time",
            result="by 35% across 4 warehouses.",
            original_answer=INVOICES,
        ),
        DraftInput(text=TRAINED, role_key="r1", original_answer=TRAINED, needs_check=True),
        role=role,
    )
    (degree,) = facts.drafts(IMPORT_B, DraftInput(text=DEGREE, original_answer=DEGREE))
    return {"routes": routes, "story": story, "flagged": flagged, "degree": degree}


def removed_blocks(answer: list[Element]) -> set[str]:
    """The ids of the facts whose blocks an answer removes from the page (out of band)."""
    prefix = fact_id_attr("")
    return {
        (e.attrs.get("id") or "")[len(prefix) :]
        for e in answer
        if e.attrs.get("hx-swap-oob") == "delete" and (e.attrs.get("id") or "").startswith(prefix)
    }


def quotes_in(elements: list[Element], block: Element) -> list[str]:
    return [e.text for e in inside(elements, block) if e.tag in ("blockquote", "q")]


# --- the import section ------------------------------------------------------------------------


def test_import_form_says_cv_text_goes_to_the_ai_provider(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        assert (f"#{IMPORT_ID}", f"#{IMPORT_START_ID}") == (IMPORT_TARGET, IMPORT_FOCUS)
        elements = facts.tab("drafts")
        section = required(elements, IMPORT_ID)
        heading_ids = (section.attrs.get("aria-labelledby") or "").split()
        assert heading_ids and required(elements, heading_ids[0]).tag == "h2"

        form = required(elements, IMPORT_FORM_ID)
        assert form.tag == "form" and form.within(section)
        assert (form.attrs.get("method") or "").lower() == "post"
        assert form.attrs.get("action") == IMPORT_URL == form.attrs.get("hx-post")
        assert form.attrs.get("enctype") == "multipart/form-data"
        assert form.attrs.get("hx-encoding") == "multipart/form-data"
        assert form.attrs.get("hx-target") == IMPORT_TARGET, "the budget dialog swaps the same"
        file = named_control(elements, form, "file")
        assert file.tag == "input" and file.attrs.get("type") == "file"
        text = named_control(elements, form, "text")
        assert text.tag == "textarea"
        assert has_label(elements, file) and has_label(elements, text)

        start = required(elements, IMPORT_START_ID)
        assert start.tag == "button" and start.text == IMPORT and start.within(form)
        assert (start.attrs.get("type") or "submit") == "submit"
        notice = required(elements, IMPORT_PRIVACY_ID)
        assert notice.within(section)
        said = notice.text.lower()
        assert "sent to" in said and "ai provider" in said and "cv" in said, notice.text
        assert IMPORT_PRIVACY_ID in (start.attrs.get("aria-describedby") or "").split(), (
            "the button is described by the notice"
        )
        # Nothing kept yet, and no drafts: both say so.
        assert not [e for e in inside(elements, section) if e.text in (START, REMOVE)]
        assert says(elements, NO_DRAFTS, required(elements, DRAFTS_ID))
        # The section is on the Drafts tab only.
        assert by_id(facts.tab("library"), IMPORT_ID) is None


def test_kept_uploads_listed_with_start_and_remove(tmp_path: Path) -> None:
    """ADR-092: every kept upload is listed (``CvImportService.imports()``). One still to be
    imported has Start and Remove; a finished one keeps Remove, so the stored file can go."""
    with facts_app(tmp_path) as facts:
        facts.add_import("imp-kept", state="uploaded")
        facts.add_import(IMPORT_A)
        facts.drafts(IMPORT_A, ROUTES)
        assert {item.id for item in facts.run.app.state.cv_import.imports()} == {
            "imp-kept",
            IMPORT_A,
        }

        for elements in (facts.tab("drafts"), facts.fragment(IMPORTS_URL)):
            listing = required(elements, IMPORT_LIST_ID)
            kept = required(elements, upload_id("imp-kept"))
            done = required(elements, upload_id(IMPORT_A))
            assert kept.within(listing) and done.within(listing)

            start = button(elements, kept, START)
            path, values = submitted_by(elements, start)
            assert path == start_path("imp-kept")
            assert "text" not in values and "file" not in values, "the CV is not sent again"
            remove = button(elements, kept, REMOVE)
            assert submitted_by(elements, remove)[0] == remove_path("imp-kept")
            for control in (start, remove):
                named = [
                    required(elements, i)
                    for i in (control.attrs.get("aria-describedby") or "").split()
                ]
                assert named and all(e.within(kept) and e.text for e in named), (
                    f'"{control.text}" says which upload it acts on'
                )

            assert not buttons(elements, done, START) and not buttons(elements, done, TRY_AGAIN)
            assert submitted_by(elements, button(elements, done, REMOVE))[0] == remove_path(
                IMPORT_A
            )
            # Generated file names are never shown.
            shown = " ".join(e.text for e in inside(elements, listing))
            assert "imp-kept.txt" not in shown and f"{IMPORT_A}.txt" not in shown
        assert required(facts.fragment(IMPORTS_URL), IMPORT_LIST_ID) is not None


def test_removed_upload_leaves_the_list_and_keeps_its_drafts(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        run = facts.run
        run.finished(run.paste(CV_TEXT))
        import_id = run.only_import()["id"]
        drafts = [row["id"] for row in run.facts()]
        assert len(drafts) == 4
        elements = facts.tab("drafts")
        group = required(elements, group_id(import_id))
        assert len(fact_ids_shown(elements, drafts, group)) == 4
        button(elements, group, CONFIRM_ALL)

        removed = facts.post(remove_path(import_id))
        assert removed.status_code == 200 and MSG_REMOVED in removed.text
        assert announcement(removed) == MSG_REMOVED
        assert by_id(outline(removed.text), upload_id(import_id)) is None

        elements = facts.tab("drafts")
        assert by_id(elements, upload_id(import_id)) is None
        region = required(elements, DRAFTS_ID)
        assert len(fact_ids_shown(elements, drafts, region)) == 4, "the drafts stay"
        assert not buttons(elements, region, CONFIRM_ALL), "no import left to confirm together"
        for fact_id in drafts:
            button(elements, required(elements, fact_id_attr(fact_id)), CONFIRM)
        assert facts.post(remove_path(import_id), htmx=False).status_code == 404


def test_failed_import_shows_reason_and_try_again(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        run = facts.run
        text = f"{CV_TEXT}\n{fake_scripts.KEYWORD_MALFORMED}\n"
        run.finished(run.paste(text, htmx=True), status="failed")
        import_id = run.only_import()["id"]
        run.assert_upload_kept(import_id)

        for elements in (facts.tab("drafts"), facts.fragment(IMPORTS_URL)):
            item = required(elements, upload_id(import_id))
            assert says(elements, "Failed", item), "the status is in words"
            assert says(elements, InvalidModelOutput.message, item), "with its reason"
            again = button(elements, item, TRY_AGAIN)
            assert submitted_by(elements, again)[0] == start_path(import_id)
            assert not buttons(elements, item, START)
            button(elements, item, REMOVE)
            assert fake_scripts.KEYWORD_MALFORMED not in item.text, "nothing of the CV is shown"
        assert says(facts.tab("drafts"), NO_DRAFTS), "a failed import leaves no drafts"

        # Try again starts the stored upload once more (no second upload, a new attempt).
        path, values = submitted_by(elements, again)
        calls = run.spy.entered
        answer = facts.post(path, values)
        assert answer.status_code == 200, answer.status_code
        run.wait_entered(calls + 1)
        for row in run.tasks():
            assert str(run.wait_terminal(row.id).status.value) == "failed"
        run.assert_no_fact_rows()
        assert len(run.imports()) == 1 and len(run.stored_files()) == 1


def test_started_import_shows_task_status_that_follows_live_updates(tmp_path: Path) -> None:
    """A start by htmx answers the import section again (``#cv-import``, what the form and the
    budget dialog target) with the upload's task: an element that refetches itself on the
    body's task events. When the task ends the drafts are there, none confirmed."""
    with facts_app(tmp_path) as facts:
        run = facts.run
        run.hold.hold = True
        answer = run.paste(CV_TEXT, htmx=True)
        assert answer.status_code == 200 and "<html" not in answer.text
        task = run.only_task()
        import_id = run.only_import()["id"]
        run.wait_entered(1)

        def task_status(elements: list[Element]) -> Element:
            item = required(elements, upload_id(import_id))
            status = required(elements, upload_task_id(import_id))
            assert status.within(item), "the task's status sits with its upload"
            assert "CV import" in status.text, "the task's label"
            assert says(elements, "Running", status) or says(elements, "Queued", status)
            return item

        elements = outline(answer.text)
        section = required(elements, IMPORT_ID)
        assert section is elements[0], "the answer replaces #cv-import"
        item = task_status(elements)
        focus = only_focus(elements)
        assert focus.within(section), "focus moves to the started import"
        assert announcement(answer) == SAID_STARTED
        assert required(elements, IMPORT_START_ID).within(section), "the form is still there"
        assert not buttons(elements, item, START), "a running import can't be started twice"
        # The page and the list fragment show the same while it runs.
        task_status(facts.tab("drafts"))
        listed = facts.fragment(IMPORTS_URL)
        task_status(listed)
        trigger = required(listed, IMPORT_LIST_ID).attrs.get("hx-trigger") or ""
        assert required(listed, IMPORT_LIST_ID).attrs.get("hx-get") == IMPORTS_URL
        assert "tasks:updated from:body" in trigger and "tasks:resync from:body" in trigger

        run.hold.open()
        assert str(run.wait_terminal(task.id).status.value) == "succeeded"
        drafts = [row["id"] for row in run.facts()]
        assert len(drafts) == 4 and {row["status"] for row in run.facts()} == {"draft"}
        region = drafts_region(facts)
        group = required(region, group_id(import_id))
        assert len(fact_ids_shown(region, drafts, group)) == 4
        listed = facts.fragment(IMPORTS_URL)
        done = required(listed, upload_id(import_id))
        assert not buttons(listed, done, START) and not buttons(listed, done, TRY_AGAIN)
        button(listed, done, REMOVE)


# --- the drafts --------------------------------------------------------------------------------


def test_ac_m3_1_drafts_grouped_by_import_with_quote(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        own = typed(facts.bank, "Edited the monthly staff newsletter")
        ids = [view.id for view in seeded.values()]

        for elements in (facts.tab("drafts"), drafts_region(facts)):
            region = required(elements, DRAFTS_ID)
            group_a = required(elements, group_id(IMPORT_A))
            group_b = required(elements, group_id(IMPORT_B))
            for group in (group_a, group_b):
                assert group.within(region)
                (heading_id,) = (group.attrs.get("aria-labelledby") or "").split()
                heading = required(elements, heading_id)
                assert heading.within(group) and heading.tag in ("h2", "h3") and heading.text
            in_a = [seeded[name].id for name in ("routes", "story", "flagged")]
            assert set(fact_ids_shown(elements, ids, group_a)) == set(in_a)
            assert fact_ids_shown(elements, ids, group_b) == [seeded["degree"].id]
            assert by_id(elements, fact_id_attr(own.id)) is None, "a confirmed fact is no draft"

            # Each draft: its wording, the CV quote beside it, its actions.
            quotes = {"routes": ROUTES, "story": INVOICES, "flagged": TRAINED, "degree": DEGREE}
            for name, quote in quotes.items():
                block = required(elements, fact_id_attr(seeded[name].id))
                assert quotes_in(elements, block) == [quote], name
                label = QUOTE_LABEL_UNCHECKED if name == "flagged" else QUOTE_LABEL
                assert says(elements, label, block), "the quote is labelled"
                for action in (CONFIRM, DISCARD):
                    control = button(elements, block, action)
                    path, values = submitted_by(elements, control)
                    expected = confirm_path if action == CONFIRM else discard_path
                    assert path == expected(seeded[name].id)
                    assert values == {"version": str(seeded[name].version)}
            routes = required(elements, fact_id_attr(seeded["routes"].id))
            for part in (ROUTES, "Sample Logistics SA", "Junior Planner", "Scheduling"):
                assert part in routes.text, part
            story = required(elements, fact_id_attr(seeded["story"].id))
            assert "Cut invoice processing time" in story.text
            assert "by 35% across 4 warehouses." in story.text

            # The batch button lists every draft of its own import, and only those.
            path, values = submitted_by(elements, button(elements, group_a, CONFIRM_ALL))
            assert path == confirm_all_path(IMPORT_A)
            assert values == {batch_field(i): str(facts.bank.facts.get(i).version) for i in in_a}
            path, values = submitted_by(elements, button(elements, group_b, CONFIRM_ALL))
            assert path == confirm_all_path(IMPORT_B)
            assert values == {batch_field(seeded["degree"].id): "1"}

        # Showing drafts confirms nothing (AC-M3.1).
        assert {facts.status(i) for i in ids} == {"draft"}


def test_draft_edit_form_carries_the_wording(tmp_path: Path) -> None:
    """Rewording is inline: under "Edit", a form with the draft's wording that posts to
    ``confirm-edited``. A fact has ``text``; a story its three parts."""
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        elements = facts.tab("drafts")
        expected: dict[str, dict[str, str]] = {
            "routes": {"text": ROUTES},
            "story": {
                "situation": "",
                "action": "Cut invoice processing time",
                "result": "by 35% across 4 warehouses.",
            },
        }
        for name, fields in expected.items():
            view = seeded[name]
            block = required(elements, fact_id_attr(view.id))
            reveal = [
                e
                for e in inside(elements, block)
                if e.tag in ("summary", "button") and e.text == EDIT
            ]
            assert len(reveal) == 1, f'one "{EDIT}" control opens the inline form'
            save = button(elements, block, SAVE_CONFIRM)
            path, values = submitted_by(elements, save)
            assert path == edit_confirm_path(view.id)
            assert values == {"version": str(view.version)}
            form = save.parent
            while form is not None and form.tag != "form":
                form = form.parent
            assert form is not None
            areas = {
                str(e.attrs.get("name")): e for e in inside(elements, form) if e.tag == "textarea"
            }
            assert set(areas) == set(fields), name
            for field, value in fields.items():
                assert areas[field].text == value, (name, field)
                assert has_label(elements, areas[field]), f"{field} has a visible label"


def test_quote_and_wording_are_escaped(tmp_path: Path) -> None:
    """Spec 2.9 rule 4: a quote or wording holding markup is shown as text."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(
            IMPORT_A, DraftInput(text=SCRIPT_TEXT, original_answer=SCRIPT_QUOTE, needs_check=True)
        )
        answers = [
            facts.get(PAGE, htmx=False, tab="drafts"),
            facts.get(PAGE, tab="drafts"),
            facts.get(DRAFTS_URL),
        ]
        for response in answers:
            assert response.status_code == 200
            assert "<script>window.fictional" not in response.text
            assert "<img src=x" not in response.text
            assert "&lt;script&gt;window.fictional=1&lt;/script&gt;" in response.text
            elements = outline(response.text)
            block = required(elements, fact_id_attr(draft.id))
            assert quotes_in(elements, block) == [SCRIPT_QUOTE], "the quote reads as typed"
            assert SCRIPT_TEXT in block.text
            assert not [e for e in inside(elements, block) if e.tag in ("script", "img")]

        # The same after it is confirmed, in the Library and in the action's own answer.
        confirmed = facts.act(confirm_path(draft.id), draft.id)
        assert confirmed.status_code == 200 and "<img src=x" not in confirmed.text
        library = facts.get(PAGE, htmx=False, tab="library")
        assert "<img src=x" not in library.text and "&lt;img src=x" in library.text


def test_needs_check_note_and_practice_badge_on_drafts(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        for elements in (facts.tab("drafts"), drafts_region(facts)):
            for name, view in seeded.items():
                block = required(elements, fact_id_attr(view.id))
                assert says(elements, PRACTICE_BADGE, block), f"{name}: made by the fake"
                assert says(elements, NEEDS_CHECK_NOTE, block) is (name == "flagged"), name
            flagged = required(elements, fact_id_attr(seeded["flagged"].id))
            confirm = button(elements, flagged, CONFIRM)
            notes = [
                required(elements, i) for i in (confirm.attrs.get("aria-describedby") or "").split()
            ]
            assert NEEDS_CHECK_NOTE in [note.text for note in notes], (
                "the flagged draft's Confirm is described by the note"
            )

        # A reworded draft keeps both until the user confirms it (ADR-091 items 1 and 6).
        view = seeded["flagged"]
        facts.bank.facts.edit(
            view.id, FactInput(text="Trained new planners."), expected_version=view.version
        )
        elements = drafts_region(facts)
        block = required(elements, fact_id_attr(view.id))
        assert says(elements, NEEDS_CHECK_NOTE, block) and says(elements, PRACTICE_BADGE, block)


def test_confirm_route_changes_one_fact(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        target = seeded["routes"]
        others = [view.id for name, view in seeded.items() if name != "routes"]

        response = facts.act(confirm_path(target.id), target.id)

        elements, block = assert_draft_answer(facts, response, target.id, SAID_CONFIRMED, 3)
        assert "Confirmed" in block.text
        assert not buttons(elements, block, CONFIRM) and not buttons(elements, block, DISCARD)
        assert facts.status(target.id) == "confirmed"
        assert {facts.status(i) for i in others} == {"draft"}, "one fact changed"
        assert event_types(facts.db, target.id) == ["created", "confirmed"]
        assert len(version_rows(facts.db, target.id)) == 1, "confirming is no new wording"
        for other in others:
            assert event_types(facts.db, other) == ["created"]
            assert by_id(elements, fact_id_attr(other)) is None, "only its own block is answered"
        # Its draft role is confirmed with it.
        role_id = facts.bank.facts.get(target.id).role_id
        assert str(facts.bank.roles.get(role_id).status.value) == "confirmed"

        # It left the drafts and is in the Library.
        region = drafts_region(facts)
        assert by_id(region, fact_id_attr(target.id)) is None
        assert set(fact_ids_shown(region, [v.id for v in seeded.values()])) == set(others)
        library = facts.tab("library")
        assert ROUTES in required(library, fact_id_attr(target.id)).text

        # A flagged draft is confirmed the same way: this is the user's own check.
        flagged = seeded["flagged"]
        response = facts.act(confirm_path(flagged.id), flagged.id)
        assert_draft_answer(facts, response, flagged.id, SAID_CONFIRMED, 2)
        assert facts.status(flagged.id) == "confirmed"
        assert facts.bank.facts.get(flagged.id).needs_check is False


def test_ac_m3_2_edit_then_confirm_route_adds_version_and_keeps_details(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        routes = seeded["routes"]
        before = facts.bank.facts.get(routes.id)

        response = facts.act(edit_confirm_path(routes.id), routes.id, text=f"  {REWORDED}\r\n")

        elements, block = assert_draft_answer(facts, response, routes.id, SAID_EDITED, 3)
        assert not buttons(elements, block, SAVE_CONFIRM)
        after = facts.bank.facts.get(routes.id)
        assert str(after.status.value) == "confirmed" and after.content.text == REWORDED
        versions = version_rows(facts.db, routes.id)
        assert [row["text"] for row in versions] == [ROUTES, REWORDED], "a new version"
        assert current_version_row(facts.db, routes.id)["source"] == "manual"
        # Only the wording changed: role, skills, type and dates are the draft's.
        assert (after.role_id, after.skills, after.type) == (
            before.role_id,
            before.skills,
            before.type,
        )
        assert after.skills == ("Scheduling",) and after.role_id is not None
        assert after.origin_provider == "fake", "the practice label survives the edit"
        assert REWORDED in required(facts.tab("library"), fact_id_attr(routes.id)).text

        # A story is reworded by its parts and stays a story.
        story = seeded["story"]
        response = facts.act(
            edit_confirm_path(story.id),
            story.id,
            situation="Invoices took a week to process.",
            action="Cut invoice processing time",
            result="by 35%.",
        )
        assert_draft_answer(facts, response, story.id, SAID_EDITED, 2)
        saved = facts.bank.facts.get(story.id)
        assert str(saved.status.value) == "confirmed" and saved.type == FactType.STORY
        assert (saved.content.situation, saved.content.action, saved.content.result) == (
            "Invoices took a week to process.",
            "Cut invoice processing time",
            "by 35%.",
        )
        # Unchanged wording is a plain confirm: no second version.
        degree = seeded["degree"]
        response = facts.act(edit_confirm_path(degree.id), degree.id, text=DEGREE)
        assert response.status_code == 200 and facts.status(degree.id) == "confirmed"
        assert len(version_rows(facts.db, degree.id)) == 1


def test_edit_then_confirm_422_tied_to_the_field(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        routes = seeded["routes"]
        with pytest.raises(FactInputInvalid) as refused:
            facts.bank.facts.edit_and_confirm(
                routes.id, FactInput(text="  "), expected_version=routes.version
            )
        assert refused.value.field == "text"
        message = refused.value.user_message
        before = facts.snapshot()

        for blank in ("", "   ", "\r\n"):
            response = facts.act(edit_confirm_path(routes.id), routes.id, text=blank)
            assert response.status_code == 422, (blank, response.status_code)
            assert "<html" not in response.text
            elements = outline(response.text)
            block = required(elements, fact_id_attr(routes.id))
            save = button(elements, block, SAVE_CONFIRM)
            path, values = submitted_by(elements, save)
            assert path == edit_confirm_path(routes.id)
            assert values == {"version": str(routes.version)}, "the form can be sent again"
            (field,) = [
                e
                for e in inside(elements, block)
                if e.tag == "textarea" and e.attrs.get("name") == "text"
            ]
            assert field.attrs.get("aria-invalid") == "true"
            described = [
                required(elements, i) for i in (field.attrs.get("aria-describedby") or "").split()
            ]
            assert message in [e.text for e in described], "the error is tied to the field"
            assert all(e.within(block) for e in described)
            assert only_focus(elements) is field, "the invalid field takes focus"
            assert "hx-trigger" not in response.headers
        assert facts.snapshot() == before, "a refused edit saves nothing"
        assert facts.status(routes.id) == "draft"

        plain = facts.act(edit_confirm_path(routes.id), routes.id, htmx=False, text=" ")
        assert plain.status_code == 422 and message in plain.text
        assert facts.snapshot() == before


def test_discard_then_undo_route(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        degree = seeded["degree"]

        response = facts.act(discard_path(degree.id), degree.id)

        elements, block = assert_draft_answer(facts, response, degree.id, SAID_DISCARDED, 3)
        assert "Discarded" in block.text
        assert facts.status(degree.id) == "discarded"
        undo = button(elements, block, UNDO)
        assert only_focus(elements) is undo, "Undo is one key press away"
        assert not buttons(elements, block, CONFIRM) and not buttons(elements, block, DISCARD)
        path, values = submitted_by(elements, undo)
        assert path == undo_path(degree.id)
        assert values == {"version": str(facts.bank.facts.get(degree.id).version)}
        assert by_id(drafts_region(facts), fact_id_attr(degree.id)) is None, (
            "a discarded draft is not listed again"
        )

        restored = facts.post(path, values)

        elements, block = assert_draft_answer(facts, restored, degree.id, SAID_RESTORED, 4)
        assert facts.status(degree.id) == "draft"
        assert event_types(facts.db, degree.id) == ["created", "discarded", "discard_undone"]
        assert DEGREE in block.text and quotes_in(elements, block) == [DEGREE]
        current = {"version": str(facts.bank.facts.get(degree.id).version)}
        for action, expected in ((CONFIRM, confirm_path), (DISCARD, discard_path)):
            assert submitted_by(elements, button(elements, block, action)) == (
                expected(degree.id),
                current,
            )
        assert required(drafts_region(facts), fact_id_attr(degree.id)) is not None

        # Undo twice, or with the version the Undo button no longer holds, is refused.
        assert facts.post(path, values).status_code == 409
        assert facts.status(degree.id) == "draft"


def test_batch_confirm_route(tmp_path: Path) -> None:
    """Confirm all from one import: its drafts are confirmed, another import's are not."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        facts.add_import(IMPORT_B)
        in_a = facts.drafts(IMPORT_A, ROUTES, INVOICES, "English")
        (other,) = facts.drafts(IMPORT_B, DEGREE)
        elements = facts.tab("drafts")
        group = required(elements, group_id(IMPORT_A))
        path, values = submitted_by(elements, button(elements, group, CONFIRM_ALL))

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert "<html" not in response.text
        answer = outline(response.text)
        group = required(answer, batch_id(IMPORT_A))
        assert group is answer[0], "the answer replaces the batch's own wrapper"
        status = required(answer, f"{group_id(IMPORT_A)}-status")
        assert status.within(group) and status.text == batch_said(3, 0) == "3 facts confirmed."
        assert only_focus(answer) is status and status.attrs.get("tabindex") == "-1"
        assert announcement(response) == batch_said(3, 0)
        assert links(answer, drafts_tab_text(1)), "the tab link carries the new count"
        assert not buttons(answer, group, CONFIRM) and not buttons(answer, group, CONFIRM_ALL)
        assert by_id(answer, group_id(IMPORT_B)) is None, "only its own group is answered"
        assert removed_blocks(answer) == {view.id for view in in_a}
        assert not [e for e in answer if e.tag == "button"], "no draft is left to act on"

        assert {facts.status(view.id) for view in in_a} == {"confirmed"}
        assert facts.status(other.id) == "draft"
        for view in in_a:
            assert event_types(facts.db, view.id) == ["created", "confirmed"]
        region = drafts_region(facts)
        assert by_id(region, group_id(IMPORT_A)) is None, "an emptied group is gone on reload"
        assert fact_ids_shown(region, [other.id]) == [other.id]

        # A plain form post does the same and goes back to the tab.
        plain = facts.confirm_all(IMPORT_B, [other.id], htmx=False)
        assert plain.status_code == 303 and plain.headers["location"] == f"{PAGE}?tab=drafts"
        assert facts.status(other.id) == "confirmed"
        assert says(facts.tab("drafts"), NO_DRAFTS)


def test_batch_confirm_reports_skipped_flagged_drafts(tmp_path: Path) -> None:
    """ADR-091 item 3: the batch leaves drafts that need a check, says how many, and each
    keeps its own Confirm."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        fine = facts.drafts(IMPORT_A, ROUTES, INVOICES)
        flagged = facts.drafts(
            IMPORT_A,
            DraftInput(text=TRAINED, original_answer=TRAINED, needs_check=True),
            DraftInput(text="Led 50 engineers.", original_answer=ROUTES, needs_check=True),
        )
        everything = [view.id for view in (*fine, *flagged)]

        response = facts.confirm_all(IMPORT_A, everything)

        assert response.status_code == 200
        said = batch_said(2, 2)
        assert said == (
            "2 facts confirmed. 2 drafts need a check and were left for you to confirm one by one."
        )
        answer = outline(response.text)
        assert required(answer, f"{group_id(IMPORT_A)}-status").text == said
        assert announcement(response) == said
        assert links(answer, drafts_tab_text(2))
        assert {facts.status(view.id) for view in fine} == {"confirmed"}
        assert {facts.status(view.id) for view in flagged} == {"draft"}
        left = [view.id for view in flagged]
        # The answer takes the confirmed drafts' blocks off the page and touches no other:
        # the drafts it left stay as the page has them, each with its own Confirm.
        assert removed_blocks(answer) == {view.id for view in fine}
        assert set(fact_ids_shown(answer, everything)) == {view.id for view in fine}
        assert not buttons(answer, required(answer, batch_id(IMPORT_A)), CONFIRM_ALL), (
            "nothing is left that the batch would confirm"
        )
        shown = drafts_region(facts)
        group = required(shown, group_id(IMPORT_A))
        assert set(fact_ids_shown(shown, everything, group)) == set(left)
        for fact_id in left:
            block = required(shown, fact_id_attr(fact_id))
            assert says(shown, NEEDS_CHECK_NOTE, block)
            path, values = submitted_by(shown, button(shown, block, CONFIRM))
            assert path == confirm_path(fact_id) and values == {"version": "1"}

        # One left: the sentence is singular. Then none: nothing to say about checks.
        facts.act(confirm_path(left[0]), left[0])
        (late,) = facts.drafts(IMPORT_A, DEGREE)
        again = facts.confirm_all(IMPORT_A, [left[1], late.id])
        assert (
            announcement(again)
            == batch_said(1, 1)
            == (
                "1 fact confirmed. 1 draft needs a check and was left for you "
                "to confirm on its own."
            )
        )
        assert facts.status(left[1]) == "draft"
        only_flagged = facts.confirm_all(IMPORT_A, [left[1]])
        assert only_flagged.status_code == 200
        assert announcement(only_flagged) == batch_said(0, 1)
        assert facts.status(left[1]) == "draft"


def test_stale_draft_shows_reload_prompt(tmp_path: Path) -> None:
    """A draft that changed since the page was rendered: 409 with the reload prompt in its
    own block, and nothing saved (spec 2.11)."""
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        routes = seeded["routes"]
        shown = facts.tab("drafts")
        block = required(shown, fact_id_attr(routes.id))
        forms = {
            name: submitted_by(shown, button(shown, block, name))
            for name in (CONFIRM, DISCARD, SAVE_CONFIRM)
        }
        batch = submitted_by(shown, button(shown, required(shown, group_id(IMPORT_A)), CONFIRM_ALL))
        # Meanwhile the draft is reworded elsewhere (another tab).
        facts.bank.facts.edit(routes.id, FactInput(text=REWORDED), expected_version=routes.version)
        before = facts.snapshot()

        for name, (path, values) in forms.items():
            data = {**values, "text": "Typed in the stale tab"} if name == SAVE_CONFIRM else values
            response = facts.post(path, data)
            assert_reload_prompt(
                response, f"{fact_id_attr(routes.id)}-conflict", VersionConflict.message
            )
            assert required(outline(response.text), fact_id_attr(routes.id)), "inside its block"
            assert facts.post(path, data, htmx=False).status_code == 409, name
        # The batch is all or nothing: one stale draft stops it.
        response = facts.post(*batch)
        assert_reload_prompt(response, f"{group_id(IMPORT_A)}-conflict", VersionConflict.message)
        assert facts.post(*batch, htmx=False).status_code == 409
        # A version that was never issued, or isn't a number, is stale too.
        for version in ("7", "", "one", "-1", "1.0", '"><b id="injected">'):
            response = facts.post(confirm_path(routes.id), {"version": version})
            assert response.status_code == 409, (version, response.status_code)
            assert 'id="injected"' not in response.text
        assert facts.post(confirm_path(routes.id), {}).status_code == 409, "no version at all"

        assert facts.snapshot() == before, "nothing was saved"
        assert {facts.status(view.id) for view in seeded.values()} == {"draft"}

        # After a reload the forms carry the stored version and work.
        fresh = facts.tab("drafts")
        path, values = submitted_by(
            fresh, button(fresh, required(fresh, fact_id_attr(routes.id)), CONFIRM)
        )
        assert values == {"version": str(facts.bank.facts.get(routes.id).version)}
        assert facts.post(path, values).status_code == 200
        assert facts.status(routes.id) == "confirmed"


def test_refused_transition_is_409_and_unknown_fact_404(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        own = typed(facts.bank, "Edited the monthly staff newsletter")
        before = facts.snapshot()

        # ``FactTransitionRefused``: the right version, but not a draft (any more).
        current = {"version": str(own.version)}
        for path in (confirm_path(own.id), discard_path(own.id), undo_path(own.id)):
            response = facts.post(path, current)
            assert_reload_prompt(
                response, f"{fact_id_attr(own.id)}-conflict", FactTransitionRefused.message
            )
            assert facts.post(path, current, htmx=False).status_code == 409
        refused = facts.post(edit_confirm_path(own.id), {**current, "text": "Reworded"})
        assert refused.status_code == 409
        # A batch that names a fact of another import is refused whole.
        degree = seeded["degree"]
        response = facts.confirm_all(IMPORT_A, [seeded["routes"].id, degree.id])
        assert_reload_prompt(
            response, f"{group_id(IMPORT_A)}-conflict", FactTransitionRefused.message
        )

        # ``FactNotFound``: a fixed message that repeats nothing from the request.
        for unknown in ("nobody", "seed-9999", "fictional<b>id", "a/b"):
            for path in (
                confirm_path(unknown),
                edit_confirm_path(unknown),
                discard_path(unknown),
                undo_path(unknown),
            ):
                for htmx in (True, False):
                    response = facts.post(path, {"version": "1", "text": "x"}, htmx=htmx)
                    assert response.status_code == 404, (path, htmx, response.status_code)
                    if "/" not in unknown:
                        assert FactNotFound.message in response.text
                    assert "fictional" not in response.text and "nobody" not in response.text
        response = facts.post(confirm_all_path("imp-nobody"), {batch_field(degree.id): "1"})
        assert response.status_code in (404, 409) and "imp-nobody" not in response.text
        assert (
            facts.post(confirm_all_path(IMPORT_A), {batch_field("nobody"): "1"}).status_code == 404
        )

        assert facts.snapshot() == before
        assert {facts.status(view.id) for view in seeded.values()} == {"draft"}


def test_plain_posts_redirect_to_drafts_tab(tmp_path: Path) -> None:
    """Without scripts every action is a form post answered with 303 back to the tab."""
    with facts_app(tmp_path) as facts:
        seeded = seed_two_imports(facts)
        back = f"{PAGE}?tab=drafts"
        steps = (
            (confirm_path, seeded["routes"].id, {}, "confirmed"),
            (
                edit_confirm_path,
                seeded["flagged"].id,
                {"text": "Trained new planners."},
                "confirmed",
            ),
            (discard_path, seeded["degree"].id, {}, "discarded"),
            (undo_path, seeded["degree"].id, {}, "draft"),
        )
        for make_path, fact_id, data, status in steps:
            response = facts.act(make_path(fact_id), fact_id, htmx=False, **data)
            assert response.status_code == 303, (make_path(fact_id), response.status_code)
            assert response.headers["location"] == back
            assert "hx-trigger" not in response.headers
            assert facts.status(fact_id) == status
        shown = facts.tab("drafts")
        ids = [view.id for view in seeded.values()]
        assert set(fact_ids_shown(shown, ids)) == {seeded["story"].id, seeded["degree"].id}
