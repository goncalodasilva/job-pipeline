"""The Refine with AI panel through its routes (M3.9; AC-M3.5 UI; spec 3.1 item 5; ADR-086,
ADR-097 and its 2026-10-19 update).

The panel's contract (routes, ids, names and copy) is in ``tests/refine_panel.py``; the M3.8
routes it builds on in ``tests/refine_harness.py``. These tests read what the server renders:
the editor's entry point, the blocked state of a "never use" fact, the waiting state with
Cancel, the questions with the answer box, the suggestion as a diff, one checkbox per flagged
claim and Accept rendered ``aria-disabled`` until the claims are acknowledged. Behaviour in a
browser (the client guard, the live region, focus) is in
``tests/e2e/test_fact_refine_panel.py``.

Real SQLite under ``tmp_path``, the real runner, the fake provider behind the spy with the
fixtures of ``tests/fixtures/ai/refine_fact/``; no network, no fixed waits; every value is
fictional.
"""

import html
from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.modules.ai.providers.fake import ScriptedResponse
from tests.cv_import_harness import message, said
from tests.facts_page import (
    announcement,
    button,
    by_id,
    controls,
    described_by,
    edit_url,
    form_values,
    has_label,
    inside,
    label_of,
    names_fact,
    posts_to,
    required,
    says,
)
from tests.integration.test_setup_redirect import Element, outline
from tests.refine_harness import ACCEPTED, REFINE, TARGET, Refining, refine_path, refining
from tests.refine_panel import (
    ACCEPT,
    ACKNOWLEDGED_FIELD,
    ANSWER_FIELD,
    CANCEL,
    DIFF_ADDED,
    DIFF_REMOVED,
    DISCARD,
    EDIT_MYSELF,
    FLAGGED_ID,
    INVENTED,
    INVENTED_NAME,
    INVENTED_NUMBERS,
    NOT_IN_BANK,
    OK_SUGGESTION,
    PANEL_ID,
    PANEL_TARGET,
    PRACTICE_BADGE,
    QUESTION,
    QUESTIONS_ID,
    REFINE_BUTTON,
    REFRESH_EVENTS,
    SAID_STOPPED,
    SEND_ANSWER,
    SUGGESTION_ID,
    TRUE_ADD,
    VERSION_FIELD,
    WAITING,
    WORDING_FIELD,
)

pytestmark = pytest.mark.integration

UNKNOWN_ID = "f-does-not-exist"
HOSTILE = 'Cut invoice processing time by 35% across 4 warehouses <img src=x onerror="alert(1)">.'


# --- reading the panel -------------------------------------------------------------------------


def panel_get(r: Refining, fact_id: str, **headers: str) -> Response:
    """``GET /facts/{id}/refine`` as htmx sends it (token, ``HX-Request``)."""
    return r.client.get(refine_path(fact_id), headers=r.run.run.htmx(**headers))


def panel_of(response: Response) -> tuple[list[Element], Element]:
    assert response.status_code in (200, 409, 422), (response.status_code, response.text[:300])
    elements = outline(response.text)
    return elements, required(elements, PANEL_ID)


def shown_panel(r: Refining, fact_id: str) -> tuple[list[Element], Element]:
    response = panel_get(r, fact_id)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return panel_of(response)


def editor_page(r: Refining, fact_id: str) -> tuple[list[Element], Element]:
    response = r.client.get(edit_url(fact_id))
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "<html" in response.text, "the editor is a page"
    elements = outline(response.text)
    return elements, required(elements, PANEL_ID)


def form_of(control: Element) -> Element:
    node = control.parent
    while node is not None and node.tag != "form":
        node = node.parent
    assert node is not None, f"{control.tag} {control.text!r} is not in a form"
    return node


def named(elements: list[Element], parent: Element, name: str) -> list[Element]:
    """Links and buttons inside ``parent`` whose words are ``name``."""
    return [e for e in inside(elements, parent) if e.tag in ("a", "button") and e.text == name]


def assert_blocked(elements: list[Element], control: Element, reason: str) -> None:
    """A blocked action (ADR-086): focusable, aria-disabled, never ``disabled``, described by
    a reason in words."""
    assert control.attrs.get("aria-disabled") == "true", control.attrs
    assert "disabled" not in control.attrs, "a blocked action stays focusable"
    shown = " ".join(found.text for found in described_by(elements, control))
    assert reason in shown, (reason, shown)


def assert_not_blocked(control: Element) -> None:
    assert control.attrs.get("aria-disabled") in (None, "false"), control.attrs
    assert "disabled" not in control.attrs, control.attrs


