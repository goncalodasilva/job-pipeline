"""Review fixes for adapting an earlier answer (M8.2 part C): what the screen of a question says
and how its forms swap. The routes, the stored shape and the first markup are pinned by
``tests/integration/test_prep_adapt.py`` (assumed interface in ``tests/prep_adapt_pages.py``);
the narrative cards this mirrors are pinned by ``tests/integration/test_prep_cards_review.py``.

The fixes pinned here:

- Every adapt form swaps only ``section#adapt`` (``hx-target`` and ``hx-select``), so a
  half-typed practice answer or an open edit elsewhere is not wiped; everything an adapt action
  can change outside its paragraphs (alerts, the reload prompt, the budget question) is INSIDE
  the section, and the section polls itself while a task runs.
- The wording: the whole earlier answer is sent as written; "Adapt again"; a stale press says
  nothing was started; editing is paused while a task runs.
"""

import re
from typing import Any

import pytest

from app.services.documents import ApprovalBlocked
from tests.board_pages import elements
from tests.integration.test_prep_adapt import adapted_page, tag
from tests.integration.test_prep_practice import block_budget
from tests.prep_adapt_pages import OLD, P1, AdaptDesk, adapt_app
from tests.tailoring_pages import by_id, has_id, said_text

pytestmark = pytest.mark.integration

RECHECK = (
    "Edit, remove or confirm the flagged paragraphs: that saves a new draft, and you approve it "
    "again. You can also adapt again."
)


@pytest.fixture
def d(tmp_path):
    with adapt_app(tmp_path) as desk:
        yield desk


def attr_of(page: str, element_id: str, name: str) -> str:
    return str(by_id(page, element_id).attrs.get(name, ""))


def forms_in_adapt(page: str) -> list[Any]:
    section = by_id(page, "adapt")
    return [e for e in elements(page) if e.tag == "form" and e.within(section)]


def drafted(d: AdaptDesk, fixture: str = "ok") -> str:
    d.ready_to_adapt()
    d.adapt(fixture)
    return str(d.version_id())


# --- security: what is sent ----------------------------------------------------------------------


def test_the_hint_says_the_whole_earlier_answer_is_sent_as_written(d: AdaptDesk):
    d.ready_to_adapt()

    hint = said_text(by_id(adapted_page(d), "adapt-hint").text)

    assert "whole earlier answer is sent to the AI as written" in hint
    assert "employer name" in hint


def test_an_unexpected_value_error_while_starting_is_not_a_bad_request(
    d: AdaptDesk, monkeypatch: pytest.MonkeyPatch
):
    d.ready_to_adapt()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("fictional failure inside the service")

    monkeypatch.setattr(d.service, "start_adapt", boom)
    try:
        response = d.post(d.path("/adapt"), {"version": ""})
    except ValueError:
        return  # raised to the server's error handling: not hidden as a bad request
    assert response.status_code != 400


# --- the swap: only the adapt section ------------------------------------------------------------


def test_every_form_of_the_adapt_section_swaps_only_the_section(d: AdaptDesk):
    drafted(d, "adversarial_old_employer")
    page = adapted_page(d)

    forms = forms_in_adapt(page)

    kinds = sorted(str(f.attrs.get("data-unit-form", f.attrs.get("id"))) for f in forms)
    assert kinds == sorted(
        ["adapt-approve-form", "adapt-form", "edit", "edit", "remove", "remove", "confirm"]
    )
    for form in forms:
        assert form.attrs["hx-target"] == "#adapt", form.attrs
        assert form.attrs["hx-select"] == "#adapt", form.attrs
        assert form.attrs["hx-swap"] == "outerHTML"
    practice = by_id(page, "practice-form")
    assert practice.attrs["hx-target"] == "#prep" and "hx-select" not in practice.attrs
    assert not practice.within(by_id(page, "adapt"))


def test_the_budget_question_and_its_confirmation_live_inside_the_adapt_section(d: AdaptDesk):
    d.ready_to_adapt()
    block_budget(d.r, uses=3)

    asked = d.post(d.path("/adapt"), {"version": ""})

    assert asked.status_code == 200
    dialog = by_id(asked.text, "adapt-budget-dialog")
    assert dialog.within(by_id(asked.text, "adapt"))
    confirm = [e for e in elements(asked.text) if e.tag == "form" and e.within(dialog)]
    assert len(confirm) == 1
    assert confirm[0].attrs["hx-target"] == "#adapt" and confirm[0].attrs["hx-select"] == "#adapt"


def test_a_budget_reason_stays_inside_the_adapt_section(d: AdaptDesk):
    d.ready_to_adapt()
    block_budget(d.r, uses=9)

    refused = d.post(d.path("/adapt"), {"version": ""})

    assert refused.status_code == 409
    assert by_id(refused.text, "adapt-reason").within(by_id(refused.text, "adapt"))


