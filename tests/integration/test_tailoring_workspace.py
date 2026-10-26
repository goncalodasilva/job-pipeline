"""M6.5 the workspace's Tailoring tab: page and fragment boundaries, the stepper, starting a step
through the budget gate, atomic results, the fake label and personal details (AC-M6.8, AC-M6.1;
spec 2.9, 3.6; ADR-106 item 11; detailed design "Application workspace").

Assumed interface: see ``tests/tailoring_pages.py`` for the module, task, route and markup names.
In short: ``GET /applications/{id}?tab=tailoring&step=N`` is a token-free read-only page (the
Timeline and contacts tab stays the default), ``GET /applications/{id}/tailoring/{step}`` is the
``#tailoring`` fragment (token), ``POST .../tailoring/1/check|1/facts|2/propose|2/angle`` need
token and origin (``angle`` also ``version``), ``app.state.tailoring`` offers ``start_gap_check``
and ``start_angles``, and the task types ``gap_check`` and ``propose_angles`` have the dedupe keys
``gap_check:<application id>`` and ``propose_angles:<application id>``. Steps 3 to 5 are
placeholders that say, in words, that they come in a later step.

Real SQLite in a temp folder, the fake provider behind the spy, no network, no fixed waits.
"""

import re
from typing import Any

import pytest

from app.domain.budgets import Budget
from app.domain.errors import ApplicationNotFound, VersionConflict
from app.domain.settings import PERSONAL_DETAILS
from app.services.job_fit import PRACTICE_NOTICE
from app.services.settings import SettingsService
from tests.board_pages import elements
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import DETAILS, personal_values
from tests.tailoring_pages import (
    GAP_CHECK,
    PROPOSE_ANGLES,
    TITLES,
    Tailoring,
    action_url,
    angle_items,
    announced,
    by_id,
    form_values,
    forms_in,
    fragment_url,
    has_id,
    open_step,
    page_url,
    said_text,
    stepper,
    tailoring_app,
    verdicts,
)

pytestmark = pytest.mark.integration

NOT_IN_FACT_BANK = "This isn't in your fact bank."
STEP_ROUTES = (
    (GAP_CHECK, 1, "check", {}),
    (GAP_CHECK, 1, "facts", {"text": "A fictional fact typed at a gap."}),
    (PROPOSE_ANGLES, 2, "propose", {}),
    (PROPOSE_ANGLES, 2, "angle", {"name": "Steady hands", "description": "", "version": "0"}),
)


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


def states(page: str) -> dict[int, str]:
    return {step: item.text for step, item in stepper(page).items()}


def tab_links(page: str) -> dict[str, dict[str, Any]]:
    nav = [
        e
        for e in elements(page)
        if e.tag == "nav" and e.attrs.get("aria-label") == "Application sections"
    ]
    assert len(nav) == 1, "one nav named 'Application sections'"
    return {e.text: dict(e.attrs) for e in elements(page) if e.tag == "a" and e.within(nav[0])}


# --- the page ---------------------------------------------------------------------------------


def test_tailoring_page_read_only_resume_step_derived(tw: Tailoring):
    app = tw.ready()
    before = snapshot(tw.r)

    default = tw.r.client.get(f"/applications/{app.id}")
    assert default.status_code == 200 and "application-timeline" in default.text
    assert not has_id(default.text, "tailoring")
    links = tab_links(default.text)
    assert list(links) == ["Timeline and contacts", "Tailoring"]  # no Interview prep placeholder
    assert links["Timeline and contacts"]["aria-current"] == "page"
    assert links["Tailoring"]["href"] == f"/applications/{app.id}?tab=tailoring"
    assert "aria-current" not in links["Tailoring"]
    assert tw.page(app.id).status_code == 200  # no tab asked: still the timeline
    other = tw.r.client.get(f"/applications/{app.id}?tab=unknown")
    assert other.status_code == 200 and "application-timeline" in other.text

    shown = tw.r.client.get(page_url(app.id))
    assert shown.status_code == 200 and "<html" in shown.text  # a page, no token needed
    assert "application-timeline" not in shown.text
    assert tab_links(shown.text)["Tailoring"]["aria-current"] == "page"
    assert tw.r.client.head(page_url(app.id)).status_code == 200
    assert open_step(shown.text) == 1  # resume: nothing is done yet
    words = states(shown.text)
    assert "Not started" in words[1] and "Not started" in words[2]
    assert "Not started" in words[3]
    assert all("Coming later" in words[step] for step in (4, 5))
    assert all(TITLES[step] in words[step] for step in TITLES)

    # Any step opens, and exactly the open step's link is the current one.
    for step in range(1, 6):
        page = tw.page(app.id, step)
        assert page.status_code == 200 and open_step(page.text) == step
        current = [
            e for e in elements(page.text) if e.tag == "a" and e.attrs.get("aria-current") == "step"
        ]
        assert [e.attrs["href"] for e in current] == [page_url(app.id, step)]
        assert by_id(page.text, "tailoring").tag == "section"
    # Reading changed nothing and asked nothing of the AI.
    plain = tw.r.client.get(page_url(app.id, 3), headers={"HX-Request": "true"})
    assert plain.status_code == 403  # a fragment-style page request needs the token
    assert snapshot(tw.r) == before and tw.spy.requests == []

    # The resume step follows the work: 1, then 2 once a gap check is stored, 2 when all is done.
    tw.check(app.id, "ok")
    assert open_step(tw.page(app.id).text) == 2
    assert "Done" in states(tw.page(app.id).text)[1]
    tw.propose(app.id, "ok")
    page = tw.page(app.id).text
    assert open_step(page) == 2 and "Needs your choice" in states(page)[2]
    form = forms_in(page, angle_items(page)[0])[0]
    assert tw.post_fields(str(form.attrs["action"]), form_values(page, form)).status_code == 200
    page = tw.page(app.id).text
    assert open_step(page) == 2 and "Done" in states(page)[2]
    assert open_step(tw.page(app.id, 1).text) == 1  # an explicit step wins