def start_form(elements: list[Element], panel: Element, fact_id: str) -> Element:
    start = button(elements, panel, REFINE_BUTTON)
    form = form_of(start)
    assert posts_to(form) == refine_path(fact_id)
    assert form.attrs.get("hx-target") == PANEL_TARGET, form.attrs
    return start


def accept_parts(elements: list[Element], panel: Element, fact_id: str) -> tuple[Element, Element]:
    """The Accept button and its form."""
    accept = button(elements, panel, ACCEPT)
    form = form_of(accept)
    assert posts_to(form) == refine_path(fact_id, "accept")
    return accept, form


def refreshes(elements: list[Element], panel: Element, fact_id: str) -> bool:
    """Whether the panel, or an element in it, refetches the panel on the task events."""
    for element in [panel, *inside(elements, panel)]:
        trigger = element.attrs.get("hx-trigger") or ""
        if element.attrs.get("hx-get") == refine_path(fact_id) and all(
            event in trigger for event in REFRESH_EVENTS
        ):
            return True
    return False


def any_task_refresh(elements: list[Element], panel: Element) -> bool:
    return any(
        "tasks:" in (element.attrs.get("hx-trigger") or "")
        for element in [panel, *inside(elements, panel)]
    )


def squeezed(text: str) -> str:
    return text.replace(" ", "")


def flagged_turn(r: Refining) -> Any:
    fact = r.target()
    r.answer_with("adversarial_invented_claims")
    r.started(fact.id)
    return fact


def claims_of(r: Refining, fact_id: str) -> list[str]:
    from tests.refine_harness import claim_texts

    return claim_texts(r.open_session(fact_id)["flagged_claims"])


# --- the editor's entry point ------------------------------------------------------------------


