"""M6.5 review fixes for the workspace's Tailoring tab: refused and stale forms keep what was typed
and tie the error to the field, the fact entry points, what the page shows for cited facts, the
accessible names of repeated controls, completion announcements and the read-only GET.

Everything is fictional; real SQLite in a temp folder, the fake provider behind the spy.
"""

import json
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.services.job_fit import PRACTICE_NOTICE
from tests.board_pages import elements
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import EARLIER_ANGLE_ID, FACT_1, REQUIREMENTS
from tests.tailoring_pages import (
    GAP_CHECK,
    PROPOSE_ANGLES,
    Tailoring,
    angle_items,
    announced,
    by_id,
    form_values,
    forms_in,
    fragment_url,
    has_id,
    inside,
    page_url,
    requirement_items,
    said_text,
    seed_summary,
    tailoring_app,
)

pytestmark = pytest.mark.integration

NEW_FACT = "Led a fictional Terraform migration for 2 teams."
EARLIER_NAME = "Reliable planner"


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


def focus_targets(page: str) -> list[Any]:
    return [e for e in elements(page) if "data-autofocus" in e.attrs]


def described_by(page: str, field: Any) -> str:
    """The text of the elements a field's ``aria-describedby`` names."""
    ids = str(field.attrs.get("aria-describedby") or "").split()
    return " ".join(
        e.text for e in elements(page) if e.attrs.get("id") in ids and e.attrs.get("id")
    )


def own_values(tw: Tailoring, aid: str, **changes: str) -> tuple[str, dict[str, str]]:
    shown = tw.page(aid, 2).text
    return shown, {**form_values(shown, by_id(shown, "angle-form")), **changes}


ANGLE_PATH = "/applications/{}/tailoring/2/angle"


# --- refused and stale angle forms -----------------------------------------------------------


def test_refused_own_angle_keeps_typed_text_and_ties_the_error_to_the_name(tw: Tailoring):
    app = tw.ready()
    _, values = own_values(tw, app.id, name="   ", description="Typed words kept.")

    refused = tw.post_fields(ANGLE_PATH.format(app.id), values)

    assert refused.status_code == 422
    form = by_id(refused.text, "angle-form")
    assert form_values(refused.text, form)["description"] == "Typed words kept."
    (details,) = [e for e in elements(refused.text) if e.tag == "details" and form.within(e)]
    assert "open" in details.attrs  # the form stays open
    (name,) = inside(refused.text, form, lambda e: e.attrs.get("name") == "name")
    assert name.attrs["aria-invalid"] == "true" and "data-autofocus" in name.attrs
    assert described_by(refused.text, name) == "Give the angle a name."
    assert announced(refused) == ["Give the angle a name."]
    assert focus_targets(refused.text) == [name]  # the heading does not compete


def test_refused_long_description_is_tied_to_the_description_field(tw: Tailoring):
    app = tw.ready()
    _, values = own_values(tw, app.id, name="Fine name", description="x" * 2001)

    refused = tw.post_fields(ANGLE_PATH.format(app.id), values)

    assert refused.status_code == 422
    form = by_id(refused.text, "angle-form")
    (area,) = inside(refused.text, form, lambda e: e.attrs.get("name") == "description")
    assert area.attrs["aria-invalid"] == "true" and "data-autofocus" in area.attrs
    assert "2000" in described_by(refused.text, area)
    (name,) = inside(refused.text, form, lambda e: e.attrs.get("name") == "name")
    assert "aria-invalid" not in name.attrs
    assert form_values(refused.text, form)["name"] == "Fine name"


def test_refused_proposal_card_keeps_the_card_form_and_leaves_own_form_closed(tw: Tailoring):
    app = tw.ready()
    tw.propose(app.id, "ok")
    shown = tw.page(app.id, 2).text
    card = angle_items(shown)[1]
    (form,) = forms_in(shown, card)
    values = {**form_values(shown, form), "name": "", "description": "My own edit."}

    refused = tw.post_fields(str(form.attrs["action"]), values)

    assert refused.status_code == 422
    second = angle_items(refused.text)[1]
    (shown_form,) = forms_in(refused.text, second)
    assert form_values(refused.text, shown_form)["description"] == "My own edit."
    (name,) = inside(refused.text, shown_form, lambda e: e.attrs.get("name") == "name")
    assert name.attrs["aria-invalid"] == "true" and "data-autofocus" in name.attrs
    assert "Give the angle a name." in described_by(refused.text, name)
    (own,) = [e for e in elements(refused.text) if e.tag == "details"]
    assert "open" not in own.attrs
    assert announced(refused) == ["Give the angle a name."]