def test_tailoring_step_shows_running_while_the_task_runs(tw: Tailoring):
    app = tw.ready()
    tw.r.run.hold.hold = True

    started = tw.post(app.id, 1, "check")
    tw.r.run.wait_entered(1)

    assert started.status_code == 200 and announced(started) == ["Gap check started."]
    for shown in (tw.page(app.id, 1).text, tw.fragment(app.id, 1).text):
        assert "Running" in states(shown)[1]
        assert [e for e in elements(shown) if e.attrs.get("role") == "status"]
        assert (
            not has_id(shown, "gap-check-start")
            or "aria-disabled" in by_id(shown, "gap-check-start").attrs
        )
        region = by_id(shown, "tailoring")
        assert region.attrs["hx-get"] == fragment_url(app.id, 1)
        assert "every 2s" in str(region.attrs["hx-trigger"])
    tw.r.run.hold.open()
    tw.ended(GAP_CHECK, app.id)
    assert "Done" in states(tw.fragment(app.id, 1).text)[1]


@pytest.mark.parametrize("step", [4, 5])
def test_steps_three_to_five_are_placeholders_that_say_so(tw: Tailoring, step: int):
    app = tw.ready()

    for shown in (tw.page(app.id, step).text, tw.fragment(app.id, step).text):
        assert open_step(shown) == step
        assert f"Step {step}: {TITLES[step]}" in said_text(shown)
        assert re.search(r"later step", said_text(shown), re.IGNORECASE)
        assert "Coming later" in states(shown)[step]
        body = by_id(shown, "tailoring")
        # Nothing to start or save yet. M6.6 (lead-approved, narrow): the CV country form is on
        # every step, and step 3 also holds the #cv-template panel; nothing else may be there.
        allowed = [
            e
            for e in elements(shown)
            if e.attrs.get("id") == "country-form"
            or (step == 3 and e.attrs.get("id") == "cv-template")
        ]
        stray = [
            e
            for e in elements(shown)
            if e.tag in ("form", "button")
            and e.within(body)
            and not any(e is a or e.within(a) for a in allowed)
        ]
        assert not stray, [(e.tag, e.attrs.get("id")) for e in stray]
    assert tw.post(app.id, step, "check").status_code in (404, 405)


def test_open_workspace_links_to_the_tailoring_tab(tw: Tailoring):
    app = tw.ready()

    drawer = tw.r.client.get(f"/applications/{app.id}/detail", headers=tw.r.run.run.htmx())

    assert drawer.status_code == 200
    links = [e for e in elements(drawer.text) if e.tag == "a" and e.text == "Open workspace"]
    assert [e.attrs["href"] for e in links] == [f"/applications/{app.id}?tab=tailoring"]


# --- boundaries -------------------------------------------------------------------------------


