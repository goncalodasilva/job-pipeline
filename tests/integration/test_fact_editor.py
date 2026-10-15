"""The fact editor (M3.6; AC-M3.2 and AC-M3.3 through the routes).

``GET /facts/new`` (``?type=story`` for a story) and ``GET /facts/{id}/edit`` are pages: no
token, full HTML, no side effects. The form (``#fact-editor``) posts to the same address
(``POST /facts/new``, ``POST /facts/{id}/edit`` with ``version``): the wording (``text``, or a
story's ``situation``, ``action`` and ``result``), the role (picked, or typed as employer and
title), the skills (ticked, or typed), tags, dates and the privacy radio. A typed fact is
confirmed at once; a save makes a version only when the wording changed. ``POST
/facts/{id}/archive`` and ``/restore`` change the status; a discarded draft is restored from the
Library's Discarded filter. Refused input is 422 tied to its field with what was typed kept; a
stale version is 409 with the reload prompt; an unknown fact is 404. The contract's names live
in ``tests/facts_page.py``. Real SQLite in a temp folder, the frozen clock, the fake provider,
no network; every value is fictional.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.domain.errors import (
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    VersionConflict,
)
from app.domain.facts import FactType, Privacy
from app.domain.partial_dates import DATE_MESSAGE
from app.services.facts import DraftInput, DraftRole, FactInput
from tests.fact_bank import (
    current_version_row,
    event_types,
    fact_row,
    role_row,
    skill_names_of,
    typed,
    version_rows,
)
from tests.facts_page import (
    ADD_FACT,
    ADD_STORY,
    ARCHIVE,
    BADGE_NEVER_USE,
    DATE_FIELDS,
    DRAFTS_URL,
    EDIT,
    EDIT_DETAILS,
    EDIT_HEADINGS,
    EDITOR_CONFLICT_ID,
    EDITOR_ID,
    EDITOR_STATE_STATUS_ID,
    EDITOR_STATUS_ID,
    END_BEFORE_START,
    FROM_FIELD,
    HISTORY,
    HISTORY_ID,
    LIBRARY_URL,
    MERGE_WITH,
    NEW_HEADINGS,
    NEW_ROLE_FIELDS,
    NEW_SKILLS_FIELD,
    NEW_URL,
    PAGE,
    PRIVACY_FIELD,
    PRIVACY_HELP,
    PRIVACY_LABELS,
    PRIVACY_LEGEND,
    RELOAD,
    RESTORE,
    RESTORE_DRAFT,
    RESULTS_ID,
    ROLE_FIELD,
    SAID_ADDED,
    SAID_ARCHIVED,
    SAID_DRAFT_SAVED,
    SAID_RESTORED,
    SAID_SAVED,
    SAID_STORY_ADDED,
    SAID_UNARCHIVED,
    SAVE,
    SKILLS_FIELD,
    TAGS_FIELD,
    TYPE_FIELD,
    WORDING_FIELDS,
    FactsApp,
    FormValues,
    alert,
    announcement,
    archive_path,
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
    history_url,
    href_parts,
    inside,
    label_of,
    links,
    merge_url,
    names_fact,
    only_focus,
    option_values,
    page_of,
    posts_to,
    required,
    restore_path,
    says,
    send,
    undo_merge_path,
    undo_path,
)
from tests.integration.test_budget_warning_dialog import submitted_by
from tests.integration.test_setup_redirect import ORIGIN, TOKEN_HEADER, Element, outline
from tests.integration.test_task_routes import database_state

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
REWORDED = "Planned 80 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
NEWSLETTER = "Edited the monthly staff newsletter"
SITUATION = "Invoices took nine days to process."
ACTION = "Mapped the steps and removed two approvals."
RESULT = "Processing fell to six days."
EMPLOYER = "Sample Logistics SA"
TITLE = "Junior Planner"
HOSTILE = '"><script id="injected">window.fictional=1</script><b id="bold">Planned routes</b>'
IMPORT_A = "imp-a"


# --- helpers -----------------------------------------------------------------------------------


def editor(elements: list[Element]) -> Element:
    form = required(elements, EDITOR_ID)
    assert form.tag == "form", "the editor is one form"
    return form


def open_new(facts: FactsApp, kind: str = "fact") -> tuple[list[Element], FormValues]:
    elements = page_of(facts, NEW_URL, **({} if kind == "fact" else {"type": kind}))
    form = editor(elements)
    assert posts_to(form) == NEW_URL
    return elements, form_values(elements, form)


def open_edit(facts: FactsApp, fact_id: str) -> tuple[list[Element], FormValues]:
    elements = page_of(facts, edit_url(fact_id))
    form = editor(elements)
    assert posts_to(form) == edit_url(fact_id)
    return elements, form_values(elements, form)


def save_new(facts: FactsApp, kind: str = "fact", *, htmx: bool = True, **fields: Any) -> Response:
    """Post the new-item form as the page gave it, with ``fields`` typed in."""
    _, values = open_new(facts, kind)
    return send(facts, NEW_URL, {**values, **fields}, htmx=htmx)


def save_edit(facts: FactsApp, fact_id: str, *, htmx: bool = True, **fields: Any) -> Response:
    """Post the edit form as the page gave it (its version included), with ``fields`` changed."""
    _, values = open_edit(facts, fact_id)
    return send(facts, edit_url(fact_id), {**values, **fields}, htmx=htmx)


def fact_ids(facts: FactsApp) -> list[str]:
    return [str(row["id"]) for row in facts.run.rows("SELECT id FROM facts ORDER BY rowid")]


def newest_fact(facts: FactsApp) -> str:
    return fact_ids(facts)[-1]


def assert_saved(facts: FactsApp, response: Response, fact_id: str, said: str) -> list[Element]:
    """An htmx answer to a save: a fragment with the editor as it is now (posting to the fact's
    own address with the stored version), the line that says so, one focus target that no key
    press can submit, and the same words for the live region."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "<html" not in response.text, "an htmx answer is a fragment"
    assert facts.token not in response.text
    elements = outline(response.text)
    form = editor(elements)
    assert posts_to(form) == edit_url(fact_id)
    stored = str(facts.bank.facts.get(fact_id).version)
    assert form_values(elements, form)["version"] == stored, "the form can be saved again"
    assert required(elements, EDITOR_STATUS_ID).text == said
    focus = only_focus(elements)
    assert focus.tag not in ("button", "input", "textarea", "select", "a"), focus.tag
    assert announcement(response) == said
    return elements