def test_stale_angle_form_uses_the_reload_prompt_inside_the_form_and_keeps_text(tw: Tailoring):
    app = tw.ready()
    path = ANGLE_PATH.format(app.id)
    _, values = own_values(tw, app.id, name="First", description="One.")
    assert tw.post_fields(path, values).status_code == 200

    stale = tw.post_fields(path, {**values, "name": "Second", "description": "Typed after."})

    assert stale.status_code == 409
    form = by_id(stale.text, "angle-form")
    prompts = inside(stale.text, form, lambda e: "reload-prompt" in e.classes)
    assert len(prompts) == 1 and prompts[0].attrs["role"] == "alert"
    assert VersionConflict.message in said_text(stale.text)
    assert any(e.tag == "a" and e.within(prompts[0]) for e in elements(stale.text))
    values_now = form_values(stale.text, form)
    assert (values_now["name"], values_now["description"]) == ("Second", "Typed after.")
    assert focus_targets(stale.text) == prompts  # one focus target: the prompt
    assert [row["name"] for row in tw.rows("SELECT name FROM angles")] == ["First"]


# --- saved angles compare line endings as the server stores them ----------------------------


def test_crlf_description_still_counts_as_unchanged_for_a_reused_angle(tw: Tailoring):
    app = tw.ready()
    tw.earlier_angle(
        EARLIER_ANGLE_ID,
        "job-earlier-1",
        name=EARLIER_NAME,
        description="Line one.\nLine two.",
        updated_at="2026-10-20T09:00:00+00:00",
    )
    tw.propose(app.id, "ok")
    version = tw.angle_version(app.id)

    saved = tw.post_fields(
        ANGLE_PATH.format(app.id),
        {
            "name": EARLIER_NAME,
            "description": "Line one.\r\nLine two.",
            "version": version,
            "reuse_angle_id": EARLIER_ANGLE_ID,
        },
    )

    assert saved.status_code == 200
    assert [row["id"] for row in tw.rows("SELECT id FROM angles")] == [EARLIER_ANGLE_ID]
    (workspace,) = tw.rows("SELECT angle_id FROM workspaces WHERE application_id=?", (app.id,))
    assert workspace["angle_id"] == EARLIER_ANGLE_ID


def test_crlf_description_of_a_proposal_saved_unchanged_is_origin_ai(tw: Tailoring):
    app = tw.ready()
    angle = {
        "name": "Calm closer",
        "description": "First line.\nSecond line.",
        "rationale": "Fits.",
        "fact_ids": ["F1@1"],
        "reuse_angle_id": None,
        "claims": [],
    }
    tw.r.run.execute(
        "INSERT INTO angle_proposals (id, application_id, result, provider, created_at) "
        "VALUES ('proposal-multiline', ?, ?, 'anthropic', '2026-10-21T09:00:00+00:00')",
        (app.id, json.dumps({"angles": [angle]})),
    )
    result = {"angles": [angle, angle]}
    version = tw.angle_version(app.id)

    saved = tw.post_fields(
        ANGLE_PATH.format(app.id),
        {
            "name": result["angles"][1]["name"],
            "description": "First line.\r\nSecond line.",
            "version": version,
        },
    )

    assert saved.status_code == 200
    (angle,) = tw.rows("SELECT name, description, origin FROM angles")
    assert angle["origin"] == "ai" and angle["description"] == "First line.\nSecond line."


# --- the fact typed at a gap ----------------------------------------------------------------


def fact_path(aid: str) -> str:
    return f"/applications/{aid}/tailoring/1/facts"