def test_tailoring_fragments_need_token_posts_need_origin_and_version(tw: Tailoring):
    app = tw.ready()
    anonymous = tw.r.client
    htmx = tw.r.run.run.htmx()

    assert anonymous.get(fragment_url(app.id, 1)).status_code == 403
    for step in (1, 2, 3, 4, 5):
        shown = tw.fragment(app.id, step)
        assert shown.status_code == 200 and "<html" not in shown.text
        assert open_step(shown.text) == step
    for step in ("0", "6", "x"):
        assert tw.fragment(app.id, step).status_code == 404
    for aid in ("missing", "bad.id", "<script>"):
        for answer in (
            anonymous.get(fragment_url(aid, 1), headers=htmx),
            anonymous.get(f"/applications/{aid}?tab=tailoring"),
        ):
            assert answer.status_code == 404 and answer.text == ApplicationNotFound.message

    before = snapshot(tw.r)
    for _, step, action, values in STEP_ROUTES:
        path = action_url(app.id, step, action)
        assert anonymous.post(path, data=values).status_code == 403  # nothing
        assert (
            anonymous.post(path, data=values, headers=tw.r.run.run.with_token()).status_code == 403
        )  # token, no origin
        wrong = {**htmx, "Origin": "https://attacker.example.test"}
        assert anonymous.post(path, data=values, headers=wrong).status_code == 403
        no_token = {"Origin": tw.r.run.run.action()["origin"]}
        assert anonymous.post(path, data=values, headers=no_token).status_code == 403
    assert snapshot(tw.r) == before and tw.spy.requests == []

    # The angle is versioned: no version is a bad request, a stale one a conflict, and neither
    # changes anything.
    path = action_url(app.id, 2, "angle")
    fields = {"name": "Steady hands", "description": "Calm delivery."}
    assert tw.post_fields(path, fields).status_code == 400
    version = tw.angle_version(app.id)
    assert tw.post_fields(path, {**fields, "version": version}).status_code == 200
    stale = tw.post_fields(path, {**fields, "version": version, "name": "Other"})
    assert stale.status_code == 409 and VersionConflict.message in said_text(stale.text)
    assert [row["name"] for row in tw.rows("SELECT name FROM angles")] == ["Steady hands"]


# --- starting a step ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task_type", "step", "action", "message"),
    [
        (GAP_CHECK, 1, "check", "Gap check started."),
        (PROPOSE_ANGLES, 2, "propose", "Angle proposals started."),
    ],
)
def test_tailoring_steps_start_through_budget_gate_with_dedupe(
    tw: Tailoring, task_type: str, step: int, action: str, message: str
):
    app = tw.ready()
    second = tw.ready("second-job")
    usage = tw.r.run.app.state.usage
    service = tw.r.run.app.state.tailoring
    start = service.start_gap_check if task_type == GAP_CHECK else service.start_angles
    path = action_url(app.id, step, action)

    def set_budget(**limits: int) -> None:
        budget = Budget(reset_day=1, **limits)
        usage.save_budget("fake", budget, expected_version=usage.load_budgets().version)

    def use(count: int) -> None:
        for _ in range(count):
            record = usage.reserve("fake", tier="thorough", task_type=task_type)
            usage.complete(record, success=True, tokens=None)

    def tasks() -> list[dict[str, Any]]:
        return tw.rows("SELECT * FROM tasks WHERE type=?", (task_type,))

    # A warning is due: the dialog asks first and nothing starts.
    set_budget(allowance=10, first_warning=3, second_warning=5, block_at=8)
    use(2)
    warned = tw.post_fields(path, {})
    assert warned.status_code == 200 and "Continue" in warned.text
    assert tasks() == [] and tw.spy.requests == []
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', warned.text))

    # Continue starts exactly one task for this application; the running task is returned for
    # a second start (route and service), and another application gets its own.
    tw.r.run.hold.hold = True
    accepted = tw.post_fields(path, fields)
    assert accepted.status_code == 200 and message in announced(accepted)
    tw.r.run.wait_entered(1)
    (task,) = tasks()
    assert task["dedupe_key"] == f"{task_type}:{app.id}"
    assert tw.post_fields(path, {}).status_code == 200
    assert [row["id"] for row in tasks()] == [task["id"]]
    assert start(app.id).id == task["id"]
    other = start(second.id)
    assert other.id != task["id"]
    assert {row["dedupe_key"] for row in tasks()} == {
        f"{task_type}:{app.id}",
        f"{task_type}:{second.id}",
    }
    tw.r.run.hold.open()
    tw.r.run.wait_terminal(task["id"])
    tw.r.run.wait_terminal(other.id)
    # The request was metered on Thorough, the tier of both tasks.
    tiers = tw.rows("SELECT DISTINCT tier FROM usage_records WHERE task_type=?", (task_type,))
    assert {row["tier"] for row in tiers} == {"thorough"}

    # At the blocking point nothing starts: 409 with the reason, the button disabled, no task.
    (count,) = tw.rows("SELECT count(*) AS n FROM usage_records")
    use(8 - count["n"])
    known, requests = len(tasks()), len(tw.spy.requests)
    blocked = tw.post_fields(path, {})
    assert blocked.status_code == 409 and "Monthly limit reached" in said_text(blocked.text)
    assert len(tasks()) == known and len(tw.spy.requests) == requests
    button = by_id(tw.page(app.id, step).text, "gap-check-start" if step == 1 else "angle-propose")
    assert button.attrs.get("aria-disabled") == "true"


