"""The capture inbox after review (M3.7): actions on a note that is no longer where the page
thought, ids that name nothing, refused notes, and turning a note into a fact when the save is
refused or the note is gone.

Builds on ``tests/integration/test_capture_inbox.py``. A finished setup with the fake provider
saved; a real SQLite database under ``tmp_path``; no provider call; every value is fictional.
"""

from pathlib import Path

import pytest

from tests.facts_page import (
    CAPTURE_ERROR_ID,
    CAPTURE_FORM_ID,
    CAPTURE_ID,
    CAPTURE_STATUS_ID,
    NEW_URL,
    NOTE_FIELD,
    SAID_NOTE_ADDED,
    UNDO,
    announcement,
    buttons,
    capture_delete_path,
    capture_undo_path,
    controls,
    facts_app,
    focus_targets,
    form_values,
    note_id_attr,
    page_of,
    required,
    send,
)
from tests.integration.test_capture_inbox import NOTE, OTHER, add_note, added, notes
from tests.integration.test_setup_redirect import outline

pytestmark = pytest.mark.integration

NOT_IN_INBOX = "That note is no longer in the inbox."
ALREADY_IN_INBOX = "That note is already in the inbox."
NOTE_LINE = "Made from your capture note. Saving it takes the note out of the Capture inbox."
MAX_NOTE_CHARS = 5000


def test_deleting_a_note_no_longer_in_the_inbox_says_so(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        (row,) = notes(facts)
        note_id = str(row["id"])
        facts.post(capture_delete_path(note_id))
        deleted_at = notes(facts)[0]["deleted_at"]

        again = facts.post(capture_delete_path(note_id))

        assert again.status_code == 200
        assert announcement(again) == NOT_IN_INBOX
        shown = outline(again.text)
        assert required(shown, CAPTURE_STATUS_ID).text == NOT_IN_INBOX
        assert buttons(shown, required(shown, CAPTURE_ID), UNDO) == [], "nothing to undo"
        assert notes(facts)[0]["deleted_at"] == deleted_at, "left as it was"


def test_undo_of_a_note_that_was_not_deleted_changes_nothing(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        (row,) = notes(facts)
        before = facts.snapshot()

        response = facts.post(capture_undo_path(str(row["id"])))

        assert response.status_code == 200
        assert announcement(response) == ALREADY_IN_INBOX
        assert facts.snapshot() == before


@pytest.mark.parametrize("note_id", ["no-such-note", "..", "a b", "x" * 65])
def test_unknown_or_unsafe_note_ids_are_a_fixed_404(tmp_path: Path, note_id: str) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        before = facts.snapshot()
        for path in (capture_delete_path(note_id), capture_undo_path(note_id)):
            response = facts.post(path)
            assert response.status_code == 404, (path, response.status_code)
            assert note_id not in response.text
        assert facts.snapshot() == before


def test_note_longer_than_the_limit_is_refused(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, "x" * (MAX_NOTE_CHARS + 1))
        assert response.status_code == 422
        shown = outline(response.text)
        (text,) = controls(shown, required(shown, CAPTURE_FORM_ID), "text")
        assert text.attrs.get("aria-invalid") == "true"
        assert required(shown, CAPTURE_ERROR_ID).text
        assert notes(facts) == []
        assert add_note(facts, "x" * MAX_NOTE_CHARS).status_code == 200, "the limit itself is fine"


def test_plain_blank_note_answers_the_page_with_the_error(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, "   ", htmx=False)
        assert response.status_code == 422
        assert "<html" in response.text
        shown = outline(response.text)
        (text,) = controls(shown, required(shown, CAPTURE_FORM_ID), "text")
        assert text.attrs.get("aria-invalid") == "true"
        assert CAPTURE_ERROR_ID in (text.attrs.get("aria-describedby") or "").split()
        assert [e.attrs.get("id") for e in focus_targets(shown)] == [text.attrs.get("id")]
        assert notes(facts) == []


def test_refused_save_keeps_the_note_field(tmp_path: Path) -> None:
    """The editor made from a note says so; a refused save shows the form again still
    naming the note, and the note stays in the inbox."""
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        note_id = str(notes(facts)[0]["id"])
        editor = page_of(facts, NEW_URL, note=note_id)
        assert required(editor, "fact-editor-note").text == NOTE_LINE
        values = form_values(editor, required(editor, "fact-editor"))

        refused = send(facts, NEW_URL, {**values, "text": "  "})

        assert refused.status_code == 422
        shown = outline(refused.text)
        assert form_values(shown, required(shown, "fact-editor"))[NOTE_FIELD] == note_id
        assert facts.run.facts() == []
        assert notes(facts)[0]["resolved_fact_id"] is None


@pytest.mark.parametrize("gone", ["deleted", "resolved"])
def test_saving_with_a_note_no_longer_open_leaves_it_unchanged(tmp_path: Path, gone: str) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        note_id = str(notes(facts)[0]["id"])
        editor = page_of(facts, NEW_URL, note=note_id)
        values = form_values(editor, required(editor, "fact-editor"))
        if gone == "deleted":
            facts.post(capture_delete_path(note_id))
        else:
            assert send(facts, NEW_URL, values).status_code == 200
        before = notes(facts)[0]

        response = send(facts, NEW_URL, {**values, "text": OTHER})

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert notes(facts)[0] == before, "the note is left as it was"
        assert OTHER in [fact["text"] for fact in facts.run.facts()]


def test_editor_from_a_note_no_longer_open_is_empty(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        added(facts, NOTE)
        note_id = str(notes(facts)[0]["id"])
        facts.post(capture_delete_path(note_id))
        for query in (note_id, "no-such-note", "../x"):
            editor = page_of(facts, NEW_URL, note=query)
            values = form_values(editor, required(editor, "fact-editor"))
            assert values["text"] == "" and NOTE_FIELD not in values
            assert NOTE not in str(values)


def test_added_note_answer_still_lists_it(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        response = add_note(facts, NOTE)
        assert announcement(response) == SAID_NOTE_ADDED
        note_id = str(notes(facts)[0]["id"])
        assert required(outline(response.text), note_id_attr(note_id))
