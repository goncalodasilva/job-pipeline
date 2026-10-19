"""The capture inbox on the Fact bank page (M3.7; detailed design "Fact bank", Capture inbox;
ADR-090 item 10, ADR-094 consequences).

The contract (routes, ids, copy) is in ``tests/facts_page.py`` under "M3.7". A note is added,
listed, turned into a fact through the editor (which sets ``resolved_fact_id``), or deleted
(soft, ``deleted_at``) with an Undo. A finished setup with the fake provider saved; a real
SQLite database under ``tmp_path``; no provider call; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from tests.facts_page import (
    ADD_NOTE,
    CAPTURE_EMPTY,
    CAPTURE_ERROR_ID,
    CAPTURE_FORM_ID,
    CAPTURE_ID,
    CAPTURE_LIST_ID,
    CAPTURE_STATUS_ID,
    CAPTURE_URL,
    DELETE_NOTE,
    NEW_URL,
    NOTE_FIELD,
    SAID_ADDED,
    SAID_NOTE_ADDED,
    SAID_NOTE_DELETED,
    SAID_NOTE_RESTORED,
    TURN_INTO_FACT,
    UNDO,
    FactsApp,
    announcement,
    button,
    by_id,
    capture_delete_path,
    capture_undo_path,
    controls,
    facts_app,
    focus_targets,
    form_values,
    has_label,
    href_parts,
    links,
    note_id_attr,
    page_of,
    posts_to,
    required,
    send,
    tab_url,
)
from tests.integration.test_setup_redirect import BASE_URL, Element, outline

pytestmark = pytest.mark.integration

NOTE = "Ran the fictional spring stocktake at the Example Freight depot in two days."
OTHER = "Ask Example Freight about the forklift course."
HOSTILE = '"><script id="injected">window.fictional=1</script>'


# --- helpers -----------------------------------------------------------------------------------


def notes(facts: FactsApp) -> list[dict[str, Any]]:
    return facts.run.rows("SELECT * FROM capture_notes ORDER BY rowid")


def capture_tab(facts: FactsApp) -> list[Element]:
    return facts.tab("capture")


def add_note(facts: FactsApp, text: str, *, htmx: bool = True) -> Response:
    return facts.post(CAPTURE_URL, {"text": text}, htmx=htmx)


def added(facts: FactsApp, text: str) -> None:
    """Seed a note through the route (htmx); fails plainly while the route is missing."""
    response = add_note(facts, text)
    assert response.status_code == 200, (CAPTURE_URL, response.status_code, response.text[:200])


def listed(elements: list[Element]) -> Element:
    return required(elements, CAPTURE_LIST_ID)


def note_block(elements: list[Element], note_id: str) -> Element | None:
    return by_id(elements, note_id_attr(note_id))


def assert_section_answer(response: Response, said: str) -> list[Element]:
    """An htmx answer: the ``#capture-inbox`` section, the status line saying ``said``, one
    focus target and the same words announced."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "<html" not in response.text, "an htmx answer is a fragment"
    elements = outline(response.text)
    required(elements, CAPTURE_ID)
    assert required(elements, CAPTURE_STATUS_ID).text.startswith(said)
    assert len(focus_targets(elements)) == 1
    assert announcement(response) == said
    return elements


# --- the tab -----------------------------------------------------------------------------------