def test_tailoring_results_are_written_with_task_success_or_not_at_all_gap_check(tw: Tailoring):
    app = tw.ready()
    service = tw.r.run.app.state.tailoring
    tw.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.type='gap_check' AND NEW.status='succeeded' "
        "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    ref = service.start_gap_check(app.id)
    assert tw.r.run.wait_terminal(ref.id).status.value == "failed"
    assert tw.rows("SELECT * FROM gap_checks") == []
    assert tw.rows("SELECT * FROM gap_check_facts") == []
    tw.r.run.execute("DROP TRIGGER fictional_abort")

    tw.r.run.hold.hold = True
    ref = service.start_gap_check(app.id)
    tw.r.run.wait_entered(2)
    tw.r.run.app.state.task_service.request_cancel(ref.id)
    tw.r.run.hold.open()
    assert tw.r.run.wait_terminal(ref.id).status.value == "cancelled"
    assert tw.rows("SELECT * FROM gap_checks") == []

    ref = service.start_gap_check(app.id)
    assert tw.r.run.wait_terminal(ref.id).status.value == "succeeded"
    (row,) = tw.rows("SELECT task_id FROM gap_checks")
    assert row["task_id"] == ref.id


def test_tailoring_results_are_written_with_task_success_or_not_at_all_angles(tw: Tailoring):
    app = tw.ready()
    service = tw.r.run.app.state.tailoring
    tw.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.type='propose_angles' AND NEW.status='succeeded' "
        "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    ref = service.start_angles(app.id)
    assert tw.r.run.wait_terminal(ref.id).status.value == "failed"
    assert tw.rows("SELECT * FROM angle_proposals") == []
    tw.r.run.execute("DROP TRIGGER fictional_abort")

    tw.r.run.hold.hold = True
    ref = service.start_angles(app.id)
    tw.r.run.wait_entered(2)
    tw.r.run.app.state.task_service.request_cancel(ref.id)
    tw.r.run.hold.open()
    assert tw.r.run.wait_terminal(ref.id).status.value == "cancelled"
    assert tw.rows("SELECT * FROM angle_proposals") == []

    ref = service.start_angles(app.id)
    assert tw.r.run.wait_terminal(ref.id).status.value == "succeeded"
    (row,) = tw.rows("SELECT task_id FROM angle_proposals")
    assert row["task_id"] == ref.id


def test_tailoring_fake_output_is_labelled_practice_data(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, None)  # the app's own fake answers, as in the demo
    tw.propose(app.id, None)

    first = tw.page(app.id, 1).text
    assert PRACTICE_NOTICE in said_text(first)
    # The fake never reads the posting or the facts, so no requirement is shown as shown by facts.
    assert set(verdicts(first).values()) == {"missing"}
    second = tw.page(app.id, 2).text
    assert PRACTICE_NOTICE in said_text(second)
    names = [item.text for item in angle_items(second)]
    assert len(names) == 2 and "Practice angle one" in names[0]
    assert all(NOT_IN_FACT_BANK in text for text in names)  # they cite nothing
    for table in ("gap_checks", "angle_proposals"):
        assert {row["provider"] for row in tw.rows(f"SELECT provider FROM {table}")} == {"fake"}  # noqa: S608


def test_gap_check_and_angles_requests_have_no_personal_details(tw: Tailoring):
    app = tw.ready()
    settings = SettingsService(tw.db, tw.r.run.clock)
    settings.save(
        PERSONAL_DETAILS, DETAILS, expected_version=settings.load(PERSONAL_DETAILS).version
    )
    assert settings.load(PERSONAL_DETAILS).value.name == DETAILS.name

    tw.check(app.id, "ok")
    tw.propose(app.id, "ok")

    sent = tw.requests(GAP_CHECK) + tw.requests(PROPOSE_ANGLES)
    assert len(sent) == 2
    stored = [
        repr(tw.rows(f"SELECT * FROM {table}"))  # noqa: S608
        for table in ("gap_checks", "gap_check_facts", "angle_proposals")
    ]
    pages = [tw.page(app.id, step).text for step in (1, 2)]
    for value in personal_values():
        for request in sent:
            assert value not in repr(request), value
        assert all(value not in text for text in (*stored, *pages)), value
