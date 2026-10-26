"""M6.6 the workspace's CV step and the country template: where the draft is offered, how a template
is drafted once per country, approved and reused by every application in that country (AC-M6.5;
ADR-106 items 3 and 6).

Placement (a choice, recorded here for the implementer and in the report): the template unit
``#cv-template`` is shown on step 3 (CV) of the Tailoring tab, which is the step that needs an
approved template; the step keeps its "later step" text about the CV itself. Steps 1, 2, 4 and 5
show no unit. The unit is for the application's CV country (the workspace's, else the preference,
see ``tests/integration/test_application_country.py``) and swaps only itself.

Assumed API, routes and markup: the docstring of ``tests/template_pages.py``. Everything is
fictional; real SQLite in a temp folder, the fake provider behind the spy. No network.
"""

from typing import Any

import pytest

from app.domain.cv_templates import TemplateSpec
from app.domain.errors import InvalidModelOutput, message_for_kind
from tests.board_pages import elements
from tests.integration.test_board import snapshot
from tests.tailoring_pages import fragment_url, hidden_value
from tests.template_pages import (
    APPROVED,
    BASE,
    DRAFT_TEMPLATE,
    STARTED,
    Templates,
    announced,
    by_id,
    controls_in,
    fixture_data,
    form_fields,
    has_id,
    review_values,
    said_text,
    templates_app,
    unit,
    unit_state,
)

pytestmark = pytest.mark.integration

OK = fixture_data("ok")


@pytest.fixture
def tw(tmp_path):
    with templates_app(tmp_path) as desk:
        yield desk


def without_version(values: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in values.items() if name != "version"}


def options(page: str, form_id: str, name: str) -> list[str]:
    select = controls_in(page, by_id(page, form_id))[name]
    return [
        str(e.attrs.get("value", ""))
        for e in elements(page)
        if e.tag == "option" and e.within(select)
    ]


def selected(page: str, form_id: str, name: str) -> str:
    select = controls_in(page, by_id(page, form_id))[name]
    chosen = [e for e in elements(page) if e.tag == "option" and e.within(select)]
    picked = [o for o in chosen if "selected" in o.attrs] or chosen[:1]
    return str(picked[0].attrs.get("value", ""))


def set_country(tw: Templates, aid: str, code: str) -> None:
    shown = tw.page(aid, 3).text
    version = hidden_value(shown, by_id(shown, "country-form"), "version")
    answered = tw.post_fields(
        f"/applications/{aid}/tailoring/country", {"version": version, "cv_country": code}
    )
    assert answered.status_code == 200, (answered.status_code, answered.text[:300])


def test_ac_m6_5_template_drafted_once_approved_and_reused(tw: Templates) -> None:
    first = tw.ready("job-pt-first")
    second = tw.ready("job-pt-second")  # the finished setup's CV country is PT for both

    # Neither application has a template: each offers the draft, and reading starts nothing.
    for app in (first, second):
        shown = tw.page(app.id, 3).text
        assert unit_state(shown) == "none" and unit(shown).attrs["data-country"] == "PT"
        assert by_id(shown, "template-draft-start").text == "Draft a template for Portugal"
    assert tw.spy.requests == [] and tw.template_tasks() == []

    # One draft for the country (from the first application's panel), reviewed and approved.
    tw.script(DRAFT_TEMPLATE, "ok")
    started = tw.start("PT")
    assert started.status_code == 200 and STARTED in announced(started)
    tw.ended(DRAFT_TEMPLATE, "PT")
    assert unit_state(tw.fragment(first.id, 3).text) == "draft"
    assert tw.approved_spec("PT") is None, "a draft is not reused"
    approved = tw.approve("PT")
    assert approved.status_code == 200 and APPROVED in announced(approved)

    # Both applications, the one that asked and the other, now use it without another request.
    requests, tasks = len(tw.requests(DRAFT_TEMPLATE)), len(tw.template_tasks())
    for app in (first, second):
        for shown in (tw.page(app.id, 3).text, tw.fragment(app.id, 3).text):
            assert unit_state(shown) == "approved" and unit(shown).attrs["data-country"] == "PT"
            assert by_id(shown, "template-draft-start").text == "Draft again"
            assert not any(
                e.tag == "button" and e.text == "Draft a template for Portugal"
                for e in elements(shown)
            )
            assert without_version(review_values(shown)) == form_fields(OK["template"])
    assert len(tw.requests(DRAFT_TEMPLATE)) == requests == 1
    assert len(tw.template_tasks()) == tasks == 1
    assert len(tw.rows("SELECT * FROM usage_records WHERE task_type=?", (DRAFT_TEMPLATE,))) == 1
    assert tw.approved_spec("PT") == TemplateSpec.model_validate(OK["template"])
    assert [row["country"] for row in tw.template_rows()] == ["PT"]