def test_the_reload_prompt_of_a_stale_adapt_action_is_inside_the_adapt_section(d: AdaptDesk):
    drafted(d)

    stale = d.post(d.approve_path("no-such-version"))

    assert stale.status_code == 409
    prompt = by_id(stale.text, "adapt-conflict")
    assert prompt.within(by_id(stale.text, "adapt"))
    assert not has_id(stale.text, "practice-conflict")


def test_the_adapt_section_polls_itself_while_a_task_runs(d: AdaptDesk):
    d.ready_to_adapt()
    d.script("ok")
    d.r.run.hold.hold = True
    try:
        first = d.service.start_adapt(d.aid, d.qid)
        d.r.run.wait_entered(1)

        section = tag(adapted_page(d), "adapt")

        assert 'hx-trigger="every 2s"' in section
        assert f'hx-get="{d.path()}?refresh=1"' in section
        assert 'hx-target="this"' in section and 'hx-select="#adapt"' in section
    finally:
        d.r.run.hold.open()
    d.r.run.wait_terminal(first.id)
    assert "hx-trigger" not in tag(adapted_page(d), "adapt"), "no poll once it has ended"


def test_an_invalid_edit_focuses_its_textarea_not_the_heading(d: AdaptDesk):
    version = drafted(d)
    unit = d.unit_with(version, P1)["id"]

    refused = d.post(d.unit_path(version, unit, "edit"), {"text": "  "})

    assert refused.status_code == 422
    assert "data-autofocus" in tag(refused.text, f"edit-{unit}")
    assert "data-autofocus" not in tag(refused.text, "adapt-heading")


# --- alerts live in the section ------------------------------------------------------------------


def test_a_refused_approval_alerts_inside_the_section_and_the_buttons_name_it(d: AdaptDesk):
    version = drafted(d, "adversarial_unknown_id")

    refused = d.post(d.approve_path(version))

    assert refused.status_code == 409
    alert = by_id(refused.text, "adapt-alert")
    assert alert.within(by_id(refused.text, "adapt")) and alert.attrs["role"] == "alert"
    assert "1 paragraph needs your check." in said_text(alert.text)
    assert "adapt-alert" in attr_of(refused.text, "adapt-approve", "aria-describedby").split()
    assert "adapt-alert" in attr_of(refused.text, "adapt-submit", "aria-describedby").split()


def test_a_refusal_for_missing_facts_alerts_inside_the_section(tmp_path):
    with adapt_app(tmp_path, facts=False) as d:
        d.practice(d.src, OLD)

        refused = d.post(d.path("/adapt"), {"version": ""})

        assert refused.status_code == 409
        alert = by_id(refused.text, "adapt-alert")
        assert alert.within(by_id(refused.text, "adapt")) and alert.attrs["role"] == "alert"


def test_without_a_section_a_refusal_still_alerts_on_the_page(d: AdaptDesk):
    d.practice(d.aid, OLD, ready=True)  # only this application's own try: no section

    refused = d.post(d.path("/adapt"), {"version": ""})

    assert refused.status_code == 409 and not has_id(refused.text, "adapt")
    assert by_id(refused.text, "adapt-alert").attrs["role"] == "alert"


# --- placement and labels ------------------------------------------------------------------------


def test_the_adapt_section_follows_the_practice_form_and_precedes_the_tries(d: AdaptDesk):
    d.ready_to_adapt()

    page = adapted_page(d)

    form = page.index('id="practice-form"')
    section = page.index('id="adapt"')
    tries = page.index('id="practice-attempts-heading"')
    assert form < section < tries


def test_the_earlier_and_the_adapted_answer_each_have_a_label(d: AdaptDesk):
    drafted(d)

    page = adapted_page(d)

    label = by_id(page, "adapt-earlier-label")
    assert said_text(label.text) == "Your earlier answer"
    assert page.index('id="adapt-earlier-label"') < page.index('id="adapt-earlier"')
    heading = by_id(page, "adapt-units-heading")
    assert said_text(heading.text) == "Adapted for this role"
    assert page.index('id="adapt-units-heading"') < page.index('id="adapt-units"')
    assert by_id(page, "adapt-units").attrs["aria-labelledby"] == "adapt-units-heading"


def test_the_earlier_answers_date_is_a_time_element_and_ready_is_explained(d: AdaptDesk):
    d.practice(d.src, OLD, ready=True)

    page = adapted_page(d)

    meta = by_id(page, "adapt-earlier-meta")
    assert "Marked ready when you practiced it." in said_text(meta.text)
    stamp = d.rows("SELECT created_at FROM practice_attempts")[0]["created_at"]
    assert re.search(rf'<time datetime="{re.escape(stamp)}">{stamp[:10]}</time>', page)