def invalid_fields(elements: list[Element], form: Element) -> list[Element]:
    return [e for e in inside(elements, form) if e.attrs.get("aria-invalid") == "true"]


def assert_refused(
    response: Response, field: str, message: str | None = None
) -> tuple[list[Element], Element]:
    """A 422 from the editor: a fragment with the form, the message tied to ``field`` and that
    field (the first invalid one) focused; nothing is announced."""
    assert response.status_code == 422, (response.status_code, response.text[:300])
    assert "<html" not in response.text
    elements = outline(response.text)
    form = editor(elements)
    invalid = invalid_fields(elements, form)
    assert invalid, "the refused field is marked aria-invalid"
    names = [e.attrs.get("name") for e in invalid]
    assert field in names, (field, names)
    control = invalid[names.index(field)]
    notes = [e.text for e in described_by(elements, control)]
    assert any(notes), "a message is tied to the field with aria-describedby"
    if message is not None:
        assert message in notes, (message, notes)
    assert only_focus(elements) is invalid[0], "the first invalid field takes focus"
    assert "hx-trigger" not in response.headers, "nothing was saved, so nothing is announced"
    return elements, control


def seed_role(facts: FactsApp) -> str:
    role = facts.bank.roles.create(employer=EMPLOYER, title=TITLE)
    return str(role.id)


def library(facts: FactsApp, **params: str) -> list[Element]:
    return facts.tab("library", **params)


# --- ways in -----------------------------------------------------------------------------------


def test_library_links_to_add_edit_and_history(tmp_path: Path) -> None:
    """The Library offers "Add a fact" and "Add a story" in every state, and each listed fact
    an Edit and a History link that say which fact they are for."""
    with facts_app(tmp_path) as facts:
        for elements in (library(facts), library(facts, q="xylophone")):
            (add_fact,) = links(elements, ADD_FACT)
            (add_story,) = links(elements, ADD_STORY)
            assert href_parts(add_fact)[:2] == (NEW_URL, {})
            assert href_parts(add_story)[:2] == (NEW_URL, {"type": "story"})

        routes = typed(facts.bank, ROUTES)
        invoices = typed(facts.bank, INVOICES)
        archived = typed(facts.bank, NEWSLETTER)
        facts.bank.facts.archive(archived.id, expected_version=archived.version)

        shown = (
            (library(facts), (routes, invoices)),
            (facts.fragment(LIBRARY_URL), (routes, invoices)),
            (library(facts, status="archived"), (archived,)),
        )
        for elements, listed in shown:
            for view in listed:
                block = required(elements, fact_id_attr(view.id))
                (edit,) = links(elements, EDIT, block)
                (history,) = links(elements, HISTORY, block)
                assert href_parts(edit) == (edit_url(view.id), {}, "")
                assert href_parts(history) == (edit_url(view.id), {}, HISTORY_ID)
                wording = str(view.content.text)
                for link in (edit, history):
                    assert names_fact(elements, link, wording), (
                        f'"{link.text}" says which fact it is for'
                    )
        assert links(library(facts), ADD_FACT), "still offered once facts exist"