def test_workspace_cv_step_offers_a_template_draft_when_none_is_approved(tw: Templates) -> None:
    app = tw.ready()
    before = snapshot(tw.r)

    for shown in (tw.page(app.id, 3), tw.fragment(app.id, 3)):
        assert shown.status_code == 200
        page = shown.text
        assert unit_state(page) == "none"
        assert by_id(page, "cv-template-heading").text == "CV template: Portugal"
        form = by_id(page, "template-draft-form")
        assert form.attrs.get("action") == f"{BASE}/PT/draft" and form.attrs["method"] == "post"
        button = by_id(page, "template-draft-start")
        assert button.tag == "button" and button.text == "Draft a template for Portugal"
        assert options(page, "template-draft-form", "pages") == ["1", "2", "3"]
        assert options(page, "template-draft-form", "photo") == ["none", "optional", "expected"]
        assert selected(page, "template-draft-form", "pages") == "2"
        assert selected(page, "template-draft-form", "photo") == "optional"
        assert controls_in(page, form)["note"].tag == "textarea"
        assert not has_id(page, "template-form"), "nothing to review before a draft"
        # The CV itself still comes later; the unit is the only new thing on the step.
        assert "later step" in said_text(page).lower()
    # The other steps carry no unit; reading wrote nothing and asked nothing.
    for step in (1, 2, 4, 5):
        assert not has_id(tw.page(app.id, step).text, "cv-template"), step
        assert not has_id(tw.fragment(app.id, step).text, "cv-template"), step
    assert snapshot(tw.r) == before and tw.spy.requests == []


def test_workspace_template_requests_need_token_origin_and_version(tw: Templates) -> None:
    app = tw.ready()
    run = tw.r.run.run

    assert tw.r.client.get(fragment_url(app.id, 3)).status_code == 403  # no token
    path = f"/applications/{app.id}/tailoring/country"
    body = {"version": "0", "cv_country": "DE"}
    no_origin = tw.r.client.post(path, data=body, headers=run.with_token(**{"HX-Request": "true"}))
    assert no_origin.status_code == 403
    no_token = tw.r.client.post(path, data=body, headers={"origin": run.action()["origin"]})
    assert no_token.status_code == 403
    assert tw.post_fields(path, {"cv_country": "DE"}).status_code == 400  # no version
    assert tw.rows("SELECT * FROM workspaces") == []

    set_country(tw, app.id, "DE")
    shown = tw.page(app.id, 3).text
    assert unit(shown).attrs["data-country"] == "DE"
    assert by_id(shown, "template-draft-start").text == "Draft a template for Germany"


def test_workspace_template_follows_the_application_country(tw: Templates) -> None:
    first = tw.ready("job-country-first")
    second = tw.ready("job-country-second")
    tw.approved_template("PT", "ok")

    set_country(tw, first.id, "DE")

    # The first application needs a German template; the second still has the approved PT one.
    german = tw.page(first.id, 3).text
    assert unit(german).attrs["data-country"] == "DE" and unit_state(german) == "none"
    assert by_id(german, "template-draft-start").text == "Draft a template for Germany"
    assert unit_state(tw.page(second.id, 3).text) == "approved"
    assert tw.approved_spec("DE") is None and tw.approved_spec("PT") is not None
    assert tw.template_tasks()[0]["dedupe_key"] == "draft_cv_template:PT"

    tw.drafted("DE", "photo_expected", pages="2", photo="expected")
    request = tw.requests(DRAFT_TEMPLATE)[-1]
    assert '"DE"' in request.user and "Germany" in request.user
    assert unit_state(tw.page(first.id, 3).text) == "draft"
    assert tw.approve("DE").status_code == 200

    assert unit_state(tw.page(first.id, 3).text) == "approved"
    assert tw.stored_template_json("DE")["page_size"] == "Letter"
    assert [row["country"] for row in tw.template_rows()] == ["DE", "PT"]
    assert len(tw.requests(DRAFT_TEMPLATE)) == 2, "one request per country"
    # Back to Portugal: the first template is untouched and approved.
    set_country(tw, first.id, "PT")
    assert unit_state(tw.page(first.id, 3).text) == "approved"
    assert tw.stored_template_json("PT") == OK["template"]


def test_workspace_applications_in_one_country_share_the_running_draft(tw: Templates) -> None:
    first = tw.ready("job-share-first")
    second = tw.ready("job-share-second")

    tw.r.run.hold.hold = True
    assert tw.start("PT").status_code == 200
    tw.r.run.wait_entered(1)

    for app in (first, second):
        shown = tw.page(app.id, 3).text
        assert unit_state(shown) == "running"
        assert any(e.attrs.get("role") == "status" for e in elements(shown))
    assert tw.start("PT").status_code == 200  # the second application's press: same task
    assert len(tw.template_tasks()) == 1
    tw.r.run.hold.open()
    tw.r.run.wait_terminal(tw.template_tasks()[0]["id"])
    assert len(tw.requests(DRAFT_TEMPLATE)) == 1
    assert unit_state(tw.page(second.id, 3).text) == "draft"


def test_workspace_failed_draft_shows_the_reason_and_offers_a_retry(tw: Templates) -> None:
    app = tw.ready()

    task = tw.drafted("PT", "invalid_value", status="failed")

    assert task.error == InvalidModelOutput.kind
    shown: Any = tw.page(app.id, 3).text
    assert unit_state(shown) == "failed"
    alerts = [e.text for e in elements(shown) if e.attrs.get("role") == "alert"]
    assert alerts == [message_for_kind(task.error)]
    assert by_id(shown, "template-draft-start").text == "Draft a template for Portugal"
    retry = tw.drafted("PT", "ok")
    assert str(retry.status.value) == "succeeded"
    assert unit_state(tw.page(app.id, 3).text) == "draft"