def test_refused_fact_focuses_the_textarea_and_announces_the_error(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")

    refused = tw.post_fields(fact_path(app.id), {"text": "  ", "requirement_id": "req-2"})

    assert refused.status_code == 422
    item = requirement_items(refused.text)["req-2"]
    (area,) = inside(refused.text, item, lambda e: e.tag == "textarea")
    assert "data-autofocus" in area.attrs and area.attrs["aria-invalid"] == "true"
    assert "Write the fact first." in described_by(refused.text, area)
    assert announced(refused) == ["Write the fact first."]
    assert focus_targets(refused.text) == [area]


def test_fact_over_ten_thousand_characters_is_refused_with_a_field_message(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")
    count = len(tw.rows("SELECT id FROM facts"))

    refused = tw.post_fields(fact_path(app.id), {"text": "x" * 10_001, "requirement_id": "req-2"})

    assert refused.status_code == 422
    item = requirement_items(refused.text)["req-2"]
    (area,) = inside(refused.text, item, lambda e: e.tag == "textarea")
    assert "10,000" in described_by(refused.text, area)
    assert len(tw.rows("SELECT id FROM facts")) == count
    ok = tw.post_fields(fact_path(app.id), {"text": "x" * 10_000, "requirement_id": "req-2"})
    assert ok.status_code == 200 and len(tw.rows("SELECT id FROM facts")) == count + 1


def test_fact_added_says_what_to_do_next_naming_the_requirement(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")

    added = tw.post_fields(fact_path(app.id), {"text": NEW_FACT, "requirement_id": "req-2"})

    assert added.status_code == 200 and announced(added) == ["Fact added."]
    line = by_id(added.text, "fact-added")
    assert "Fact added. Check again to see if it covers this requirement." in line.text
    assert REQUIREMENTS[1].text in line.text
    assert not has_id(tw.page(app.id, 1).text, "fact-added")  # only the answer says it


def test_fact_posted_twice_is_stored_once_and_answered_as_added(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")
    values = {"text": NEW_FACT, "requirement_id": "req-2"}

    first = tw.post_fields(fact_path(app.id), values)
    second = tw.post_fields(fact_path(app.id), {**values, "text": f" {NEW_FACT}\r\n"})

    assert first.status_code == second.status_code == 200
    assert announced(second) == ["Fact added."]
    assert has_id(second.text, "fact-added")
    found = tw.rows(
        "SELECT f.id FROM facts f JOIN fact_versions v ON v.id=f.current_version_id WHERE v.text=?",
        (NEW_FACT,),
    )
    assert len(found) == 1


# --- steps that cannot start ----------------------------------------------------------------


@pytest.mark.parametrize("step", [1, 2])
def test_summary_without_requirements_says_to_review_it_and_starts_nothing(
    tw: Tailoring, step: int
):
    app = tw.ready(summary=False)
    seed_summary(tw.db, "tailoring-job", ())

    for shown in (tw.page(app.id, step).text, tw.fragment(app.id, step).text):
        assert "no requirements" in said_text(shown)
        region = by_id(shown, "tailoring")
        links = inside(shown, region, lambda e: e.tag == "a" and e.text == "Open job")
        assert [e.attrs["href"] for e in links] == ["/jobs/tailoring-job"]
        assert not has_id(shown, "gap-check-start" if step == 1 else "angle-propose")
    refused = tw.post(app.id, step, "check" if step == 1 else "propose")
    assert refused.status_code == 409
    assert tw.rows("SELECT id FROM tasks WHERE type IN ('gap_check', 'propose_angles')") == []
    assert tw.spy.requests == []


@pytest.mark.parametrize(
    ("step", "action"),
    [(1, "check"), (2, "propose")],
)
def test_start_that_returns_no_task_says_already_running(tw: Tailoring, step: int, action: str):
    app = tw.ready()
    service = tw.r.run.app.state.tailoring
    service.start_gap_check = lambda *args, **kwargs: None
    service.start_angles = lambda *args, **kwargs: None

    answered = tw.post(app.id, step, action)

    assert answered.status_code == 409
    assert "Already running." in said_text(answered.text)
    assert announced(answered) == ["Already running."]


# --- what the steps show -----------------------------------------------------------------------


def test_gap_rows_offer_check_again_and_unique_names_for_repeated_controls(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")
    shown = tw.page(app.id, 1).text
    page_form = by_id(shown, "gap-check-form")
    assert str(page_form.attrs["action"]).endswith(f"/applications/{app.id}/tailoring/1/check")
    items = requirement_items(shown)

    for rid in ("req-1", "req-2", "req-4"):
        item = items[rid]
        text = next(r.text for r in REQUIREMENTS if r.id == rid)
        again = inside(shown, item, lambda e: e.tag == "button" and e.text == "Check again")
        assert len(again) == 1
        assert again[0].attrs["form"] == "gap-check-form"
        assert again[0].attrs["aria-label"] == f"Check again for: {text}"
        (summary,) = inside(shown, item, lambda e: e.tag == "summary")
        assert described_by(shown, summary) == text
        (save,) = inside(shown, item, lambda e: e.tag == "button" and e.text == "Save fact")
        assert described_by(shown, save) == text
        assert len(forms_in(shown, item)) == 1  # the one fact form
    assert not inside(shown, items["req-3"], lambda e: e.text == "Check again")  # met: no gap


def test_angle_cards_show_cited_wording_and_mark_the_text_as_suggested(tw: Tailoring):
    app = tw.ready()
    tw.propose(app.id, "ok")
    shown = tw.page(app.id, 2).text

    first, second = angle_items(shown)
    assert "Suggested, not yet your angle" in first.text
    assert "Suggested, not yet your angle" in second.text
    links = inside(
        shown, first, lambda e: e.tag == "a" and e.attrs.get("href", "").startswith("/facts/")
    )
    assert [e.text for e in links] == [FACT_1]
    (button,) = inside(shown, first, lambda e: e.tag == "button")
    assert button.attrs["aria-label"] == "Use this angle: Reliable planner"
    assert button.text == "Use this angle"


def test_framing_ideas_show_the_wording_of_the_facts_they_cite(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "ok")
    shown = tw.page(app.id, 1).text

    (idea,) = [e for e in elements(shown) if e.tag == "li" and "data-framing" in e.attrs]
    links = inside(
        shown, idea, lambda e: e.tag == "a" and e.attrs.get("href", "").startswith("/facts/")
    )
    assert [e.text for e in links] == [FACT_1]


def test_practice_label_is_a_badge_on_both_steps(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, None)
    tw.propose(app.id, None)

    for step in (1, 2):
        shown = tw.page(app.id, step).text
        badges = [e for e in elements(shown) if "badge-practice" in e.classes]
        assert [(e.tag, e.text) for e in badges] == [("p", PRACTICE_NOTICE)]
        assert "badge" in badges[0].classes


# --- running and finished ----------------------------------------------------------------------


def poll(tw: Tailoring, aid: str, step: int) -> Any:
    headers = tw.r.run.run.htmx(**{"HX-Trigger": "tailoring"})
    return tw.r.client.get(fragment_url(aid, step), headers=headers)


def test_running_status_is_one_preserved_node_and_completion_is_announced_once(tw: Tailoring):
    app = tw.ready()
    tw.r.run.hold.hold = True
    tw.post(app.id, 1, "check")
    tw.r.run.wait_entered(1)

    running = tw.fragment(app.id, 1)
    (status,) = [e for e in elements(running.text) if e.attrs.get("role") == "status"]
    assert status.attrs["id"] == "tailoring-running-1" and "hx-preserve" in status.attrs
    assert announced(poll(tw, app.id, 1)) == []  # still running: nothing to say
    tw.r.run.hold.open()
    tw.ended(GAP_CHECK, app.id)

    finished = poll(tw, app.id, 1)
    warning = by_id(finished.text, "gap-check-message").text  # the practice answer shows nothing
    assert announced(finished) == [f"Gap check finished. {warning}"]
    assert not has_id(finished.text, "tailoring-running-1")
    assert announced(tw.fragment(app.id, 1)) == []  # a plain reading says nothing
    assert announced(tw.r.client.get(page_url(app.id, 1))) == []


def test_finished_gap_check_announces_the_must_have_warning_without_moving_focus(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")

    finished = poll(tw, app.id, 1)

    message = by_id(finished.text, "gap-check-message").text
    assert "may not be worth applying for" in message
    assert announced(finished) == [f"Gap check finished. {message}"]
    assert focus_targets(finished.text) == []  # the user may be anywhere by now


def test_finished_angle_proposals_are_announced(tw: Tailoring):
    app = tw.ready()
    tw.propose(app.id, "ok")

    assert announced(poll(tw, app.id, 2)) == ["Angle proposals ready."]


def test_failed_task_is_not_announced_as_finished(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "malformed", status="failed")

    assert announced(poll(tw, app.id, 1)) == []


# --- reads never touch usage ---------------------------------------------------------------


def test_tailoring_get_does_not_use_the_start_check_and_changes_nothing(tw: Tailoring):
    app = tw.ready()
    gate = tw.r.run.app.state.budget_gate

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a read must not run the start check")

    gate.check = refuse
    before = snapshot(tw.r)

    for step in (1, 2):
        assert tw.page(app.id, step).status_code == 200
        assert tw.fragment(app.id, step).status_code == 200
    assert snapshot(tw.r) == before
    assert PROPOSE_ANGLES and tw.spy.requests == []