# --- wording of the states -----------------------------------------------------------------------


def test_a_flagged_answer_that_needs_a_recheck_says_what_to_do(d: AdaptDesk):
    d.ready_to_adapt()
    d.approved()
    d.withdraw("never_use", 1)

    page = adapted_page(d)

    assert said_text(by_id(page, "adapt-recheck").text) == (
        "A fact this answer cites changed. " + RECHECK
    )


def test_with_a_draft_the_button_reads_adapt_again_and_the_hint_says_what_happens(d: AdaptDesk):
    drafted(d)

    page = adapted_page(d)

    button = re.search(r'<button[^>]*id="adapt-submit"[^>]*>\s*([^<]*?)\s*</button>', page)
    assert button is not None and button.group(1) == "Adapt again"
    hint = said_text(by_id(page, "adapt-hint").text)
    assert "new draft" in hint and "not carried" in hint
    assert "edits" in hint


def test_without_a_draft_the_button_still_reads_adapt_to_this_role(d: AdaptDesk):
    d.ready_to_adapt()

    page = adapted_page(d)

    assert re.search(r'<button[^>]*id="adapt-submit"[^>]*>\s*Adapt to this role\s*</button>', page)
    assert "not carried" not in said_text(by_id(page, "adapt-hint").text)


def test_with_no_usable_fact_a_disabled_button_is_described_by_the_reason(tmp_path):
    with adapt_app(tmp_path, facts=False) as d:
        d.practice(d.src, OLD)

        page = adapted_page(d)

        # No form and no #adapt-submit exist here (pinned by test_prep_adapt.py); the button is
        # a plain, disabled one, so it can be found, read and cannot post.
        assert not has_id(page, "adapt-form") and not has_id(page, "adapt-submit")
        button = by_id(page, "adapt-unavailable")
        assert button.tag == "button" and button.attrs["type"] == "button"
        assert button.attrs["aria-disabled"] == "true"
        assert button.attrs["aria-describedby"] == "adapt-empty"
        assert said_text(button.text) == "Adapt to this role"
        assert not button.within(by_id(page, "adapt-empty"))
        assert "No confirmed facts to adapt it with yet." in said_text(
            by_id(page, "adapt-empty").text
        )


def test_a_stale_adapt_press_says_nothing_was_started(d: AdaptDesk):
    drafted(d)

    stale = d.post(d.path("/adapt"), {"version": "no-such-version"})

    assert stale.status_code == 409
    text = said_text(by_id(stale.text, "adapt-conflict").text)
    assert "Nothing was started." in text and "Your change was not saved" not in text
    assert len(d.requests()) == 1, "no second request"


def test_a_stale_edit_keeps_the_not_saved_wording_and_other_actions_do_not_claim_it(d: AdaptDesk):
    version = drafted(d)
    unit = d.unit_with(version, P1)["id"]

    edit = d.post(d.unit_path("no-such-version", unit, "edit"), {"text": "My typed words."})
    remove = d.post(d.unit_path("no-such-version", unit, "remove"))
    approve = d.post(d.approve_path("no-such-version"))

    assert "Your change was not saved." in said_text(by_id(edit.text, "adapt-conflict").text)
    assert "My typed words." in by_id(edit.text, "adapt-recovered").text
    for other in (remove, approve):
        text = said_text(by_id(other.text, "adapt-conflict").text)
        assert "Nothing was changed." in text and "Your change was not saved" not in text


def test_while_a_task_runs_editing_is_paused_and_approving_says_why(d: AdaptDesk):
    drafted(d)
    d.script("ok")
    d.r.run.hold.hold = True
    try:
        again = d.service.start_adapt(d.aid, d.qid, version=d.version_id())
        d.r.run.wait_entered(1)

        page = adapted_page(d)

        assert "Editing is paused" in said_text(by_id(page, "adapt-running").text)
        approve = by_id(page, "adapt-approve")
        assert approve.attrs["aria-disabled"] == "true"
        assert "adapt-running" in str(approve.attrs["aria-describedby"]).split()
        assert "data-unit-form" not in " ".join(
            " ".join(map(str, e.attrs)) for e in elements(page) if e.tag == "form"
        ), "no edit, remove or confirm form while it runs"
    finally:
        d.r.run.hold.open()
    d.r.run.wait_terminal(again.id)


def test_approving_a_blocked_answer_still_names_the_blockers_reason(d: AdaptDesk):
    version = drafted(d, "adversarial_unknown_id")

    page = adapted_page(d)

    assert "adapt-approve-reason" in attr_of(page, "adapt-approve", "aria-describedby").split()
    with pytest.raises(ApprovalBlocked):
        d.service.approve_adapted(d.aid, d.qid, version)
