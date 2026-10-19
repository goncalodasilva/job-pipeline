"""Refine with AI panel, review round (M3.9; AC-M3.5 UI; ADR-086, ADR-097).

The panel's refetch (``?was=<phase>``): 204 while nothing changed, the ready line and its
announcement once the turn ended; a turn that ended before its start's answer; the failed
states with "Try again" or the way to Tasks; a stale suggestion; a fact blocked while a
conversation is open; the panel sent out of band with Archive and Restore; the editor's parts
sent out of band after an Accept; the busy refusals in words; Cancel's hint and answer; refused
fields tied to their message; three answers used. Contract: ``tests/refine_panel.py``.

Real SQLite under ``tmp_path``, the real runner, the fake provider behind the spy; no network,
no fixed waits; every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.services.fact_refine import MSG_ANSWER_NEEDED, MSG_WORDING_NEEDED
from tests.cv_import_harness import message, said
from tests.facts_page import (
    announcement,
    button,
    by_id,
    controls,
    described_by,
    focus_targets,
    form_values,
    inside,
    posts_to,
    required,
    says,
)
from tests.integration.test_fact_refine_panel import (
    accept_parts,
    any_task_refresh,
    form_of,
    named,
    panel_get,
    panel_of,
    refreshes,
    shown_panel,
    start_form,
)
from tests.integration.test_setup_redirect import Element, outline
from tests.refine_harness import TARGET, Refining, refine_path, refining
from tests.refine_panel import (
    ACCEPT,
    ANSWER_FIELD,
    ANSWERS_USED,
    CANCEL,
    CANCEL_HINT,
    CANCEL_ID,
    DISCARD,
    EDIT_ID,
    EDIT_MYSELF,
    OK_SUGGESTION,
    PANEL_ID,
    REFINE_BUTTON,
    SAID_ACCEPTED,
    SAID_DISCARDED,
    SAID_FAILED,
    SAID_QUESTIONS_READY,
    SAID_READY,
    SAID_STARTED,
    SAID_STOPPED,
    SEND_ANSWER,
    STALE_NOTE,
    STATUS_ID,
    TRY_AGAIN,
    TRY_FROM_TASKS,
    WAS_FIELD,
    WORDING_FIELD,
)

pytestmark = pytest.mark.integration

EDITED = "Cut invoice processing time by 35% across 4 regional warehouses."


def refetch(r: Refining, fact_id: str, was: str) -> Response:
    """The panel's own refetch, as its status line sends it."""
    return r.client.get(refine_path(fact_id), params={WAS_FIELD: was}, headers=r.run.run.htmx())


def after_settle(response: Response) -> str:
    import json

    trigger = json.loads(response.headers.get("hx-trigger-after-settle", "{}"))
    return str(trigger.get("announce", ""))


def buttons_in(elements: list[Element], panel: Element) -> set[str]:
    return {e.text for e in inside(elements, panel) if e.tag == "button"}


def held_start(r: Refining, fact_id: str) -> Any:
    """Start a turn and hold it in the provider call; returns its task."""
    r.answer_with("ok")
    r.run.hold.hold = True
    r.post(refine_path(fact_id), htmx=True)
    r.run.wait_entered(1)
    (task,) = r.tasks()
    return task


def edit_fact(r: Refining, fact: Any, **fields: Any) -> Any:
    from app.services.facts import FactInput

    current = r.current(fact.id)
    data = {"text": current.content.text, "role_id": current.role_id, **fields}
    return r.facts.edit(fact.id, FactInput(**data), expected_version=current.version)


# --- the refetch -------------------------------------------------------------------------------