def test_capture_tab_has_form_and_empty_list(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        elements = capture_tab(facts)
        form = required(elements, CAPTURE_FORM_ID)
        assert posts_to(form) == CAPTURE_URL
        assert form.attrs.get("hx-target") == f"#{CAPTURE_ID}", form.attrs
        (text,) = controls(elements, form, "text")
        assert text.tag == "textarea" and has_label(elements, text)
        button(elements, form, ADD_NOTE)
        assert CAPTURE_EMPTY in required(elements, CAPTURE_ID).text


def test_note_added_listed_and_resolved_by_fact(tmp_path: Path) -> None:
    """Add a note, see it listed, follow "Turn into a fact": the editor holds its text; saving
    adds a confirmed fact, records it on the note, and the note leaves the inbox."""
    with facts_app(tmp_path) as facts:
        elements = assert_section_answer(add_note(facts, NOTE), SAID_NOTE_ADDED)
        (row,) = notes(facts)
        note_id = str(row["id"])
        assert (row["text"], row["resolved_fact_id"], row["deleted_at"]) == (NOTE, None, None)
        block = note_block(elements, note_id)
        assert block is not None and NOTE in block.text and block.within(listed(elements))

        page = capture_tab(facts)
        block = note_block(page, note_id)
        assert block is not None, "the note is listed on the tab"
        (turn,) = links(page, TURN_INTO_FACT, block)
        path, query, _ = href_parts(turn)
        assert path == NEW_URL
        assert query.get(NOTE_FIELD) == note_id
        assert query.get("type", "fact") == "fact"
        button(page, block, DELETE_NOTE)

        editor_page = page_of(facts, path, **query)
        form = required(editor_page, "fact-editor")
        values = form_values(editor_page, form)
        assert values["text"] == NOTE, "the note's text is typed in"
        assert values[NOTE_FIELD] == note_id

        response = send(facts, NEW_URL, values)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert announcement(response) == SAID_ADDED
        (fact,) = facts.run.facts()
        assert (fact["status"], fact["text"], fact["provider"]) == ("confirmed", NOTE, None)
        (row,) = notes(facts)
        assert row["resolved_fact_id"] == fact["id"]
        assert row["deleted_at"] is None

        after = capture_tab(facts)
        assert note_block(after, note_id) is None, "a note turned into a fact leaves the inbox"
        assert CAPTURE_EMPTY in required(after, CAPTURE_ID).text


def test_plain_add_redirects_to_the_tab(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, OTHER, htmx=False)
        assert response.status_code == 303
        assert response.headers["location"] == tab_url("capture")
        assert [row["text"] for row in notes(facts)] == [OTHER]


@pytest.mark.parametrize("blank", ["", "   \n\t "])
def test_blank_note_refused_and_tied_to_field(tmp_path: Path, blank: str) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, blank)
        assert response.status_code == 422, (response.status_code, response.text[:300])
        elements = outline(response.text)
        form = required(elements, CAPTURE_FORM_ID)
        (text,) = controls(elements, form, "text")
        assert text.attrs.get("aria-invalid") == "true"
        assert CAPTURE_ERROR_ID in (text.attrs.get("aria-describedby") or "").split()
        assert required(elements, CAPTURE_ERROR_ID).text
        assert notes(facts) == []


def test_note_text_is_escaped(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, HOSTILE)
        assert 'id="injected"' not in response.text
        assert 'id="injected"' not in facts.get("/facts", htmx=False, tab="capture").text


def test_delete_note_undo(tmp_path: Path) -> None:
    """Delete is soft (``deleted_at``); the answer offers Undo, which brings the note back."""
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        added(facts, OTHER)
        kept, gone = (str(row["id"]) for row in notes(facts))

        elements = assert_section_answer(facts.post(capture_delete_path(gone)), SAID_NOTE_DELETED)
        assert note_block(elements, gone) is None
        assert note_block(elements, kept) is not None, "only that note is deleted"
        undo = button(elements, required(elements, CAPTURE_ID), UNDO)
        undo_form = undo.parent
        while undo_form is not None and undo_form.tag != "form":
            undo_form = undo_form.parent
        assert undo_form is not None and posts_to(undo_form) == capture_undo_path(gone)
        by_note = {str(row["id"]): row for row in notes(facts)}
        assert by_note[gone]["deleted_at"] is not None, "soft: the row stays"
        assert by_note[kept]["deleted_at"] is None
        assert note_block(capture_tab(facts), gone) is None

        restored = assert_section_answer(facts.post(capture_undo_path(gone)), SAID_NOTE_RESTORED)
        assert note_block(restored, gone) is not None
        assert {str(row["id"]): row["deleted_at"] for row in notes(facts)} == {
            kept: None,
            gone: None,
        }
        assert note_block(capture_tab(facts), gone) is not None

        plain = facts.post(capture_delete_path(kept), htmx=False)
        assert plain.status_code == 303
        assert plain.headers["location"] == tab_url("capture")


def test_capture_routes_need_token_and_origin(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        (row,) = notes(facts)
        note_id = str(row["id"])
        client = facts.run.client
        for path, data in (
            (CAPTURE_URL, {"text": OTHER}),
            (capture_delete_path(note_id), {}),
            (capture_undo_path(note_id), {}),
        ):
            no_token = client.post(path, data=data, headers={"origin": BASE_URL})
            bad_origin = client.post(
                path, data=data, headers=facts.run.run.action(origin="http://evil.example")
            )
            assert no_token.status_code == 403, path
            assert bad_origin.status_code == 403, path
        assert [(r["text"], r["deleted_at"]) for r in notes(facts)] == [(NOTE, None)]