def test_editor_page_offers_refine_with_ai_for_a_confirmed_fact_and_a_draft(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        (draft,) = r.drafts("Planned 60 delivery routes a week with a team of 3.")
        for fact_id in (fact.id, draft.id):
            elements, panel = editor_page(r, fact_id)
            start = start_form(elements, panel, fact_id)
            assert_not_blocked(start)
            heading = required(elements, "fact-refine-heading")
            assert heading.text == REFINE_BUTTON and heading.within(panel)
        assert r.requests() == [] and r.tasks() == [], "a page load starts nothing"
        assert r.sessions(fact.id) == []


def test_never_use_fact_editor_shows_refine_blocked_with_its_reason(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        secret = r.typed("Handled the confidential Brightwater Trust audit.", privacy="never_use")
        reason = message("RefineNeverUse")

        elements, panel = editor_page(r, secret.id)
        assert_blocked(elements, button(elements, panel, REFINE_BUTTON), reason)

        elements, panel = shown_panel(r, secret.id)
        assert_blocked(elements, button(elements, panel, REFINE_BUTTON), reason)
        assert r.requests() == [] and r.sessions(secret.id) == []


def test_never_use_start_answers_the_blocked_panel_409(tmp_path: Path) -> None:
    """The server refuses as the client guard does; the htmx answer is the panel (never bare
    text swapped into ``#fact-refine``), still blocked with its reason."""
    with refining(tmp_path) as r:
        secret = r.typed("Handled the confidential Brightwater Trust audit.", privacy="never_use")

        refused = r.post(refine_path(secret.id), htmx=True)

        assert refused.status_code == 409
        elements, panel = panel_of(refused)
        assert_blocked(elements, button(elements, panel, REFINE_BUTTON), message("RefineNeverUse"))
        assert r.requests() == [] and r.tasks() == []


# --- the panel's own GET -----------------------------------------------------------------------


def test_panel_get_is_a_token_fragment_with_no_side_effects(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        before = r.fact_tables()

        shown = panel_get(r, fact.id)

        assert shown.status_code == 200, (shown.status_code, shown.text[:300])
        assert "<html" not in shown.text, "a fragment, not a page"
        elements, panel = panel_of(shown)
        start_form(elements, panel, fact.id)
        assert r.client.get(refine_path(fact.id)).status_code == 403, "the token is needed"
        assert panel_get(r, UNKNOWN_ID).status_code == 404
        assert r.sessions(fact.id) == [] and r.tasks() == [] and r.requests() == []
        assert r.fact_tables() == before


# --- waiting, with Cancel ----------------------------------------------------------------------


def test_waiting_panel_has_cancel_and_refreshes_on_task_events(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.run.hold.hold = True
        try:
            started = r.post(refine_path(fact.id), htmx=True)
            r.run.wait_entered(1)
            for response in (started, panel_get(r, fact.id)):
                elements, panel = panel_of(response)
                assert response.status_code == 200
                assert WAITING in said(response), "the waiting line"
                cancel = button(elements, panel, CANCEL)
                assert posts_to(form_of(cancel)) == refine_path(fact.id, "discard")
                assert form_of(cancel).attrs.get("hx-target") == PANEL_TARGET
                assert named(elements, panel, EDIT_MYSELF), "Edit it myself while waiting"
                assert named(elements, panel, ACCEPT) == [], "nothing to accept yet"
                assert controls(elements, panel, ANSWER_FIELD) == [], "no answer box yet"
                assert refreshes(elements, panel, fact.id), "it refetches itself when the turn ends"
        finally:
            r.run.hold.open()


def test_cancel_while_waiting_discards_and_cancels_the_turn(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        before = r.fact_tables()
        r.answer_with("ok")
        r.run.hold.hold = True
        try:
            assert r.start(fact.id).status_code in ACCEPTED
            r.run.wait_entered(1)
            (task,) = r.tasks()
            elements, panel = shown_panel(r, fact.id)
            cancel_path = posts_to(form_of(button(elements, panel, CANCEL)))

            cancelled = r.post(cancel_path, htmx=True)

            assert cancelled.status_code == 200
            assert announcement(cancelled) == SAID_STOPPED
            elements, panel = panel_of(cancelled)
            assert says(elements, SAID_STOPPED, panel)
        finally:
            r.run.hold.open()
        assert str(r.run.wait_terminal(task.id).status.value) == "cancelled"
        assert [row["state"] for row in r.sessions(fact.id)] == ["discarded"]
        assert r.fact_tables() == before
        elements, panel = shown_panel(r, fact.id)
        start_form(elements, panel, fact.id)


# --- questions, the answer box and the diff ----------------------------------------------------


def test_answered_panel_shows_questions_answer_box_diff_and_practice_badge(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        elements, panel = shown_panel(r, fact.id)

        questions = required(elements, QUESTIONS_ID)
        assert questions.within(panel) and QUESTION in questions.text
        (answer,) = controls(elements, panel, ANSWER_FIELD)
        assert answer.tag == "textarea" and has_label(elements, answer)
        send = button(elements, panel, SEND_ANSWER)
        assert form_of(send) is form_of(answer)
        assert posts_to(form_of(answer)) == refine_path(fact.id, "answer")

        suggestion = required(elements, SUGGESTION_ID)
        assert suggestion.within(panel)
        removed = [e.text for e in inside(elements, suggestion) if e.tag == "del"]
        added = [e.text for e in inside(elements, suggestion) if e.tag == "ins"]
        assert any(t.startswith(DIFF_REMOVED) and "Cut" in t for t in removed), removed
        assert any(t.startswith(DIFF_ADDED) and "Reduced" in t for t in added), added
        assert not any("invoice processing time" in t for t in removed + added), (
            "words the suggestion keeps are not marked as changed"
        )
        assert says(elements, PRACTICE_BADGE, panel)

        accept, form = accept_parts(elements, panel, fact.id)
        assert_not_blocked(accept)
        values = form_values(elements, form)
        assert values[VERSION_FIELD] == str(fact.version)
        assert values[WORDING_FIELD] == OK_SUGGESTION
        (wording,) = controls(elements, form, WORDING_FIELD)
        assert wording.tag == "textarea" and has_label(elements, wording), "editable wording"
        assert by_id(elements, FLAGGED_ID) is None, "nothing flagged"
        assert named(elements, panel, EDIT_MYSELF)
        discard = button(elements, panel, DISCARD)
        assert posts_to(form_of(discard)) == refine_path(fact.id, "discard")
        assert not any_task_refresh(elements, panel), (
            "an answered panel is not refetched by task events: typing would be lost"
        )


def test_questions_only_panel_offers_answer_edit_and_discard(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)

        elements, panel = shown_panel(r, fact.id)

        assert (
            len([e for e in inside(elements, required(elements, QUESTIONS_ID)) if e.tag == "li"])
            == 2
        )
        assert controls(elements, panel, ANSWER_FIELD), "an answer box"
        assert by_id(elements, SUGGESTION_ID) is None and named(elements, panel, ACCEPT) == []
        assert named(elements, panel, EDIT_MYSELF) and button(elements, panel, DISCARD)


def test_editor_page_shows_the_open_conversation_after_a_reload(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        elements, panel = editor_page(r, fact.id)

        assert QUESTION in required(elements, QUESTIONS_ID).text
        assert required(elements, SUGGESTION_ID).within(panel)
        accept_parts(elements, panel, fact.id)
        assert named(elements, panel, REFINE_BUTTON) == [], "no second start while one is open"


def test_suggestion_and_questions_escaped_in_the_diff(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        data = {
            "questions": ["<b>Which</b> team?"],
            "suggestion": {"text": HOSTILE, "situation": None, "action": None, "result": None},
            "added_details": [],
        }
        r.run.spy.script(REFINE, ScriptedResponse(data=data))
        r.started(fact.id)

        shown = panel_get(r, fact.id)

        assert shown.status_code == 200
        assert "<img" not in shown.text and "<b>Which" not in shown.text
        assert "&lt;img" in shown.text and "&lt;b&gt;Which" in shown.text
        assert 'onerror="alert(1)">' in html.unescape(shown.text), "shown as words"


# --- flagged claims and Accept -----------------------------------------------------------------


def test_ac_m3_5_flagged_panel_has_a_checkbox_per_claim_and_accept_aria_disabled(
    tmp_path: Path,
) -> None:
    with refining(tmp_path) as r:
        fact = flagged_turn(r)
        claims = claims_of(r, fact.id)
        assert len(claims) >= 3, claims

        elements, panel = shown_panel(r, fact.id)

        flagged = required(elements, FLAGGED_ID)
        assert flagged.within(panel)
        assert says(elements, NOT_IN_BANK, flagged), "the line in words"
        accept, form = accept_parts(elements, panel, fact.id)
        boxes = controls(elements, form, ACKNOWLEDGED_FIELD)
        assert sorted(box.attrs.get("value") or "" for box in boxes) == sorted(claims)
        for box in boxes:
            claim = box.attrs.get("value") or ""
            assert box.attrs.get("type") == "checkbox" and "checked" not in box.attrs
            assert TRUE_ADD in label_of(elements, box), "labelled 'This is true; add it'"
            assert names_fact(elements, box, claim) or claim in label_of(elements, box), (
                f"the checkbox says which claim it is for: {claim!r}"
            )
            assert any(claim in e.text for e in inside(elements, flagged)), claim
        assert {squeezed(claim) for claim in claims} >= INVENTED_NUMBERS
        assert any(INVENTED_NAME in claim for claim in claims)
        assert form_values(elements, form)[WORDING_FIELD] == INVENTED
        assert_blocked(elements, accept, "")
        reason = " ".join(found.text for found in described_by(elements, accept))
        assert reason.strip(), "Accept says why it is blocked"
        assert named(elements, panel, EDIT_MYSELF) and button(elements, panel, DISCARD)
        assert r.current(fact.id).content.text == TARGET, "nothing applied"


def test_refused_accept_keeps_the_ticks_and_the_typed_wording(tmp_path: Path) -> None:
    """A forced Accept (no client guard) with one of three claims ticked: 422, and the panel
    comes back with that box still ticked, the others not, the wording as posted and Accept
    still aria-disabled."""
    with refining(tmp_path) as r:
        fact = flagged_turn(r)
        claims = claims_of(r, fact.id)
        typed = f"{INVENTED} "  # a trailing space: the answer keeps what was posted
        data = {
            VERSION_FIELD: [str(fact.version)],
            WORDING_FIELD: [typed],
            ACKNOWLEDGED_FIELD: claims[:1],
        }

        refused = r.post(refine_path(fact.id, "accept"), data, htmx=True)

        assert refused.status_code == 422
        elements, panel = panel_of(refused)
        accept, form = accept_parts(elements, panel, fact.id)
        boxes = {
            box.attrs.get("value"): "checked" in box.attrs
            for box in controls(elements, form, ACKNOWLEDGED_FIELD)
        }
        assert boxes == {claim: claim == claims[0] for claim in claims}, boxes
        assert form_values(elements, form)[WORDING_FIELD].strip() == INVENTED
        assert accept.attrs.get("aria-disabled") == "true"
        assert r.current(fact.id).content.text == TARGET


def test_accept_with_every_claim_ticked_answers_the_closed_panel(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = flagged_turn(r)
        claims = claims_of(r, fact.id)
        data = {
            VERSION_FIELD: [str(fact.version)],
            WORDING_FIELD: [INVENTED],
            ACKNOWLEDGED_FIELD: claims,
        }

        accepted = r.post(refine_path(fact.id, "accept"), data, htmx=True)

        assert accepted.status_code == 200
        elements, panel = panel_of(accepted)
        assert by_id(elements, FLAGGED_ID) is None
        start_form(elements, panel, fact.id)
        assert r.current(fact.id).content.text == INVENTED