def test_waiting_refetch_with_nothing_changed_answers_204_and_controls_keep_ids(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        try:
            held_start(r, fact.id)
            elements, panel = shown_panel(r, fact.id)
            cancel = button(elements, panel, CANCEL)
            assert cancel.attrs.get("id") == CANCEL_ID
            (edit,) = named(elements, panel, EDIT_MYSELF)
            assert edit.tag == "button" and edit.attrs.get("type") == "button"
            assert edit.attrs.get("id") == EDIT_ID
            hint = [e for e in described_by(elements, cancel) if e.text == CANCEL_HINT]
            assert hint, "Cancel says it ends the conversation"
            status = required(elements, STATUS_ID)
            assert refreshes(elements, panel, fact.id)
            assert '"was"' in (status.attrs.get("hx-vals") or "")

            same = refetch(r, fact.id, "waiting")

            assert same.status_code == 204, same.status_code
            assert same.text == ""
        finally:
            r.run.hold.open()


def test_refetch_after_the_turn_ended_shows_and_says_ready(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        try:
            task = held_start(r, fact.id)
        finally:
            r.run.hold.open()
        r.run.wait_terminal(task.id)

        ended = refetch(r, fact.id, "waiting")

        assert ended.status_code == 200
        assert announcement(ended) == SAID_READY
        elements, panel = panel_of(ended)
        status = required(elements, STATUS_ID)
        assert status.text == SAID_READY and status.within(panel)
        assert "data-autofocus" not in status.attrs, "a refresh never takes focus"
        assert not any_task_refresh(elements, panel)


def test_questions_only_turn_ending_says_its_questions_are_ready(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)

        ended = refetch(r, fact.id, "waiting")

        assert announcement(ended) == SAID_QUESTIONS_READY


def test_turn_ended_before_the_start_answer_announces_ready_after_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        service = r.run.app.state.fact_refine
        start = service.start

        def start_and_finish(*args: Any, **kwargs: Any) -> Any:
            ref = start(*args, **kwargs)
            r.run.wait_terminal(ref.id)
            return ref

        monkeypatch.setattr(service, "start", start_and_finish)

        started = r.post(refine_path(fact.id), htmx=True)

        assert started.status_code == 200
        assert announcement(started) == SAID_STARTED
        assert after_settle(started) == SAID_READY
        elements, panel = panel_of(started)
        status = required(elements, STATUS_ID)
        assert status.text == SAID_READY and "data-autofocus" in status.attrs
        assert not any_task_refresh(elements, panel)


def test_panel_get_of_an_id_of_another_shape_is_the_fixed_404(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        for bad in ("a/b", "f-does-not-exist"):
            got = panel_get(r, bad)
            assert got.status_code == 404, (bad, got.status_code)
            assert bad not in got.text


# --- failed turns ------------------------------------------------------------------------------


def test_failed_first_turn_offers_try_again_and_keeps_refreshing(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("malformed")
        r.started(fact.id, status="failed")

        elements, panel = shown_panel(r, fact.id)

        assert required(elements, STATUS_ID).text == SAID_FAILED
        assert refreshes(elements, panel, fact.id), "a Retry from Tasks would show"
        retry = button(elements, panel, TRY_AGAIN)
        assert posts_to(form_of(retry)) == refine_path(fact.id)
        assert form_of(retry).attrs.get("hx-target") == f"#{PANEL_ID}"
        assert TRY_FROM_TASKS not in said(panel_get(r, fact.id))
        assert refetch(r, fact.id, "failed").status_code == 204
        assert announcement(refetch(r, fact.id, "waiting")) == SAID_FAILED

        r.answer_with("ok")
        r.ended_turn(lambda: r.post(posts_to(form_of(retry)), htmx=True))
        elements, panel = shown_panel(r, fact.id)
        accept_parts(elements, panel, fact.id)


def test_failed_turn_after_an_answer_points_to_tasks(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only", "malformed")
        r.started(fact.id)
        r.answered(fact.id, "About 1,200 invoices a month.", status="failed")

        elements, panel = shown_panel(r, fact.id)

        assert says(elements, TRY_FROM_TASKS, panel)
        assert named(elements, panel, TRY_AGAIN) == []
        assert button(elements, panel, DISCARD)


# --- a stale suggestion, a blocked fact --------------------------------------------------------


def test_suggestion_made_before_the_fact_changed_blocks_accept_with_its_reason(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        edit_fact(r, fact, text=EDITED)

        elements, panel = shown_panel(r, fact.id)

        accept, form = accept_parts(elements, panel, fact.id)
        assert accept.attrs.get("aria-disabled") == "true" and "disabled" not in accept.attrs
        reason = " ".join(found.text for found in described_by(elements, accept))
        assert STALE_NOTE in reason
        assert "data-refine-stale" in form.attrs, "the client check leaves it blocked"
        assert button(elements, panel, DISCARD) and named(elements, panel, EDIT_MYSELF)

        refused = r.accept(fact.id, text=OK_SUGGESTION)
        assert refused.status_code == 409
        assert r.current(fact.id).content.text == EDITED


def test_blocked_fact_with_an_open_conversation_offers_only_discard_and_edit(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        edit_fact(r, fact, privacy="never_use")

        elements, panel = shown_panel(r, fact.id)

        assert says(elements, message("RefineNeverUse"), panel)
        assert buttons_in(elements, panel) == {DISCARD, EDIT_MYSELF}
        assert controls(elements, panel, ANSWER_FIELD) == []
        assert controls(elements, panel, WORDING_FIELD) == []
        assert not any_task_refresh(elements, panel)


def test_blocked_fact_while_its_turn_waits_offers_discard_not_cancel(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        try:
            held_start(r, fact.id)
            edit_fact(r, fact, privacy="never_use")

            elements, panel = shown_panel(r, fact.id)

            assert says(elements, message("RefineNeverUse"), panel)
            assert buttons_in(elements, panel) == {DISCARD, EDIT_MYSELF}
        finally:
            r.run.hold.open()


# --- out of band -------------------------------------------------------------------------------


def oob_panel(response: Response) -> tuple[list[Element], Element]:
    assert response.status_code == 200, (response.status_code, response.text[:300])
    elements = outline(response.text)
    panel = required(elements, PANEL_ID)
    assert panel.attrs.get("hx-swap-oob") == "true", panel.attrs
    return elements, panel


def test_archive_and_restore_answers_carry_the_panel_out_of_band(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()

        archived = r.post(f"/facts/{fact.id}/archive", {"version": str(fact.version)}, htmx=True)

        elements, panel = oob_panel(archived)
        start = button(elements, panel, REFINE_BUTTON)
        assert start.attrs.get("aria-disabled") == "true"
        shown = " ".join(found.text for found in described_by(elements, start))
        assert message("RefineNotAvailable") in shown

        current = r.current(fact.id)
        restored = r.post(f"/facts/{fact.id}/restore", {"version": str(current.version)}, htmx=True)

        elements, panel = oob_panel(restored)
        start = start_form(elements, panel, fact.id)
        assert start.attrs.get("aria-disabled") is None
        assert not focus_targets(inside(elements, panel)), "the panel takes no focus"


def test_accept_answer_refreshes_the_editor_out_of_band(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        data = {"version": [str(fact.version)], WORDING_FIELD: [OK_SUGGESTION]}
        accepted = r.post(refine_path(fact.id, "accept"), data, htmx=True)

        assert accepted.status_code == 200 and announcement(accepted) == SAID_ACCEPTED
        new = r.current(fact.id).version
        assert new == fact.version + 1
        elements = outline(accepted.text)
        version = required(elements, "fact-editor-version")
        assert version.attrs.get("hx-swap-oob") == "true"
        assert version.attrs.get("value") == str(new)
        wording = required(elements, "editor-text")
        assert wording.tag == "textarea" and wording.attrs.get("hx-swap-oob") == "true"
        assert wording.text == OK_SUGGESTION and wording.attrs.get("name") == "text"
        state = required(elements, "fact-editor-state")
        assert state.attrs.get("hx-swap-oob") == "true"
        archive_form = next(e for e in inside(elements, state) if e.tag == "form")
        assert form_values(elements, archive_form)["version"] == str(new)
        history = required(elements, "fact-history")
        assert history.attrs.get("hx-swap-oob") == "true"
        assert by_id(elements, f"fact-version-{new}") is not None


# --- refusals in words -------------------------------------------------------------------------


def test_busy_refusals_say_why_in_their_own_words(tmp_path: Path) -> None:
    waiting_copy = "Wait for its answer"
    with refining(tmp_path) as r:
        fact = r.target()
        try:
            held_start(r, fact.id)

            running = r.answer(fact.id, "About 1,200 invoices a month.")
            assert running.status_code == 409
            assert message("RefineTurnRunning") in said(running)

            r.post(refine_path(fact.id, "discard"), htmx=True)
            again = r.post(refine_path(fact.id), htmx=True)
            assert again.status_code == 409
            assert message("RefineStopping") in said(again)
            assert waiting_copy not in said(again)
        finally:
            r.run.hold.open()

    with refining(tmp_path / "second") as r:
        fact = r.target()
        r.answer_with("uses_answer")  # a suggestion, and no question
        r.started(fact.id)
        nothing_asked = r.answer(fact.id, "About 1,200 invoices a month.")
        assert nothing_asked.status_code == 409
        assert waiting_copy not in said(nothing_asked)


def test_cancel_before_any_suggestion_says_stopped(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        try:
            held_start(r, fact.id)
            cancelled = r.post(refine_path(fact.id, "discard"), htmx=True)
        finally:
            r.run.hold.open()

        assert cancelled.status_code == 200 and announcement(cancelled) == SAID_STOPPED
        assert SAID_DISCARDED not in said(cancelled)

        r.answer_with("ok")
        # The cancelled turn must settle before another conversation opens.
        r.run.wait_terminal(r.tasks()[0].id)
        r.started(fact.id)
        discarded = r.post(refine_path(fact.id, "discard"), htmx=True)
        assert announcement(discarded) == SAID_DISCARDED


def assert_tied(elements: list[Element], field: Element, text: str) -> None:
    assert field.attrs.get("aria-invalid") == "true", field.attrs
    assert "data-autofocus" in field.attrs
    assert any(text == found.text for found in described_by(elements, field)), text
    assert len(focus_targets(elements)) == 1


def test_refused_answer_and_wording_are_tied_to_their_fields(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        blank = r.post(refine_path(fact.id, "answer"), {"answer": "   "}, htmx=True)
        assert blank.status_code == 422
        elements, panel = panel_of(blank)
        (answer,) = controls(elements, panel, ANSWER_FIELD)
        assert_tied(elements, answer, MSG_ANSWER_NEEDED)

        data = {"version": [str(fact.version)], WORDING_FIELD: ["   "]}
        empty = r.post(refine_path(fact.id, "accept"), data, htmx=True)
        assert empty.status_code == 422
        elements, panel = panel_of(empty)
        (wording,) = controls(elements, panel, WORDING_FIELD)
        assert_tied(elements, wording, MSG_WORDING_NEEDED)


def test_three_answers_used_replaces_the_answer_box(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")  # every turn asks again
        r.started(fact.id)
        for number in range(3):
            r.answered(fact.id, f"About {1200 + number} invoices a month.")

        elements, panel = shown_panel(r, fact.id)

        assert controls(elements, panel, ANSWER_FIELD) == []
        assert named(elements, panel, SEND_ANSWER) == []
        assert says(elements, ANSWERS_USED, panel)
        assert named(elements, panel, ACCEPT) and button(elements, panel, DISCARD)
        assert r.current(fact.id).content.text == TARGET