def test_drafts_link_to_edit_details_history_and_merge(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        routes, degree = facts.drafts(IMPORT_A, ROUTES, DEGREE)
        for elements in (facts.tab("drafts"), facts.fragment(DRAFTS_URL)):
            for view, wording in ((routes, ROUTES), (degree, DEGREE)):
                block = required(elements, fact_id_attr(view.id))
                wanted = {
                    EDIT_DETAILS: (edit_url(view.id), {}, ""),
                    HISTORY: (edit_url(view.id), {}, HISTORY_ID),
                    MERGE_WITH: (merge_url(view.id), {}, ""),
                }
                for name, target in wanted.items():
                    (link,) = links(elements, name, block)
                    assert href_parts(link) == target, name
                    assert names_fact(elements, link, wording), name
        # A draft restored by Undo gets them back with its other actions.
        discarded = facts.bank.facts.discard(routes.id, expected_version=routes.version)
        answer = outline(facts.post(undo_path(routes.id), {"version": str(discarded.version)}).text)
        assert links(answer, MERGE_WITH, required(answer, fact_id_attr(routes.id)))


# --- the form ----------------------------------------------------------------------------------


def test_new_fact_page_has_labelled_fields(tmp_path: Path) -> None:
    """The new-item page, for a fact and for a story: a page (no token), one form, every
    control labelled, confirmed roles to pick from, the user's skills to tick."""
    with facts_app(tmp_path) as facts:
        role_id = seed_role(facts)
        typed(facts.bank, NEWSLETTER, skills=("Scheduling", "Excel"))
        # A draft role (an import found it; nothing confirmed it) is not offered.
        facts.add_import(IMPORT_A)
        facts.drafts(
            IMPORT_A,
            DraftInput(text=ROUTES, role_key="r1"),
            role=DraftRole(key="r1", employer="Example Freight Lda", title="Dispatcher"),
        )

        for kind in ("fact", "story"):
            elements, values = open_new(facts, kind)
            form = editor(elements)
            assert NEW_HEADINGS[kind] in headings(elements, "h1")
            assert values[TYPE_FIELD] == kind
            assert "version" not in values, "nothing exists yet"
            other = "story" if kind == "fact" else "fact"
            for name in WORDING_FIELDS[kind]:
                (field,) = controls(elements, form, name)
                assert field.tag == "textarea" and field.text == ""
                assert has_label(elements, field), name
            for name in WORDING_FIELDS[other]:
                assert not controls(elements, form, name), f"a {kind} has no {name} field"

            (role,) = controls(elements, form, ROLE_FIELD)
            assert role.tag == "select" and has_label(elements, role)
            options = option_values(elements, role)
            assert set(options) == {"", role_id}, "no role, or a confirmed one"
            assert TITLE in options[role_id] and EMPLOYER in options[role_id]
            assert values[ROLE_FIELD] == ""

            ticks = controls(elements, form, SKILLS_FIELD)
            assert {t.attrs.get("value") for t in ticks} == {"Scheduling", "Excel"}
            assert all(t.attrs.get("type") == "checkbox" and has_label(elements, t) for t in ticks)
            assert {label_of(elements, t) for t in ticks} == {"Scheduling", "Excel"}
            assert values[SKILLS_FIELD] == []

            for name in (*NEW_ROLE_FIELDS, NEW_SKILLS_FIELD, TAGS_FIELD, *DATE_FIELDS):
                (field,) = controls(elements, form, name)
                assert has_label(elements, field), name
                assert values[name] == ""
            save = button(elements, form, SAVE)
            assert (save.attrs.get("type") or "submit") == "submit"
            assert "novalidate" in form.attrs, "the server's messages are the ones shown"
            assert by_id(elements, HISTORY_ID) is None, "nothing has a history yet"

        # An unknown type is a fact, and is not shown back.
        response = facts.run.client.get(NEW_URL, params={"type": '"><b id="injected">'})
        assert response.status_code == 200 and 'id="injected"' not in response.text
        shown = outline(response.text)
        assert form_values(shown, editor(shown))[TYPE_FIELD] == "fact"


def test_privacy_radio_has_one_line_explanations(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        for elements in (
            open_new(facts)[0],
            open_new(facts, "story")[0],
            open_edit(facts, own.id)[0],
        ):
            form = editor(elements)
            radios = controls(elements, form, PRIVACY_FIELD)
            assert [r.attrs.get("value") for r in radios] == list(PRIVACY_LABELS)
            assert all(r.attrs.get("type") == "radio" for r in radios)
            assert [r.attrs.get("value") for r in radios if "checked" in r.attrs] == ["normal"]
            groups = {id(node) for node in elements if node.tag == "fieldset"}
            for radio in radios:
                value = str(radio.attrs.get("value"))
                assert label_of(elements, radio) == PRIVACY_LABELS[value]
                notes = [e.text for e in described_by(elements, radio)]
                assert PRIVACY_HELP[value] in notes, "each choice is described by its explanation"
                group = radio.parent
                while group is not None and group.tag != "fieldset":
                    group = group.parent
                assert group is not None and id(group) in groups, "the radios share a fieldset"
                legends = [e.text for e in inside(elements, group) if e.tag == "legend"]
                assert legends == [PRIVACY_LEGEND]
            for line in PRIVACY_HELP.values():
                assert says(elements, line, form), "the explanations are text on the page"
                assert "\n" not in line and len(line) <= 120, "one line each"


# --- adding ------------------------------------------------------------------------------------


def test_add_fact_confirmed_at_once_with_role_and_skills(tmp_path: Path) -> None:
    """ADR-089 item 11: a fact the user types is confirmed at once. Its new role is confirmed
    with it, typed skills join the list, and the events say what happened."""
    with facts_app(tmp_path) as facts:
        typed(facts.bank, NEWSLETTER, skills=("Scheduling",))

        response = save_new(
            facts,
            text=f"  {ROUTES}\r\n",
            new_role_employer=EMPLOYER,
            new_role_title=TITLE,
            skills=["Scheduling"],
            new_skills="Route planning, Excel , ",
            tags="logistics, planning",
            start_date="2022-03",
            end_date="2024",
        )

        fact_id = newest_fact(facts)
        elements = assert_saved(facts, response, fact_id, SAID_ADDED)
        assert response.headers.get("hx-push-url") == edit_url(fact_id), (
            "a reload shows the fact that was added"
        )
        row = fact_row(facts.db, fact_id)
        assert (row["status"], row["type"], row["privacy"]) == ("confirmed", "fact", "normal")
        assert (row["start_date"], row["end_date"]) == ("2022-03", "2024")
        assert json.loads(row["tags"]) == ["logistics", "planning"]
        (version,) = version_rows(facts.db, fact_id)
        assert (version["text"], version["source"], version["provider"]) == (ROUTES, "manual", None)
        assert event_types(facts.db, fact_id) == ["created"]
        role = role_row(facts.db, row["role_id"])
        assert (role["employer"], role["title"], role["status"]) == (EMPLOYER, TITLE, "confirmed")
        assert skill_names_of(facts.db, fact_id) == {"Scheduling", "Route planning", "Excel"}
        assert facts.run.tasks() == [] and facts.run.spy.requests == [], "no AI is involved"

        # The answer is the fact's own editor, holding what was saved.
        values = form_values(elements, editor(elements))
        assert values["text"] == ROUTES and values[ROLE_FIELD] == row["role_id"]
        assert sorted(values[SKILLS_FIELD]) == ["Excel", "Route planning", "Scheduling"]
        assert (values["start_date"], values["end_date"]) == ("2022-03", "2024")
        assert values[NEW_SKILLS_FIELD] == "" and values[NEW_ROLE_FIELDS[0]] == ""
        # It is in the Library, with its role and skills, and the role can be picked next time.
        block = required(library(facts), fact_id_attr(fact_id))
        assert ROUTES in block.text and TITLE in block.text and "Route planning" in block.text
        options = option_values(*_role_select(open_new(facts)[0]))
        assert row["role_id"] in options


def _role_select(elements: list[Element]) -> tuple[list[Element], Element]:
    (role,) = controls(elements, editor(elements), ROLE_FIELD)
    return elements, role


def test_add_story_stores_its_parts_and_picked_role(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        role_id = seed_role(facts)

        response = save_new(
            facts, "story", situation=SITUATION, action=ACTION, result=RESULT, role_id=role_id
        )

        fact_id = newest_fact(facts)
        assert_saved(facts, response, fact_id, SAID_STORY_ADDED)
        row = fact_row(facts.db, fact_id)
        assert (row["type"], row["status"], row["role_id"]) == ("story", "confirmed", role_id)
        version = current_version_row(facts.db, fact_id)
        assert (version["text"], version["situation"], version["action"], version["result"]) == (
            None,
            SITUATION,
            ACTION,
            RESULT,
        )
        elements, values = open_edit(facts, fact_id)
        assert EDIT_HEADINGS["story"] in headings(elements, "h1")
        assert (values["situation"], values["action"], values["result"]) == (
            SITUATION,
            ACTION,
            RESULT,
        )
        assert values[TYPE_FIELD] == "story" and "text" not in values


def test_add_plain_post_redirects_to_the_new_facts_editor(tmp_path: Path) -> None:
    """Without scripts the form posts and the answer is 303 to the fact's own editor."""
    with facts_app(tmp_path) as facts:
        response = save_new(facts, htmx=False, text=ROUTES)
        fact_id = newest_fact(facts)
        assert response.status_code == 303, (response.status_code, response.text[:300])
        assert response.headers["location"] == edit_url(fact_id)
        assert "hx-trigger" not in response.headers
        assert facts.status(fact_id) == "confirmed"
        elements, values = open_edit(facts, fact_id)
        assert EDIT_HEADINGS["fact"] in headings(elements, "h1") and values["text"] == ROUTES

        edited = save_edit(facts, fact_id, htmx=False, text=REWORDED)
        assert edited.status_code == 303 and edited.headers["location"] == edit_url(fact_id)
        assert current_version_row(facts.db, fact_id)["text"] == REWORDED


def test_story_requires_at_least_one_part(tmp_path: Path) -> None:
    """A story with no part at all is refused, tied to its first part; one part is enough."""
    with facts_app(tmp_path) as facts:
        before = facts.snapshot()
        for blank in ("", "   ", "\r\n"):
            response = save_new(
                facts, "story", situation=blank, action=blank, result=blank, tags="planning"
            )
            elements, _ = assert_refused(response, "situation")
            form = editor(elements)
            assert posts_to(form) == NEW_URL
            assert form_values(elements, form)[TAGS_FIELD] == "planning", "what was typed is kept"
            assert button(elements, form, SAVE)
        plain = save_new(facts, "story", htmx=False)
        assert plain.status_code == 422 and "<html" in plain.text
        plain_page = outline(plain.text)
        assert invalid_fields(plain_page, editor(plain_page))
        assert facts.snapshot() == before, "a refused story saves nothing"

        for part in WORDING_FIELDS["story"]:
            response = save_new(facts, "story", **{part: RESULT})
            assert response.status_code == 200, (part, response.status_code)
            version = current_version_row(facts.db, newest_fact(facts))
            assert [version[name] for name in WORDING_FIELDS["story"]].count(RESULT) == 1
            assert version[part] == RESULT and version["text"] is None

        # Editing a story down to nothing is refused as well.
        fact_id = newest_fact(facts)
        before = facts.snapshot()
        emptied = save_edit(facts, fact_id, situation="", action=" ", result="")
        assert_refused(emptied, "situation")
        assert facts.snapshot() == before


# --- editing -----------------------------------------------------------------------------------


def test_editor_save_creates_version(tmp_path: Path) -> None:
    """AC-M3.3: a save that changes the wording adds a version; the first one is kept."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES, tags=("logistics",), skills=("Scheduling",))
        elements, values = open_edit(facts, own.id)
        assert EDIT_HEADINGS["fact"] in headings(elements, "h1")
        assert values["version"] == str(own.version) and values["text"] == ROUTES
        assert values[TAGS_FIELD] == "logistics" and values[SKILLS_FIELD] == ["Scheduling"]

        response = send(facts, edit_url(own.id), {**values, "text": REWORDED})

        answer = assert_saved(facts, response, own.id, SAID_SAVED)
        first, second = version_rows(facts.db, own.id)
        assert (first["version"], first["text"]) == (1, ROUTES), "the old wording is kept"
        assert (second["version"], second["text"], second["source"]) == (2, REWORDED, "manual")
        assert current_version_row(facts.db, own.id)["id"] == second["id"]
        assert event_types(facts.db, own.id) == ["created", "edited"]
        assert facts.status(own.id) == "confirmed"
        saved = facts.bank.facts.get(own.id)
        assert saved.tags == ("logistics",) and saved.skills == ("Scheduling",), "details stay"
        assert form_values(answer, editor(answer))["text"] == REWORDED
        assert REWORDED in required(library(facts), fact_id_attr(own.id)).text


def test_edit_details_only_makes_no_version(tmp_path: Path) -> None:
    """ADR-090 item 4: role, skills, tags and dates live on the fact. Changing them is an
    event, never a version; saving the form unchanged writes nothing at all."""
    with facts_app(tmp_path) as facts:
        role_id = seed_role(facts)
        own = typed(facts.bank, ROUTES, tags=("logistics",), skills=("Scheduling",))

        response = save_edit(
            facts,
            own.id,
            role_id=role_id,
            tags="logistics, planning",
            new_skills="Excel",
            start_date="2022",
            end_date="2023-06-30",
        )

        assert_saved(facts, response, own.id, SAID_SAVED)
        assert len(version_rows(facts.db, own.id)) == 1, "no wording changed"
        assert event_types(facts.db, own.id) == ["created", "details_changed"]
        saved = facts.bank.facts.get(own.id)
        assert saved.role_id == role_id and saved.tags == ("logistics", "planning")
        assert set(saved.skills) == {"Scheduling", "Excel"}
        assert (saved.start_date, saved.end_date) == ("2022", "2023-06-30")

        # Unticking a skill removes it from the fact; the same wording, sent with other line
        # endings and spaces around it, is still the same wording.
        response = save_edit(facts, own.id, skills=["Excel"], text=f" {ROUTES}\r\n")
        assert response.status_code == 200
        assert facts.bank.facts.get(own.id).skills == ("Excel",)
        assert len(version_rows(facts.db, own.id)) == 1

        before = facts.snapshot()
        unchanged = save_edit(facts, own.id)
        assert unchanged.status_code == 200, (unchanged.status_code, unchanged.text[:300])
        assert facts.snapshot() == before, "the form as the page gave it changes nothing"


def test_editing_a_draft_keeps_it_a_draft_with_its_role(tmp_path: Path) -> None:
    """The full editor on a draft: its own draft role is offered and stays chosen, a save
    keeps it a draft (only Confirm confirms) and keeps what made it."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(
            IMPORT_A,
            DraftInput(text=ROUTES, role_key="r1", needs_check=True, original_answer=ROUTES),
            role=DraftRole(key="r1", employer=EMPLOYER, title=TITLE),
        )
        elements, values = open_edit(facts, draft.id)
        assert values[ROLE_FIELD] == draft.role_id, "the draft's own role stays chosen"
        assert draft.role_id in option_values(*_role_select(elements))

        response = send(facts, edit_url(draft.id), {**values, "text": REWORDED})

        assert_saved(facts, response, draft.id, SAID_DRAFT_SAVED)
        saved = facts.bank.facts.get(draft.id)
        assert saved.status.value == "draft" and saved.role_id == draft.role_id
        assert saved.needs_check, "ADR-091: an edit is not the user's confirmation"
        assert saved.origin_provider == "fake"
        assert role_row(facts.db, draft.role_id)["status"] == "draft"
        assert event_types(facts.db, draft.id) == ["created", "edited"]


def test_privacy_saved_and_shown(tmp_path: Path) -> None:
    """The privacy choice is saved as an event (not a version), shown checked in the editor
    and as a text badge in the Library; a new fact can be given one from the start."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)

        response = save_edit(facts, own.id, privacy="never_use")

        answer = assert_saved(facts, response, own.id, SAID_SAVED)
        assert fact_row(facts.db, own.id)["privacy"] == "never_use"
        assert event_types(facts.db, own.id) == ["created", "privacy_changed"]
        assert len(version_rows(facts.db, own.id)) == 1
        for elements in (answer, open_edit(facts, own.id)[0]):
            radios = controls(elements, editor(elements), PRIVACY_FIELD)
            assert [r.attrs.get("value") for r in radios if "checked" in r.attrs] == ["never_use"]
        listed = library(facts)
        block = required(listed, fact_id_attr(own.id))
        assert says(listed, BADGE_NEVER_USE, block), "a text badge, not a colour"

        # Wording and privacy in one save: one version, both events.
        response = save_edit(facts, own.id, privacy="generalize", text=REWORDED)
        assert response.status_code == 200
        assert facts.bank.facts.get(own.id).privacy is Privacy.GENERALIZE
        assert len(version_rows(facts.db, own.id)) == 2
        assert sorted(event_types(facts.db, own.id)) == sorted(
            ["created", "privacy_changed", "edited", "privacy_changed"]
        )

        # Back to normal: the badge goes.
        assert save_edit(facts, own.id, privacy="normal").status_code == 200
        listed = library(facts)
        block = required(listed, fact_id_attr(own.id))
        assert not any(says(listed, badge, block) for badge in PRIVACY_LABELS.values())

        created = save_new(facts, text=INVOICES, privacy="never_use")
        assert created.status_code == 200
        fact_id = newest_fact(facts)
        assert fact_row(facts.db, fact_id)["privacy"] == "never_use"
        assert facts.status(fact_id) == "confirmed"

        # A value that isn't a choice is refused and saves nothing.
        before = facts.snapshot()
        refused = save_edit(facts, own.id, privacy='secret"><b id="injected">')
        assert refused.status_code == 422 and 'id="injected"' not in refused.text
        assert facts.snapshot() == before


def test_editor_422_ties_errors_to_fields(tmp_path: Path) -> None:
    """A refused save answers 422 with the editor: the message tied to its field, that field
    focused, everything that was typed still there, and nothing saved, role and skills
    included."""
    with facts_app(tmp_path) as facts:
        role_id = seed_role(facts)
        typed(facts.bank, NEWSLETTER, skills=("Scheduling",))
        with pytest.raises(FactInputInvalid) as blank:
            facts.bank.facts.create(FactInput(text="  "))
        assert blank.value.field == "text"
        typed_in = {
            "text": "   ",
            ROLE_FIELD: role_id,
            NEW_ROLE_FIELDS[0]: "Example Freight Lda",
            NEW_ROLE_FIELDS[1]: "Dispatcher",
            SKILLS_FIELD: ["Scheduling"],
            NEW_SKILLS_FIELD: "Route planning",
            TAGS_FIELD: "logistics, planning",
            "start_date": "2022-03",
            "end_date": "2024",
            PRIVACY_FIELD: "generalize",
        }
        before = facts.snapshot()

        response = save_new(facts, **typed_in)

        elements, field = assert_refused(response, "text", blank.value.user_message)
        assert field.tag == "textarea"
        form = editor(elements)
        assert posts_to(form) == NEW_URL, "the form can be sent again"
        kept = form_values(elements, form)
        for name, value in typed_in.items():
            if name != "text":
                assert kept[name] == value, f"{name}: what was typed is kept"
        notes = described_by(elements, field)
        assert all(note.within(form) for note in notes), "the message sits in the form"
        assert facts.snapshot() == before, "no fact, no role and no skill were stored"

        # Without scripts: the full page, with the same marked field.
        plain = save_new(facts, htmx=False, **typed_in)
        assert plain.status_code == 422 and "<html" in plain.text
        page = outline(plain.text)
        (marked,) = invalid_fields(page, editor(page))
        assert marked.attrs.get("name") == "text"
        assert blank.value.user_message in [e.text for e in described_by(page, marked)]
        assert form_values(page, editor(page))[TAGS_FIELD] == "logistics, planning"
        assert facts.snapshot() == before

        # The same on an existing fact: the typed wording comes back, the stored one stays.
        own = typed(facts.bank, ROUTES)
        before = facts.snapshot()
        response = save_edit(facts, own.id, text="", tags="planning", end_date="2024")
        elements, field = assert_refused(response, "text", blank.value.user_message)
        kept = form_values(elements, editor(elements))
        assert kept["text"] == "" and kept[TAGS_FIELD] == "planning" and kept["end_date"] == "2024"
        assert kept["version"] == str(own.version)
        assert posts_to(editor(elements)) == edit_url(own.id)
        assert facts.snapshot() == before

        # A role that doesn't exist is tied to the role field.
        response = save_edit(facts, own.id, role_id="role-nobody")
        assert_refused(response, ROLE_FIELD)
        assert "role-nobody" not in response.text
        assert facts.snapshot() == before


def test_dates_validated_and_end_before_start_refused(tmp_path: Path) -> None:
    """Dates are a year, a month or a day. An end that is certainly before the start is
    refused on the end date; a less precise date that may be the same period is accepted."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        before = facts.snapshot()
        malformed = (
            ("start_date", "2024-13"),
            ("start_date", "March 2024"),
            ("end_date", "2024-02-30"),
            ("end_date", "soon"),
            ("end_date", "24"),
        )
        for name, value in malformed:
            for response in (
                save_new(facts, text=INVOICES, **{name: value}),
                save_edit(facts, own.id, **{name: value}),
            ):
                elements, _ = assert_refused(response, name, DATE_MESSAGE)
                assert form_values(elements, editor(elements))[name] == value
        backwards = (
            ("2024-05", "2023"),
            ("2024-05", "2024-04"),
            ("2024-05-10", "2024-05-09"),
            ("2024", "2023-12-31"),
        )
        for start, end in backwards:
            for response in (
                save_new(facts, text=INVOICES, start_date=start, end_date=end),
                save_edit(facts, own.id, start_date=start, end_date=end),
            ):
                elements, _ = assert_refused(response, "end_date", END_BEFORE_START)
                kept = form_values(elements, editor(elements))
                assert (kept["start_date"], kept["end_date"]) == (start, end)
        assert facts.snapshot() == before, "nothing refused was saved"

        possible = (("2024-05", "2024"), ("2024", "2024-01"), ("2024-05", "2024-05"), ("", "2020"))
        for start, end in possible:
            response = save_edit(facts, own.id, start_date=start, end_date=end)
            assert response.status_code == 200, (start, end, response.status_code)
            saved = facts.bank.facts.get(own.id)
            assert (saved.start_date or "", saved.end_date or "") == (start, end)


def test_service_refuses_an_end_before_the_start(tmp_path: Path) -> None:
    """The rule is the fact service's (routes hold no business rules): create and edit raise
    ``FactInputInvalid`` on ``end_date`` and write nothing."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES, start_date="2022", end_date="2023")
        before = facts.snapshot()
        with pytest.raises(FactInputInvalid) as created:
            facts.bank.facts.create(FactInput(text=INVOICES, start_date="2024-05", end_date="2023"))
        assert created.value.field == "end_date"
        assert created.value.user_message == END_BEFORE_START
        with pytest.raises(FactInputInvalid) as edited:
            facts.bank.facts.edit(
                own.id,
                FactInput(text=ROUTES, start_date="2024-05-10", end_date="2024-05-09"),
                expected_version=own.version,
            )
        assert edited.value.field == "end_date"
        assert facts.snapshot() == before
        kept = facts.bank.facts.create(
            FactInput(text=INVOICES, start_date="2024-05", end_date="2024")
        )
        assert (kept.start_date, kept.end_date) == ("2024-05", "2024")


# --- archive, restore and discarded drafts ------------------------------------------------------


def test_archive_and_restore_routes(tmp_path: Path) -> None:
    """A confirmed fact is archived at once and can be restored; both are events, never
    versions. Focus goes to the line that says what happened, never to the other button."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        elements, _ = open_edit(facts, own.id)
        archive = button(elements, required(elements, "main"), ARCHIVE)
        assert not buttons(elements, required(elements, "main"), RESTORE)
        path, values = submitted_by(elements, archive)
        assert path == archive_path(own.id) and values == {"version": str(own.version)}, (
            "a form of its own: it sends the version and no wording"
        )

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert "<html" not in response.text and announcement(response) == SAID_ARCHIVED
        assert facts.status(own.id) == "archived"
        answer = outline(response.text)
        (restore,) = [e for e in answer if e.tag == "button" and e.text == RESTORE]
        assert not [e for e in answer if e.tag == "button" and e.text == ARCHIVE]
        focus = only_focus(answer)
        assert focus is required(answer, EDITOR_STATE_STATUS_ID) and focus.tag != "button", (
            "the line that says so takes focus: a second Enter must not undo the archive"
        )
        assert SAID_ARCHIVED in focus.text
        path, values = submitted_by(answer, restore)
        assert path == restore_path(own.id)
        assert values == {"version": str(facts.bank.facts.get(own.id).version)}
        assert by_id(library(facts), fact_id_attr(own.id)) is None, "not in the default list"
        assert required(library(facts, status="archived"), fact_id_attr(own.id))
        # The archived fact's page offers Restore, not Archive.
        page, _ = open_edit(facts, own.id)
        assert [e.text for e in page if e.tag == "button" and e.text in (ARCHIVE, RESTORE)] == [
            RESTORE
        ]

        response = facts.post(path, values)

        assert response.status_code == 200 and announcement(response) == SAID_UNARCHIVED
        assert facts.status(own.id) == "confirmed"
        answer = outline(response.text)
        assert [e.text for e in answer if e.tag == "button" and e.text in (ARCHIVE, RESTORE)] == [
            ARCHIVE
        ]
        assert len(only_focus(answer).text) > 0
        assert event_types(facts.db, own.id) == ["created", "archived", "restored"]
        assert len(version_rows(facts.db, own.id)) == 1

        # Without scripts: 303 back to the fact's page.
        for make_path, status in ((archive_path, "archived"), (restore_path, "confirmed")):
            plain = facts.act(make_path(own.id), own.id, htmx=False)
            assert plain.status_code == 303 and plain.headers["location"] == edit_url(own.id)
            assert facts.status(own.id) == status


def test_archive_and_restore_refusals(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, DEGREE)
        before = facts.snapshot()
        # A draft is confirmed or discarded, never archived; nothing confirmed can be restored.
        refusals = (
            (archive_path(draft.id), draft.version),
            (restore_path(draft.id), draft.version),
            (restore_path(own.id), own.version),
        )
        for path, version in refusals:
            response = facts.post(path, {"version": str(version)})
            assert response.status_code == 409, (path, response.status_code)
            prompt = alert(outline(response.text), EDITOR_CONFLICT_ID)
            assert FactTransitionRefused.message in prompt.text
            assert "hx-trigger" not in response.headers
            assert facts.post(path, {"version": str(version)}, htmx=False).status_code == 409
        # A stale version, or none.
        for version in (str(own.version + 1), "", "one", '"><b id="injected">'):
            response = facts.post(archive_path(own.id), {"version": version})
            assert response.status_code == 409, version
            assert VersionConflict.message in response.text
            assert 'id="injected"' not in response.text
        assert facts.post(archive_path(own.id), {}).status_code == 409
        assert facts.snapshot() == before


def test_discarded_draft_found_in_library_and_restored(tmp_path: Path) -> None:
    """TD-195: a discarded draft can be found again after leaving the page, under the
    Library's Discarded filter, and restored there. A draft a merge closed has no Restore
    draft (its way back is Undo merge)."""
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, INVOICES)
        facts.add_import(IMPORT_A)
        gone, merged, kept = facts.drafts(IMPORT_A, ROUTES, DEGREE, NEWSLETTER)
        facts.bank.facts.discard(gone.id, expected_version=gone.version)
        facts.bank.facts.merge(
            merged.id,
            target.id,
            target.content,
            expected_draft_version=merged.version,
            expected_target_version=target.version,
        )

        for elements in (
            library(facts, status="discarded"),
            facts.fragment(LIBRARY_URL, status="discarded"),
        ):
            block = required(elements, fact_id_attr(gone.id))
            assert ROUTES in block.text
            restore = button(elements, block, RESTORE_DRAFT)
            assert names_fact(elements, restore, ROUTES)
            path, values = submitted_by(elements, restore)
            assert path == undo_path(gone.id)
            assert values == {
                "version": str(facts.bank.facts.get(gone.id).version),
                FROM_FIELD: "library",
            }
            assert not links(elements, EDIT, block), "a discarded draft is restored first"
            closed = required(elements, fact_id_attr(merged.id))
            assert not buttons(elements, closed, RESTORE_DRAFT)
            assert by_id(elements, fact_id_attr(kept.id)) is None

        response = facts.post(path, values)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_RESTORED
        assert facts.status(gone.id) == "draft"
        assert event_types(facts.db, gone.id) == ["created", "discarded", "discard_undone"]
        assert required(facts.fragment(DRAFTS_URL), fact_id_attr(gone.id))
        assert by_id(library(facts, status="discarded"), fact_id_attr(gone.id)) is None
        assert facts.status(merged.id) == "discarded"


# --- stale, unknown and hostile -----------------------------------------------------------------


def test_stale_editor_shows_reload_prompt_and_keeps_typing(tmp_path: Path) -> None:
    """The fact changed since the page showed it: 409 with the reload prompt, nothing saved,
    and what was typed is still in the answer so it isn't lost."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        _, values = open_edit(facts, own.id)
        facts.bank.facts.edit(own.id, FactInput(text=REWORDED), expected_version=own.version)
        before = facts.snapshot()
        typed_text = "Planned delivery routes in the stale tab."

        response = send(
            facts, edit_url(own.id), {**values, "text": typed_text, "privacy": "never_use"}
        )

        assert response.status_code == 409, (response.status_code, response.text[:300])
        assert "<html" not in response.text
        elements = outline(response.text)
        prompt = alert(elements, EDITOR_CONFLICT_ID)
        assert says(elements, VersionConflict.message, prompt)
        (reload,) = links(elements, RELOAD, prompt)
        assert href_parts(reload) == (edit_url(own.id), {}, "")
        assert only_focus(elements) is prompt
        assert "hx-trigger" not in response.headers, "nothing happened, so nothing is announced"
        kept = [e for e in elements if e.tag == "textarea" and e.attrs.get("name") == "text"]
        assert [e.text for e in kept] == [typed_text], "the typed wording can still be copied"

        plain = send(facts, edit_url(own.id), {**values, "text": typed_text}, htmx=False)
        assert plain.status_code == 409
        for version in ("", "one", "-1", "1.0", "99", '"><b id="injected">'):
            response = send(facts, edit_url(own.id), {**values, "version": version})
            assert response.status_code == 409, (version, response.status_code)
            assert 'id="injected"' not in response.text
        without = {name: value for name, value in values.items() if name != "version"}
        assert send(facts, edit_url(own.id), without).status_code == 409, "no version at all"
        assert facts.snapshot() == before, "nothing was saved"
        assert fact_row(facts.db, own.id)["privacy"] == "normal"

        # After a reload the form carries the stored version and saves.
        assert save_edit(facts, own.id, text=typed_text).status_code == 200
        assert current_version_row(facts.db, own.id)["text"] == typed_text


def test_discarded_fact_cannot_be_saved(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, ROUTES)
        _, values = open_edit(facts, draft.id)
        gone = facts.bank.facts.discard(draft.id, expected_version=draft.version)
        before = facts.snapshot()
        response = send(facts, edit_url(draft.id), {**values, "version": str(gone.version)})
        assert response.status_code == 409
        assert (
            FactTransitionRefused.message in alert(outline(response.text), EDITOR_CONFLICT_ID).text
        )
        assert facts.snapshot() == before


def test_unknown_fact_is_404(tmp_path: Path) -> None:
    """``FactNotFound``: 404 on every editor, history and merge route, with nothing of the
    request repeated."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        before = facts.snapshot()
        for unknown in ("nobody", "seed-9999", "fictional<b>id"):
            for path in (edit_url(unknown), merge_url(unknown)):
                for response in (facts.run.client.get(path), facts.get(path)):
                    assert response.status_code == 404, (path, response.status_code)
                    assert "fictional" not in response.text and "nobody" not in response.text
            response = facts.get(history_url(unknown))
            assert response.status_code == 404, response.status_code
            assert FactNotFound.message in response.text
            posts: dict[str, dict[str, str]] = {
                edit_url(unknown): {"version": "1", "type": "fact", "text": "x"},
                archive_path(unknown): {"version": "1"},
                restore_path(unknown): {"version": "1"},
                merge_url(unknown): {
                    "target": own.id,
                    "draft_version": "1",
                    "target_version": "1",
                    "wording": "target",
                },
                undo_merge_path(unknown): {"draft_version": "1", "target_version": "1"},
            }
            for path, data in posts.items():
                for htmx in (True, False):
                    response = facts.post(path, data, htmx=htmx)
                    assert response.status_code == 404, (path, htmx, response.status_code)
                    assert FactNotFound.message in response.text
                    assert "fictional" not in response.text and "nobody" not in response.text
        assert facts.snapshot() == before


def test_editor_escapes_hostile_text(tmp_path: Path) -> None:
    """Wording, role, skill and tag text is the user's or AI-made: always escaped (spec 2.9
    rule 4), in the page, the saved answer and a refused one."""
    with facts_app(tmp_path) as facts:
        response = save_new(
            facts,
            text=HOSTILE,
            new_role_employer=HOSTILE,
            new_role_title="Planner",
            new_skills='<i id="injected">Scheduling</i>',
            tags='"><b id="injected">',
        )
        assert response.status_code == 200, (response.status_code, response.text[:300])
        fact_id = newest_fact(facts)
        assert current_version_row(facts.db, fact_id)["text"] == HOSTILE, "stored as typed"
        refused = save_edit(facts, fact_id, text=HOSTILE + " again", end_date=HOSTILE)
        assert refused.status_code == 422
        shown = (
            response.text,
            refused.text,
            facts.run.client.get(edit_url(fact_id)).text,
            facts.run.client.get(NEW_URL).text,
            facts.get(PAGE, htmx=False, tab="library").text,
        )
        for text in shown:
            elements = outline(text)
            assert by_id(elements, "injected") is None and by_id(elements, "bold") is None
            assert not [e for e in elements if e.tag == "script" and "fictional" in e.text]
        assert "Planned routes" in required(library(facts), RESULTS_ID).text


# --- security ----------------------------------------------------------------------------------


def test_editor_routes_need_token_and_origin(tmp_path: Path) -> None:
    """Every M3.6 route that isn't a page refuses without the token, and each POST without
    the app's own origin too. A refusal changes nothing."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        archived = typed(facts.bank, NEWSLETTER)
        archived = facts.bank.facts.archive(archived.id, expected_version=archived.version)
        target = typed(facts.bank, INVOICES)
        facts.add_import(IMPORT_A)
        draft, merged = facts.drafts(IMPORT_A, DEGREE, "English")
        after = facts.bank.facts.merge(
            merged.id,
            target.id,
            target.content,
            expected_draft_version=merged.version,
            expected_target_version=target.version,
        )
        closed = facts.bank.facts.get(merged.id)
        token = facts.token
        refused = {
            "no token, no origin": {},
            "origin only": {"origin": ORIGIN},
            "token only": {TOKEN_HEADER: token},
            "wrong token": {TOKEN_HEADER: "x" * len(token), "origin": ORIGIN},
            "another site's origin": {TOKEN_HEADER: token, "origin": "http://evil.example"},
            "origin null": {TOKEN_HEADER: token, "origin": "null"},
        }
        posts: dict[str, dict[str, str]] = {
            NEW_URL: {"type": "fact", "text": "Typed by another site", "privacy": "normal"},
            edit_url(own.id): {
                "version": str(own.version),
                "type": "fact",
                "text": "Reworded by another site",
                "privacy": "never_use",
            },
            archive_path(own.id): {"version": str(own.version)},
            restore_path(archived.id): {"version": str(archived.version)},
            merge_url(draft.id): {
                "target": own.id,
                "draft_version": str(draft.version),
                "target_version": str(own.version),
                "wording": "draft",
            },
            undo_merge_path(merged.id): {
                "draft_version": str(closed.version),
                "target_version": str(after.version),
            },
        }
        before = facts.snapshot()
        for label, headers in refused.items():
            for path, data in posts.items():
                for htmx in ({}, {"HX-Request": "true"}):
                    response = facts.run.client.post(path, data=data, headers={**headers, **htmx})
                    assert response.status_code == 403, (label, path, response.status_code)
                    assert token not in response.text and ROUTES not in response.text
        # The history fragment is a GET: it needs the token (no origin).
        for headers in ({}, {"origin": ORIGIN}, {TOKEN_HEADER: "x" * len(token)}):
            response = facts.run.client.get(history_url(own.id), headers=headers)
            assert response.status_code == 403, headers
            assert ROUTES not in response.text
        # The POST-only routes are not pages: a GET never reaches them.
        for path in (archive_path(own.id), restore_path(archived.id), undo_merge_path(merged.id)):
            assert facts.run.client.get(path).status_code in (403, 404, 405), path
            got = facts.run.client.get(path, headers=facts.run.run.with_token())
            assert got.status_code in (404, 405), (path, got.status_code)
        assert facts.snapshot() == before, "a refused request changes nothing"

        # With the token and the origin the same requests are served: the refusals above were
        # the checks', not a missing route.
        assert facts.get(history_url(own.id)).status_code == 200

        def version(fact_id: str) -> str:
            return str(facts.bank.facts.get(fact_id).version)

        served = facts.post(undo_merge_path(merged.id), posts[undo_merge_path(merged.id)])
        assert served.status_code == 200, (served.status_code, served.text[:200])
        served = facts.post(restore_path(archived.id), posts[restore_path(archived.id)])
        assert served.status_code == 200, (served.status_code, served.text[:200])
        served = facts.post(merge_url(draft.id), posts[merge_url(draft.id)])
        assert served.status_code == 200, (served.status_code, served.text[:200])
        served = facts.post(NEW_URL, posts[NEW_URL])
        assert served.status_code == 200, (served.status_code, served.text[:200])
        served = facts.post(archive_path(target.id), {"version": version(target.id)})
        assert served.status_code == 200, (served.status_code, served.text[:200])
        assert facts.status(merged.id) == "draft" and facts.status(archived.id) == "confirmed"
        assert facts.status(draft.id) == "discarded" and facts.status(target.id) == "archived"
        saved = facts.post(
            edit_url(own.id), {**posts[edit_url(own.id)], "version": version(own.id)}
        )
        assert saved.status_code == 200
        assert fact_row(facts.db, own.id)["privacy"] == "never_use"


def test_editor_pages_are_pages_and_change_nothing(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: the three pages load without the token as full HTML, only without
    ``HX-Request``, and no GET or HEAD writes, starts a task or calls a provider, whatever
    its query says."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, DEGREE)
        data_dir = tmp_path / "data"
        before = database_state(data_dir)
        client = facts.run.client
        forged = {
            "version": str(own.version),
            "text": "Typed into the address",
            "privacy": "never_use",
            "wording": "draft",
            "draft_version": str(draft.version),
            "target_version": str(own.version),
            "action": "archive",
        }
        pages: list[tuple[str, dict[str, str]]] = [
            (NEW_URL, {}),
            (NEW_URL, {"type": "story", **forged}),
            (edit_url(own.id), {}),
            (edit_url(own.id), forged),
            (edit_url(draft.id), forged),
            (merge_url(draft.id), {}),
            (merge_url(draft.id), {"q": "routes"}),
            (merge_url(draft.id), {"target": own.id, **forged}),
        ]
        for path, query in pages:
            response = client.get(path, params=query)
            assert response.status_code == 200, (path, query, response.status_code)
            assert "<html" in response.text and "Typed into the address" not in response.text
            assert response.headers.get("cache-control") == "no-store"
            assert client.head(path, params=query).status_code == 200, path
            # With HX-Request a page is a fragment, and a fragment needs the token.
            bare = client.get(path, params=query, headers={"HX-Request": "true"})
            assert bare.status_code == 403, (path, bare.status_code)
            assert facts.get(path, **query).status_code == 200, path
        assert facts.get(history_url(own.id), **forged).status_code == 200
        assert facts.get(history_url(draft.id)).status_code == 200

        assert database_state(data_dir) == before, "nothing was written"
        assert facts.run.tasks() == [] and facts.run.spy.requests == []
        assert facts.status(own.id) == "confirmed" and facts.status(draft.id) == "draft"
        # The pages are not a way in for other methods.
        for path in (NEW_URL, edit_url(own.id), merge_url(draft.id), history_url(own.id)):
            for method in ("put", "delete", "patch"):
                response = getattr(client, method)(path, headers=facts.run.run.action())
                assert response.status_code == 405, (method, path, response.status_code)
        assert database_state(data_dir) == before


def test_fact_type_is_fixed_once_saved(tmp_path: Path) -> None:
    """The editor shows the fields of the item's own type; a forged ``type`` or a wording
    field the form doesn't show changes nothing it shouldn't."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        elements, values = open_edit(facts, own.id)
        form = editor(elements)
        assert values[TYPE_FIELD] == "fact"
        assert not controls(elements, form, "situation")
        response = send(
            facts, edit_url(own.id), {**values, TYPE_FIELD: "story", "result": "Forged result"}
        )
        assert response.status_code in (200, 422), response.status_code
        row = current_version_row(facts.db, own.id)
        assert row["result"] is None, "a field the form doesn't show is never stored"
        assert fact_row(facts.db, own.id)["type"] == FactType.FACT.value
