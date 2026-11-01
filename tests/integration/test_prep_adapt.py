"""Adapting an earlier answer through the real service, task and routes (M8.2 part C; AC-M8.1
carrying answers over; AC-M8.4 honesty rule for generated prep text; spec 3.1 items 1 to 6, 3.2,
3.3; ADR-113 item 8; ADR-124 items 7, 8 and 11) on fictional temporary data.

The assumed interface, the stored shape, the routes, the markup hooks and the announcement texts
are written in the docstring of ``tests/prep_adapt_pages.py``; the prompt is pinned by
``tests/unit/modules/ai/test_adapt_answer_prompt.py``. The narrative cards this feature mirrors
are pinned by ``tests/integration/test_prep_cards.py``.
"""

import logging
import re
from typing import Any

import pytest

from app.domain.errors import ApplicationNotFound, VersionConflict
from app.modules.ai.prompts import HONESTY_NOTICE
from app.services.documents import (
    SAVE_AS_FACT,
    ApprovalBlocked,
    GenerateNotReady,
    NothingToApprove,
    UnitInvalid,
    UnitNotFound,
)
from app.services.facts import FactInput
from app.services.tailoring import NO_FACTS
from tests.asgi_stream import open_stream
from tests.board_pages import elements
from tests.integration.test_board import snapshot
from tests.integration.test_inbox import get
from tests.integration.test_m4_cross_cutting import assert_stored_only_in, tables_containing
from tests.integration.test_prep_cards import block_of
from tests.integration.test_prep_practice import block_budget
from tests.leak_scan import RawLogRecords
from tests.prep_adapt_pages import (
    ANNOUNCE,
    COMPANY,
    INJECTION_OLD,
    OLD,
    P1,
    P2,
    QUESTION,
    TARGET_COMPANY,
    TARGET_TITLE,
    TASK,
    AdaptDesk,
    adapt_app,
)
from tests.refine_harness import refining
from tests.tailoring_ai_inputs import FACT_1, FACT_2, FACT_3, HIDDEN
from tests.tailoring_pages import DRAFT_TEXT, announced, by_id, form_values, has_id, said_text

pytestmark = pytest.mark.integration

OPEN = "<untrusted_document"
CLOSE = "</untrusted_document>"
BLOCKS = 5  # old_answer, question, facts, angle, job_context
READY_ANSWER = "I planned 60 delivery routes a week with a team of 3, and I kept the plan on time."
LATER_UNREADY = "A later and unready try: I planned the depot routes and told my team the plan."
NEW_CLAIM_TEXT = "I planned 60 delivery routes a week with a team of 9 and a budget of $2m."
SKILL_OLD = "I planned 60 delivery routes a week using Terraform with a team of 3."

# case -> (fixture, the old answer the person wrote, flags as (kind, claim, unit text)).
ADVERSARIAL = {
    "old_employer": (
        "adversarial_old_employer",
        "I planned 60 delivery routes a week with a team of 3. I led route planning at Globex "
        "Corporation.",
        [("needs_check", "Globex Corporation", "Led route planning at Globex Corporation.")],
    ),
    "old_number": (
        "adversarial_old_number",
        "I planned 60 delivery routes a week with a team of 3. I cut invoice time by 85% across "
        "14 warehouses.",
        [
            ("needs_check", "85%", "Cut invoice processing time by 85% across 14 warehouses."),
            ("needs_check", "14", "Cut invoice processing time by 85% across 14 warehouses."),
        ],
    ),
    "old_skill": (
        "adversarial_old_skill",
        SKILL_OLD,
        [
            (
                "needs_check",
                "Terraform",
                "I planned 60 delivery routes a week using Terraform with a team of 3.",
            )
        ],
    ),
    "unknown_id": (
        "adversarial_unknown_id",
        OLD,
        [("unsupported", "F999@1", "Managed a regional planning office.")],
    ),
    "never_use_id": (
        "adversarial_never_use_id",
        OLD,
        [("unsupported", "F4@1", P1)],
    ),
    "added_details": (
        "adversarial_added_details",
        OLD,
        [("needs_check", "ran the depot's night shift", P1)],
    ),
}


@pytest.fixture
def d(tmp_path):
    with adapt_app(tmp_path) as desk:
        yield desk


def sessions_link(page: str) -> bool:
    """The no-fact state's link to a session (``a#adapt-sessions``, in either attribute order)."""
    found = re.search(r'<a [^>]*id="adapt-sessions"[^>]*>', page)
    return found is not None and 'href="/sessions/new"' in found.group(0)


def adapted_page(d: AdaptDesk, aid: str | None = None) -> str:
    return get(d.r, d.path("", aid)).text


def tag(page: str, element_id: str) -> str:
    found = re.search(rf"<[a-z0-9]+ [^>]*id=\"{element_id}\"[^>]*>", page)
    assert found is not None, element_id
    return found.group(0)


# --- AC-M8.1: a reused question brings its best practiced answer ---------------------------------


def test_ac_m8_1_reused_question_brings_best_answer_and_adapts(d: AdaptDesk):
    assert d.view().earlier is None and d.view().can_adapt is False
    d.practice(d.src, "A first, older try for the depot.")
    d.practice(d.src, READY_ANSWER)
    d.mark_ready(d.src, d.qid, True)
    elsewhere = d.tw.ready("adapt-later-job").id
    d.practice(elsewhere, LATER_UNREADY)  # a later attempt, but its item is not ready

    view = d.view()

    assert view.earlier.text == READY_ANSWER, "a ready item's latest attempt beats a later try"
    assert view.earlier.ready is True and view.can_adapt is True
    assert view.answer.state == "none" and view.answer.version is None
    page = adapted_page(d)
    assert "From your earlier answer" in said_text(page)
    assert READY_ANSWER in said_text(by_id(page, "adapt-earlier").text)
    assert LATER_UNREADY not in said_text(page)
    form = by_id(page, "adapt-form")
    assert form_values(page, form) == {"version": ""}
    assert re.search(r'<button[^>]*id="adapt-submit"[^>]*>\s*Adapt to this role', page)
    requests = len(d.r.run.spy.requests)

    ref = d.adapt("ok")

    assert len(d.r.run.spy.requests) == requests + 1
    (document,) = d.rows("SELECT * FROM prep_documents")
    assert (document["application_id"], document["kind"], document["question_id"]) == (
        d.aid,
        "adapted_answer",
        d.qid,
    )
    (version,) = d.versions()
    assert document["current_version_id"] == version["id"]
    assert (version["status"], version["number"], version["origin"]) == ("draft", 1, "generated")
    assert (version["parent_version_id"], version["reused_version_id"]) == (None, None)
    assert (version["provider"], version["model"]) == ("fake", "fake-thorough")
    assert (version["prompt_id"], version["prompt_version"]) == (TASK, "2")
    assert (version["angle_id"], version["angle_text"]) == (None, "")
    rows = d.units(version["id"])
    assert [row["text"] for row in rows] == [P1, P2]
    assert {(row["section"], row["kind"], row["origin"], row["role_id"]) for row in rows} == {
        ("answer", "content", "ai", None)
    }
    assert [row["cited"] for row in rows] == [["F1@1"], ["F2@1"]]
    assert all(row["flag_list"] == [] and row["details"] == [] for row in rows)
    assert d.sent_pairs(version["id"]) == {(1, 1), (2, 1), (3, 1)}, "not F4 (never use), not F5"
    (task,) = d.rows("SELECT * FROM tasks WHERE type=?", (TASK,))
    assert task["id"] == ref.id and task["status"] == "succeeded"
    assert task["dedupe_key"] == f"{TASK}:{d.aid}/{d.qid}"
    (usage,) = d.rows("SELECT * FROM usage_records WHERE task_type=?", (TASK,))
    assert (usage["provider"], usage["tier"]) == ("fake", "thorough")
    (request,) = d.requests()
    assert READY_ANSWER in block_of(request.user, "old_answer")
    assert LATER_UNREADY not in request.user and "first, older try" not in request.user
    # The screen: a draft with its paragraphs, the earlier answer still shown.
    answer = d.answer()
    assert answer.state == "draft" and answer.version.id == version["id"]
    assert [u.text for u in answer.units] == [P1, P2] and answer.blockers == ()
    assert answer.label == "Adapted answer" and answer.kind == "adapted_answer"
    page = adapted_page(d)
    assert by_id(page, "adapt").attrs["data-state"] == "draft"
    assert READY_ANSWER in said_text(by_id(page, "adapt-earlier").text)
    assert "Draft, version 1. Not approved yet." in said_text(by_id(page, "adapt-status").text)
    assert P1 in said_text(page) and P2 in said_text(page)

    # Unmark the ready item: the latest attempt overall is now the best.
    d.mark_ready(d.src, d.qid, False)
    assert d.view().earlier.text == LATER_UNREADY and d.view().earlier.ready is False


def test_the_latest_attempt_is_used_when_no_item_is_ready(d: AdaptDesk):
    elsewhere = d.tw.ready("adapt-later-job").id
    d.practice(d.src, "An older try.")
    d.practice(elsewhere, LATER_UNREADY)
    d.practice(d.src, READY_ANSWER)

    assert d.view().earlier.text == READY_ANSWER and d.view().earlier.ready is False

    d.mark_ready(elsewhere, d.qid, True)

    assert d.view().earlier.text == LATER_UNREADY, "now a ready item exists, it comes first"


def test_the_latest_of_two_ready_items_wins(d: AdaptDesk):
    elsewhere = d.tw.ready("adapt-later-job").id
    d.practice(d.src, "The older ready try.", ready=True)
    d.practice(elsewhere, LATER_UNREADY, ready=True)

    assert d.view().earlier.text == LATER_UNREADY


def test_the_earlier_answer_is_never_this_applications_own_or_another_questions(d: AdaptDesk):
    d.practice(d.aid, "My own answer in this application.", ready=True)  # the target itself
    other_question = d.question("Describe a hard decision you made.")
    d.practice(d.src, "An answer to another question.", qid=other_question, ready=True)

    assert d.view().earlier is None and d.view().can_adapt is False
    assert not has_id(adapted_page(d), "adapt")

    d.practice(d.src, READY_ANSWER)

    assert d.view().earlier.text == READY_ANSWER
    d.practice(d.aid, "An even newer answer of my own.", ready=True)
    assert d.view().earlier.text == READY_ANSWER, "own attempts never count"
    assert d.view(d.aid, other_question).earlier.text == "An answer to another question."
    # Seen from the application where the answers were practiced, the target's try is "other".
    assert d.view(d.src, d.qid).earlier.text == "An even newer answer of my own."


def test_the_earlier_answer_carries_only_application_independent_provenance(d: AdaptDesk):
    d.practice(d.src, READY_ANSWER, ready=True)
    (attempt,) = d.rows("SELECT * FROM practice_attempts WHERE answer_text=?", (READY_ANSWER,))

    earlier = d.view().earlier

    assert earlier.practiced_at == attempt["created_at"]
    assert earlier.ready is True
    shown = repr(earlier)
    for private in (d.src, attempt["id"], attempt["prep_item_id"], COMPANY, "Route Planner"):
        assert private not in shown, private
    page = adapted_page(d)
    meta = said_text(by_id(page, "adapt-earlier-meta").text)
    assert "Practiced" in meta
    for private in (COMPANY, d.src, attempt["id"]):
        assert private not in said_text(by_id(page, "adapt").text), private


def test_the_screen_of_a_question_with_an_earlier_answer_escapes_it_and_writes_nothing(
    d: AdaptDesk,
):
    hostile = "I planned <script>alert(1)</script> 60 routes & told 3 people."
    d.practice(d.src, hostile)
    before, requests = snapshot(d.r), len(d.r.run.spy.requests)

    shown = d.get()
    d.view()

    assert shown.status_code == 200
    assert "<script>alert(1)</script>" not in shown.text and "&lt;script&gt;" in shown.text
    assert snapshot(d.r) == before and len(d.r.run.spy.requests) == requests
    assert d.rows("SELECT id FROM tasks WHERE type=?", (TASK,)) == []
    assert d.rows("SELECT id FROM prep_documents") == []


# --- no button, no start -------------------------------------------------------------------------


def test_no_earlier_answer_means_no_button_and_the_post_is_refused(d: AdaptDesk):
    d.practice(d.aid, OLD, ready=True)  # only this application's own try exists
    page = adapted_page(d)
    assert not has_id(page, "adapt") and not has_id(page, "adapt-form")
    assert not has_id(page, "adapt-submit")
    before = snapshot(d.r)

    with pytest.raises(GenerateNotReady) as refused:
        d.service.start_adapt(d.aid, d.qid)
    with pytest.raises(GenerateNotReady):
        d.service.plan_adapt(d.aid, d.qid)
    posted = d.post(d.path("/adapt"), {"version": ""})

    assert refused.value.reason == "answer" and refused.value.message
    assert posted.status_code == 409 and refused.value.message in said_text(posted.text)
    assert snapshot(d.r) == before and d.requests() == []


def test_without_a_usable_fact_the_earlier_answer_is_shown_but_nothing_is_asked(tmp_path):
    with adapt_app(tmp_path, facts=False) as d:
        d.practice(d.src, OLD)
        assert d.view().earlier.text == OLD and d.view().can_adapt is False
        page = adapted_page(d)
        assert OLD in said_text(by_id(page, "adapt-earlier").text)
        assert not has_id(page, "adapt-form") and not has_id(page, "adapt-submit")
        assert sessions_link(page), "a way to add facts: a session"
        before = snapshot(d.r)

        with pytest.raises(GenerateNotReady) as refused:
            d.service.start_adapt(d.aid, d.qid)
        posted = d.post(d.path("/adapt"), {"version": ""})

        assert refused.value.reason == "facts" and refused.value.message == NO_FACTS
        assert posted.status_code == 409 and NO_FACTS in said_text(posted.text)
        assert snapshot(d.r) == before and d.requests() == []


def test_an_approved_adaptation_hides_the_button_until_it_is_changed(d: AdaptDesk):
    d.ready_to_adapt()
    version = d.approved()

    page = adapted_page(d)
    assert d.view().can_adapt is False
    assert not has_id(page, "adapt-form") and not has_id(page, "adapt-submit")
    assert has_id(page, "adapt-approved") and by_id(page, "adapt").attrs["data-state"] == "approved"
    assert "Approved. Changing a paragraph starts a new draft." in said_text(
        by_id(page, "adapt-approved").text
    )

    d.service.edit_adapted(d.aid, d.qid, version, d.unit_with(version, P1)["id"], P1 + " Really.")

    page = adapted_page(d)
    assert d.view().can_adapt is True and d.answer().state == "draft"
    assert has_id(page, "adapt-submit") and not has_id(page, "adapt-approved")


def test_a_question_outside_the_applications_scope_is_not_found_everywhere(d: AdaptDesk):
    d.ready_to_adapt()
    company = d.rows("SELECT id FROM companies WHERE name=?", (COMPANY,))[0]["id"]
    foreign = d.question("Why do you want to join this fictional company?", company_id=company)
    version = "some-version"
    before = snapshot(d.r)

    for qid in (foreign, "no-such-question", "bad id"):
        with pytest.raises(ApplicationNotFound):
            d.service.adapted_view(d.aid, qid)
        with pytest.raises(ApplicationNotFound):
            d.service.start_adapt(d.aid, qid)
    for aid in ("no-such-application", "bad id", ""):
        with pytest.raises(ApplicationNotFound):
            d.service.adapted_view(aid, d.qid)
        with pytest.raises(ApplicationNotFound):
            d.service.start_adapt(aid, d.qid)
    for qid in (foreign, "no-such-question", "bad%20id"):
        assert get(d.r, d.path("", d.aid, qid)).status_code == 404
        for tail, values in (
            ("/adapt", {"version": ""}),
            (f"/adapted/{version}/approve", {}),
            (f"/adapted/{version}/units/some-unit/remove", {}),
            (f"/adapted/{version}/units/some-unit/edit", {"text": "x"}),
            (f"/adapted/{version}/units/some-unit/confirm", {}),
        ):
            assert d.post(d.path(tail, d.aid, qid), values).status_code == 404, (qid, tail)
    for aid in ("no-such-application", "bad%20id"):
        assert d.post(d.path("/adapt", aid), {"version": ""}).status_code == 404
    assert snapshot(d.r) == before and d.requests() == []


# --- the request ---------------------------------------------------------------------------------


def test_the_request_holds_the_old_answer_question_facts_angle_and_job_in_untrusted_blocks(
    d: AdaptDesk,
):
    d.set_angle()
    d.ready_to_adapt(READY_ANSWER)

    d.adapt("ok")

    (request,) = d.requests()
    assert request.model == "fake-thorough" and request.allow_web_search is False
    assert request.on_tool_use is None
    assert request.system.endswith(HONESTY_NOTICE) and request.system.count(HONESTY_NOTICE) == 1
    user = request.user
    assert user.count(OPEN) == user.count(CLOSE) == BLOCKS
    for source in ("old_answer", "question", "facts", "angle", "job_context"):
        assert f'<untrusted_document source="{source}"' in user
    assert READY_ANSWER in block_of(user, "old_answer")
    assert QUESTION in block_of(user, "question")
    assert "Planner" in block_of(user, "angle") and "Lead with routes." in block_of(user, "angle")
    context = block_of(user, "job_context")
    assert TARGET_TITLE in context and TARGET_COMPANY in context
    for text in (FACT_1, FACT_2, FACT_3, "F1@1", "F2@1", "F3@1"):
        assert text in block_of(user, "facts"), text
    # The old answer is data: it appears nowhere but its own block, and neither does the source
    # application's identity.
    outside = re.sub(r"<untrusted_document.*?</untrusted_document>", "", user, flags=re.S)
    assert READY_ANSWER not in outside and READY_ANSWER not in request.system
    assert COMPANY not in user and "salary" not in user.lower()
    assert HIDDEN not in user and "F4@" not in user and "F5@" not in user
    (version,) = d.versions()
    assert version["angle_id"] is not None and "Lead with routes." in version["angle_text"]


def test_never_use_and_draft_facts_are_absent_from_the_adapt_answer_request(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")

    (request,) = d.requests()
    everything = request.system + request.user + str(request.output_schema)
    assert HIDDEN not in everything and "Brightwater" not in everything
    assert "F4@" not in everything and "F5@" not in everything and DRAFT_TEXT not in everything
    assert d.fact(4)["privacy"] == "never_use"

    d.withdraw("never_use", 1)
    d.adapt(data={"units": [{"text": P2, "fact_ids": ["F2@1"], "added_details": []}]})

    again = d.requests()[-1]
    assert FACT_1 not in again.user and "F1@" not in again.user
    assert FACT_2 in again.user
    first, second = d.versions()
    assert d.sent_pairs(second["id"]) == {(2, 1), (3, 1)}
    assert first["number"] == 1 and second["parent_version_id"] == first["id"]


def test_adapt_answer_injection_in_the_old_answer_changes_nothing(d: AdaptDesk):
    hostile = INJECTION_OLD.read_text()
    d.ready_to_adapt(hostile)
    stable = (
        "jobs",
        "facts",
        "fact_versions",
        "fact_events",
        "skills",
        "fact_skills",
        "roles",
        "status_history",
        "companies",
        "angles",
        "workspaces",
    )
    before = {t: d.rows(f"SELECT * FROM {t} ORDER BY 1") for t in stable}  # noqa: S608

    d.adapt("injection_obeyed")

    (request,) = d.requests()
    assert "disregard the rules above" in block_of(request.user, "old_answer")
    assert "disregard the rules above" not in request.system
    assert request.user.count(OPEN) == request.user.count(CLOSE) == BLOCKS
    assert "&lt;/untrusted_document>" in request.user
    assert not request.allow_web_search and request.on_tool_use is None
    for table in stable:
        assert d.rows(f"SELECT * FROM {table} ORDER BY 1") == before[table], table  # noqa: S608
    (version,) = d.versions()
    flagged = {(k, c) for k, c, _ in d.flag_triples(version["id"])}
    assert ("unsupported", "F4@1") in flagged and ("needs_check", "Globex Corporation") in flagged
    assert d.sent_pairs(version["id"]) == {(1, 1), (2, 1), (3, 1)}, "F4 was never sent"
    assert version["status"] == "draft" and d.answer().approved is False
    with pytest.raises(ApprovalBlocked):
        d.service.approve_adapted(d.aid, d.qid, version["id"])
    assert d.rows("SELECT id FROM facts WHERE number>5") == []


# --- the honesty rule: flags block approval ------------------------------------------------------


@pytest.mark.parametrize("case", list(ADVERSARIAL), ids=list(ADVERSARIAL))
def test_adapt_answer_adversarial_flagged_and_approval_blocked(d: AdaptDesk, case: str):
    fixture, old_answer, expected = ADVERSARIAL[case]
    if case == "old_skill":
        d.known_skill("Terraform")
    d.ready_to_adapt(old_answer)

    d.adapt(fixture)

    (version,) = d.versions()
    assert old_answer in block_of(d.requests()[0].user, "old_answer")
    assert d.flag_triples(version["id"]) == expected
    answer = d.answer()
    assert [(f.kind, f.claim) for u in answer.units for f in u.flags] == [
        (kind, claim) for kind, claim, _ in expected
    ]
    assert {(b.kind, b.claim) for b in answer.blockers} >= {
        (kind, claim) for kind, claim, _ in expected
    }
    assert answer.state == "draft" and answer.approved is False
    before = d.adapt_tables()
    with pytest.raises(ApprovalBlocked) as blocked:
        d.service.approve_adapted(d.aid, d.qid, version["id"])
    assert blocked.value.message == "1 paragraph needs your check."
    refused = d.post(d.approve_path(version["id"]))
    assert refused.status_code == 409 and "1 paragraph needs your check." in said_text(refused.text)
    assert d.adapt_tables() == before and d.version_row(version["id"])["status"] == "draft"
    page = adapted_page(d)
    assert re.search(r'<button[^>]*id="adapt-approve"[^>]*aria-disabled="true"', page)
    assert "1 paragraph needs your check." in said_text(by_id(page, "adapt-approve-reason").text)
    assert 'data-flagged="true"' in page


def test_the_target_applications_title_and_company_are_the_only_name_sources(d: AdaptDesk):
    d.ready_to_adapt()

    d.adapt("adversarial_source_company")

    (first,) = d.versions()
    assert d.flag_triples(first["id"]) == [
        (
            "needs_check",
            COMPANY,
            "I planned 60 delivery routes a week with a team of 3, and I want to do it at "
            "Harbor Kite Fictional.",
        )
    ], "the application the answer was practiced in is not a source of names here"
    with pytest.raises(ApprovalBlocked):
        d.service.approve_adapted(d.aid, d.qid, first["id"])

    d.adapt("target_names")

    second = d.versions()[1]
    assert d.flag_triples(second["id"]) == [], "this job's own title and company are allowed"
    d.service.approve_adapted(d.aid, d.qid, second["id"])
    assert d.answer().approved is True


def test_a_clean_adaptation_is_approved_once(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")
    version = d.version_id()

    d.service.approve_adapted(d.aid, d.qid, version)

    assert d.version_row(version)["status"] == "approved"
    answer = d.answer()
    assert (answer.state, answer.approved, answer.needs_recheck) == ("approved", True, False)
    with pytest.raises(VersionConflict):
        d.service.approve_adapted(d.aid, d.qid, version)


@pytest.mark.parametrize("fixture", ["malformed", "empty", "schema_invalid", "too_many_units"])
def test_adapt_answer_malformed_retry_then_invalid(d: AdaptDesk, fixture: str):
    d.ready_to_adapt()

    d.adapt(fixture, status="failed")

    assert len(d.requests()) == 2, "one retry, then the task fails"
    assert all(rows == [] for rows in d.adapt_tables().values())
    answer = d.answer()
    assert answer.state == "failed" and answer.task.error
    page = adapted_page(d)
    assert re.search(r'<p[^>]*id="adapt-error"[^>]*role="alert"', page)
    # The earlier answer stays and trying again is one more start, and it works.
    assert OLD in said_text(by_id(page, "adapt-earlier").text) and has_id(page, "adapt-submit")
    d.adapt("ok")
    assert d.answer().state == "draft"


def test_the_adaptation_is_written_with_task_success_or_not_at_all(d: AdaptDesk):
    d.ready_to_adapt()
    d.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.type='adapt_answer' AND NEW.status='succeeded' "
        "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )

    d.adapt("ok", status="failed")

    assert all(rows == [] for rows in d.adapt_tables().values())
    assert d.answer().state == "failed"


def test_the_same_adaptation_started_twice_runs_once_and_shows_running(d: AdaptDesk):
    d.ready_to_adapt()
    d.script("ok")
    d.r.run.hold.hold = True
    try:
        first = d.service.start_adapt(d.aid, d.qid)
        d.r.run.wait_entered(1)

        second = d.service.start_adapt(d.aid, d.qid)

        assert second.id == first.id
        assert len(d.rows("SELECT id FROM tasks WHERE type=?", (TASK,))) == 1
        assert d.answer().state == "running" and d.answer().task.active is True
        page = adapted_page(d)
        assert re.search(r'<p[^>]*id="adapt-running"[^>]*role="status"', page)
        assert by_id(page, "adapt").attrs["data-state"] == "running"
        assert "every 2s" in tag(page, "prep"), "the region refreshes itself while it runs"
        assert re.search(r'<button[^>]*id="adapt-submit"[^>]*aria-disabled="true"', page)
        assert d.answer().version is None, "nothing is written before the task succeeds"
    finally:
        d.r.run.hold.open()
    d.r.run.wait_terminal(first.id)
    assert d.answer().state == "draft"


# --- editing -------------------------------------------------------------------------------------


def draft_ok(d: AdaptDesk) -> str:
    d.ready_to_adapt()
    d.adapt("ok")
    return str(d.version_id())


def test_an_edit_makes_a_new_version_and_is_rechecked_against_the_facts_sent(d: AdaptDesk):
    v1 = draft_ok(d)
    before_units = d.units(v1)
    unit = d.unit_with(v1, P1)

    outcome = d.service.edit_adapted(d.aid, d.qid, v1, unit["id"], NEW_CLAIM_TEXT)

    assert outcome.offer is not None and outcome.offer.question == SAVE_AS_FACT
    assert outcome.offer.claims == ("9", "$2m") and outcome.offer.unit_id == outcome.unit_id
    v2 = d.version_row(outcome.version_id)
    assert (v2["status"], v2["parent_version_id"], v2["origin"], v2["number"]) == (
        "draft",
        v1,
        "user",
        2,
    )
    assert d.sent_pairs(v2["id"]) == d.sent_pairs(v1), "the sent facts are copied"
    edited = d.unit_with(v2["id"], NEW_CLAIM_TEXT)
    assert edited["id"] == outcome.unit_id and edited["id"] != unit["id"]
    assert edited["origin"] == "user" and edited["cited"] == ["F1@1"]
    assert [(f["kind"], f["claim"]) for f in edited["flag_list"]] == [
        ("needs_check", "9"),
        ("needs_check", "$2m"),
    ]
    assert d.texts(v2["id"])[1] == P2
    assert d.units(v1) == before_units and d.version_row(v1)["status"] == "draft"
    assert (
        d.rows("SELECT current_version_id FROM prep_documents")[0]["current_version_id"]
        == (v2["id"])
    )
    assert d.answer().blockers and d.answer().state == "draft"


def test_an_edit_that_stays_within_the_facts_has_no_flag_and_can_be_approved(d: AdaptDesk):
    v1 = draft_ok(d)

    outcome = d.service.edit_adapted(
        d.aid, d.qid, v1, d.unit_with(v1, P1)["id"], "I planned 60 delivery routes weekly."
    )

    assert outcome.offer is None and d.flag_triples(outcome.version_id) == []
    d.service.approve_adapted(d.aid, d.qid, outcome.version_id)


def test_an_edit_may_only_cite_facts_sent_for_that_version(d: AdaptDesk):
    v1 = draft_ok(d)
    later = d.r.typed("A fictional fact the person added after the answer was adapted.")
    assert later.number == 6
    plain = "I planned delivery routes with my team."
    shown = P1

    for ref in ("F6@1", "F4@1"):  # F6 is confirmed but was not sent; F4 is never use
        version = str(d.version_id())
        outcome = d.service.edit_adapted(
            d.aid, d.qid, version, d.unit_with(version, shown)["id"], plain, fact_ids=[ref]
        )
        unit = d.unit_with(outcome.version_id, plain)
        assert [(f["kind"], f["claim"]) for f in unit["flag_list"]] == [("unsupported", ref)]
        assert unit["cited"] == []
        with pytest.raises(ApprovalBlocked):
            d.service.approve_adapted(d.aid, d.qid, outcome.version_id)
        fixed = d.service.edit_adapted(
            d.aid, d.qid, outcome.version_id, unit["id"], plain, fact_ids=["F1@1"]
        )
        assert d.unit_with(fixed.version_id, plain)["flag_list"] == []
        shown = plain
    assert d.version_row(v1)["status"] == "draft"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "x" * 1201, "two\nlines", "bidi ‮ text", "nul \x00 text"],
    ids=["empty", "blank", "too_long", "two_lines", "bidi", "nul"],
)
def test_an_unusable_edit_writes_nothing(d: AdaptDesk, text: str):
    v1 = draft_ok(d)
    before = d.adapt_tables()

    with pytest.raises(UnitInvalid) as refused:
        d.service.edit_adapted(d.aid, d.qid, v1, d.unit_with(v1, P1)["id"], text)

    assert refused.value.message and d.adapt_tables() == before


def test_an_edit_of_an_unknown_unit_or_a_stale_version_writes_nothing(d: AdaptDesk):
    v1 = draft_ok(d)
    unit = d.unit_with(v1, P1)["id"]
    before = d.adapt_tables()

    with pytest.raises(UnitNotFound):
        d.service.edit_adapted(d.aid, d.qid, v1, "no-such-unit", P1)
    for action in (
        lambda: d.service.edit_adapted(d.aid, d.qid, "no-such-version", unit, P1),
        lambda: d.service.remove_adapted(d.aid, d.qid, "no-such-version", unit),
        lambda: d.service.confirm_adapted(d.aid, d.qid, "no-such-version", unit),
        lambda: d.service.approve_adapted(d.aid, d.qid, "no-such-version"),
    ):
        with pytest.raises(VersionConflict):
            action()
    assert d.adapt_tables() == before


def test_a_second_edit_from_the_old_version_is_a_conflict(d: AdaptDesk):
    v1 = draft_ok(d)
    unit = d.unit_with(v1, P1)["id"]
    d.service.edit_adapted(d.aid, d.qid, v1, unit, "I planned 60 routes a week with 3 people.")
    before = d.adapt_tables()

    with pytest.raises(VersionConflict):
        d.service.edit_adapted(d.aid, d.qid, v1, unit, "Another wording of the same unit.")

    assert d.adapt_tables() == before


def test_removing_a_unit_keeps_a_tombstone_and_the_earlier_version(d: AdaptDesk):
    v1 = draft_ok(d)

    v2 = d.service.remove_adapted(d.aid, d.qid, v1, d.unit_with(v1, P2)["id"]).version_id

    assert d.texts(v2) == [P1] and d.texts(v2, removed=True) == [P1, P2]
    assert [row["text"] for row in d.units(v2) if row["removed"]] == [P2]
    assert d.texts(v1) == [P1, P2] and d.version_row(v1)["status"] == "draft"
    assert d.version_row(v2)["parent_version_id"] == v1 and d.sent_pairs(v2) == d.sent_pairs(v1)
    assert [u.text for u in d.answer().units] == [P1]
    d.service.approve_adapted(d.aid, d.qid, v2)
    # Removing the last visible unit leaves nothing to approve.
    v3 = d.service.edit_adapted(d.aid, d.qid, v2, d.unit_with(v2, P1)["id"], P1).version_id
    v4 = d.service.remove_adapted(d.aid, d.qid, v3, d.unit_with(v3, P1)["id"]).version_id
    with pytest.raises(NothingToApprove):
        d.service.approve_adapted(d.aid, d.qid, v4)


def test_confirming_a_flagged_unit_creates_a_confirmed_fact_with_source_document(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("adversarial_old_number")
    v1 = str(d.version_id())
    inflated = "Cut invoice processing time by 85% across 14 warehouses."
    flagged = d.unit_with(v1, inflated)
    before = d.counts()

    # One transaction: when the new version can't be written, no fact is left behind.
    d.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE INSERT ON prep_document_versions "
        "WHEN NEW.number = 2 BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    with pytest.raises(Exception):  # noqa: B017 - the storage error, whatever its wrapper
        d.service.confirm_adapted(d.aid, d.qid, v1, flagged["id"])
    assert d.counts() == before and d.version_id() == v1
    d.r.run.execute("DROP TRIGGER fictional_abort")

    outcome = d.service.confirm_adapted(d.aid, d.qid, v1, flagged["id"])

    after = d.counts()
    assert after["facts"] == before["facts"] + 1
    assert after["fact_versions"] == before["fact_versions"] + 1
    assert after["prep_document_versions"] == before["prep_document_versions"] + 1
    fact = d.fact(6)
    assert fact["status"] == "confirmed" and fact["privacy"] == "normal"
    (wording,) = d.rows("SELECT * FROM fact_versions WHERE fact_id=?", (fact["id"],))
    assert wording["version"] == 1 and wording["text"] == inflated
    assert wording["source"] == "document", "ADR-113 item 8"
    v2 = d.version_row(outcome.version_id)
    assert (v2["status"], v2["parent_version_id"], v2["origin"]) == ("draft", v1, "user")
    unit = d.unit_with(v2["id"], inflated)
    assert unit["id"] == outcome.unit_id and unit["origin"] == "user"
    assert unit["cited"] == ["F6@1"] and unit["flag_list"] == [] and outcome.offer is None
    assert d.texts(v2["id"]) == d.texts(v1), "nothing else changed"
    assert d.answer().blockers == ()
    assert d.unit_with(v1, inflated)["flag_list"], "the version it came from keeps its flags"
    d.service.approve_adapted(d.aid, d.qid, v2["id"])
    # The saved fact is the person's own: a later edit may cite it although it was not sent.
    v3 = d.service.edit_adapted(
        d.aid, d.qid, v2["id"], unit["id"], inflated + " overall."
    ).version_id
    assert d.unit_with(v3, inflated + " overall.")["flag_list"] == []


def test_an_edit_of_an_approved_adaptation_starts_a_new_draft_and_leaves_the_approved_one(
    d: AdaptDesk,
):
    d.ready_to_adapt()
    approved = d.approved()
    frozen = (d.version_row(approved), d.units(approved))

    outcome = d.service.edit_adapted(
        d.aid, d.qid, approved, d.unit_with(approved, P1)["id"], "I planned 60 routes weekly."
    )

    assert outcome.was_approved is True
    assert (d.version_row(approved), d.units(approved)) == frozen
    new = d.version_row(outcome.version_id)
    assert (new["status"], new["parent_version_id"]) == ("draft", approved)
    assert d.answer().state == "draft" and d.answer().approved is False


@pytest.mark.parametrize(
    ("how", "blocker"),
    [("never_use", "unsupported"), ("archived", "unsupported"), ("reworded", "needs_check")],
    ids=["never_use", "archived", "reworded"],
)
def test_an_approved_adaptation_needs_a_recheck_after_a_cited_fact_is_withdrawn(
    d: AdaptDesk, how: str, blocker: str
):
    d.ready_to_adapt()
    version = d.approved()
    assert d.answer().approved is True

    d.withdraw(how, 1)

    answer = d.answer()
    assert d.version_row(version)["status"] == "approved", "history is never rewritten"
    assert answer.state == "recheck" and answer.needs_recheck is True and answer.approved is False
    assert [(b.kind, b.claim) for b in answer.blockers] == [(blocker, "F1@1")]
    with pytest.raises(VersionConflict):
        d.service.approve_adapted(d.aid, d.qid, version)
    page = adapted_page(d)
    assert by_id(page, "adapt").attrs["data-state"] == "recheck"
    assert "Needs re-check, version 1." in said_text(by_id(page, "adapt-status").text)
    assert has_id(page, "adapt-recheck") and not has_id(page, "adapt-approved")
    assert not has_id(page, "adapt-approve")
    # The person fixes it: removing the paragraph that cites the fact starts a new draft.
    fixed = d.service.remove_adapted(d.aid, d.qid, version, d.unit_with(version, P1)["id"])
    assert d.answer().state == "draft" and d.answer().blockers == ()
    d.service.approve_adapted(d.aid, d.qid, fixed.version_id)
    assert d.answer().approved is True


# --- the budget and the version ------------------------------------------------------------------


def test_blocked_by_budget_adapt_enqueues_and_stores_nothing(d: AdaptDesk):
    d.ready_to_adapt()
    block_budget(d.r, uses=9)  # the practice answer used one
    tasks = len(d.rows("SELECT id FROM tasks"))
    usage = len(d.rows("SELECT id FROM usage_records"))

    refused = d.post(d.path("/adapt"), {"version": ""})

    assert refused.status_code == 409
    assert re.search(r'<(p|span)[^>]*id="adapt-reason"', refused.text)
    assert "Monthly limit reached" in said_text(refused.text)
    button = re.search(r'<button[^>]*id="adapt-submit"[^>]*>', refused.text)
    assert button is not None
    assert 'aria-disabled="true"' in button.group(0) and "adapt-reason" in button.group(0)
    assert "<dialog" not in refused.text
    assert len(d.rows("SELECT id FROM tasks")) == tasks and d.requests() == []
    assert len(d.rows("SELECT id FROM usage_records")) == usage
    assert all(rows == [] for rows in d.adapt_tables().values())


def test_a_budget_warning_answers_200_with_the_dialog_and_enqueues_nothing(d: AdaptDesk):
    d.ready_to_adapt()
    block_budget(d.r, uses=3)  # the practice answer used one
    tasks = len(d.rows("SELECT id FROM tasks"))

    asked = d.post(d.path("/adapt"), {"version": ""})

    assert asked.status_code == 200
    assert '<dialog id="adapt-budget-dialog"' in asked.text
    assert len(d.rows("SELECT id FROM tasks")) == tasks and d.requests() == []
    assert all(rows == [] for rows in d.adapt_tables().values())


def test_a_stale_version_starts_nothing(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")
    before, requests = d.counts(), len(d.requests())

    for stale in (None, "no-such-version", str(d.version_id()) + "x"):
        with pytest.raises(VersionConflict):
            d.service.start_adapt(d.aid, d.qid, version=stale)
    posted = d.post(d.path("/adapt"), {"version": "no-such-version"})

    assert posted.status_code == 409 and "Reload" in posted.text
    assert d.counts() == before and len(d.requests()) == requests


def test_plan_adapt_names_the_service_and_tier_the_budget_gate_checks(d: AdaptDesk):
    d.ready_to_adapt()

    plan = d.service.plan_adapt(d.aid, d.qid)

    assert (plan.service, plan.tier) == ("fake", "thorough")
    assert d.service.plan_adapt(d.aid, d.qid, tier="fast").tier == "fast"
    with pytest.raises(ValueError):
        d.service.plan_adapt(d.aid, d.qid, tier="nope")
    assert d.requests() == [] and d.rows("SELECT id FROM tasks WHERE type=?", (TASK,)) == []


# --- routes --------------------------------------------------------------------------------------


def test_adapt_routes_need_token_and_origin_and_the_right_fields(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")
    version = str(d.version_id())
    unit = d.unit_with(version, P1)["id"]
    token = {"X-JP-Token": d.r.run.run.token}
    posts = [
        (d.path("/adapt"), {"version": version}),
        (d.unit_path(version, unit, "edit"), {"text": "x"}),
        (d.unit_path(version, unit, "remove"), {}),
        (d.unit_path(version, unit, "confirm"), {}),
        (d.approve_path(version), {}),
    ]
    before = snapshot(d.r)

    assert d.r.client.get(d.path()).status_code == 403
    for path, data in posts:
        assert d.r.client.post(path, data=data).status_code == 403
        assert d.r.client.post(path, data=data, headers=token).status_code == 403, "no origin"
        assert d.post(path, {**data, "extra": "1"}).status_code == 400, path
        if data:
            assert d.post(path, {}).status_code == 400, f"{path}: the field is required"
    stale_paths = [
        (d.unit_path("no-such-version", unit, "edit"), {"text": "x"}),
        (d.unit_path("no-such-version", unit, "remove"), {}),
        (d.unit_path("no-such-version", unit, "confirm"), {}),
        (d.approve_path("no-such-version"), {}),
        (d.path("/adapt"), {"version": "no-such-version"}),
    ]
    for path, data in stale_paths:
        stale = d.post(path, data)
        assert stale.status_code == 409 and "Reload" in stale.text, path
    assert snapshot(d.r) == before and len(d.requests()) == 1


def test_adapt_routes_answer_404_for_unsafe_or_unknown_ids(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")
    version = str(d.version_id())
    unit = d.unit_with(version, P1)["id"]
    before = snapshot(d.r)

    for path, data in (
        (d.unit_path(version, "no-such-unit", "edit"), {"text": "x"}),
        (d.unit_path(version, "no-such-unit", "remove"), {}),
        (d.unit_path(version, "no-such-unit", "confirm"), {}),
        (d.unit_path(version, "bad%20id", "remove"), {}),
        (d.unit_path("bad%20id", unit, "remove"), {}),
        (d.unit_path(version, unit, "approve"), {}),
        (d.unit_path(version, unit, "nope"), {}),
        (d.approve_path("bad%20id"), {}),
    ):
        assert d.post(path, data).status_code == 404, path
    assert snapshot(d.r) == before


def test_adapt_routes_announce_and_swap_the_prep_fragment(d: AdaptDesk):
    d.ready_to_adapt()
    d.script("ok")
    started = d.post(d.path("/adapt"), {"version": ""})
    assert started.status_code == 200
    assert announced(started) == [ANNOUNCE["adapt"]]
    assert 'id="prep"' in started.text and "<html" not in started.text
    assert re.search(r'<[^>]*id="adapt-heading"[^>]*data-autofocus', started.text)
    assert not re.search(r'<[^>]*id="practice-heading"[^>]*data-autofocus', started.text)
    (task,) = d.rows("SELECT id FROM tasks WHERE type=?", (TASK,))
    assert d.r.run.wait_terminal(task["id"]).status.value == "succeeded"

    v1 = str(d.version_id())
    saved = d.post(
        d.unit_path(v1, d.unit_with(v1, P1)["id"], "edit"),
        {"text": "I planned 60 routes a week."},
    )
    assert saved.status_code == 200 and announced(saved) == [ANNOUNCE["edit"]]
    assert re.search(r'<[^>]*id="adapt-heading"[^>]*data-autofocus', saved.text)
    v2 = str(d.version_id())
    claimed = d.post(
        d.unit_path(v2, d.unit_with(v2, "I planned 60 routes a week.")["id"], "edit"),
        {"text": NEW_CLAIM_TEXT},
    )
    assert claimed.status_code == 200 and announced(claimed) == [ANNOUNCE["edit_claim"]]
    assert 'data-flagged="true"' in claimed.text

    v3 = str(d.version_id())
    confirmed = d.post(d.unit_path(v3, d.unit_with(v3, NEW_CLAIM_TEXT)["id"], "confirm"))
    assert confirmed.status_code == 200 and announced(confirmed) == [ANNOUNCE["confirm"]]
    assert d.fact(6)["status"] == "confirmed"

    v4 = str(d.version_id())
    removed = d.post(d.unit_path(v4, d.unit_with(v4, P2)["id"], "remove"))
    assert removed.status_code == 200 and announced(removed) == [ANNOUNCE["remove"]]

    v5 = str(d.version_id())
    approved = d.post(d.approve_path(v5))
    assert approved.status_code == 200 and announced(approved) == [ANNOUNCE["approve"]]
    assert "Approved. Changing a paragraph starts a new draft." in said_text(approved.text)
    assert d.answer().approved is True

    after = d.post(
        d.unit_path(v5, d.unit_with(v5, NEW_CLAIM_TEXT)["id"], "edit"),
        {"text": NEW_CLAIM_TEXT.replace("I planned", "I organised")},
    )
    assert after.status_code == 200 and announced(after) == [ANNOUNCE["edit_approved"]]
    assert d.answer().state == "draft" and d.version_row(v5)["status"] == "approved"


def test_route_refusals_show_their_messages_and_change_nothing(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("adversarial_unknown_id")
    version = str(d.version_id())
    unit = d.unit_with(version, P1)["id"]
    before = d.adapt_tables()

    blank = d.post(d.unit_path(version, unit, "edit"), {"text": "  "})
    long = d.post(d.unit_path(version, unit, "edit"), {"text": "x" * 1201})
    blocked = d.post(d.approve_path(version))

    assert blank.status_code == long.status_code == 422
    assert "Write the paragraph before saving." in said_text(blank.text)
    assert "1200" in said_text(long.text) or "1,200" in said_text(long.text)
    assert blocked.status_code == 409 and "1 paragraph needs your check." in said_text(blocked.text)
    assert d.adapt_tables() == before


def test_unit_markup_flags_facts_and_forms(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("adversarial_old_employer")
    version = str(d.version_id())
    flagged = d.unit_with(version, "Led route planning at Globex Corporation.")["id"]
    clean = d.unit_with(version, P1)["id"]

    page = adapted_page(d)

    assert by_id(page, "adapt").attrs["data-state"] == "draft"
    nodes = elements(page)
    lines = [e for e in nodes if e.tag == "li" and "data-unit-id" in e.attrs]
    assert [e.attrs["data-unit-id"] for e in lines] == [clean, flagged]
    by_unit = {str(e.attrs["data-unit-id"]): e for e in lines}

    def forms(unit: str) -> dict[str, Any]:
        return {
            str(e.attrs["data-unit-form"]): e
            for e in nodes
            if e.tag == "form" and "data-unit-form" in e.attrs and e.within(by_unit[unit])
        }

    hot, cool = by_unit[flagged], by_unit[clean]
    assert hot.attrs["data-flagged"] == "true" and cool.attrs.get("data-flagged") != "true"
    assert "Globex Corporation" in hot.text and "This is true — save it as a fact" in hot.text
    assert [e for e in nodes if "data-flag-claim" in e.attrs and e.within(hot)]
    assert sorted(forms(flagged)) == ["confirm", "edit", "remove"]
    assert sorted(forms(clean)) == ["edit", "remove"], "an unflagged unit has nothing to confirm"
    assert "Facts used (1)" in cool.text and "Edit this paragraph" in cool.text
    base = d.path(f"/adapted/{version}/units/{clean}")
    assert forms(clean)["edit"].attrs["action"] == f"{base}/edit"
    assert forms(clean)["remove"].attrs["action"] == f"{base}/remove"
    assert form_values(page, forms(clean)["edit"]) == {"text": P1}
    assert re.search(r'<button[^>]*id="adapt-approve"[^>]*aria-disabled="true"', page)
    assert d.approve_path(version) in tag(page, "adapt-approve-form")


def test_adapted_text_is_escaped_in_the_page(d: AdaptDesk):
    d.ready_to_adapt()
    hostile = "I planned routes <script>alert(1)</script> in 2 days."
    d.adapt(data={"units": [{"text": hostile, "fact_ids": ["F1@1"], "added_details": []}]})

    page = adapted_page(d)

    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_the_adapted_answer_of_another_application_is_not_shown_here(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")
    third = d.another("adapt-third-job")
    before = snapshot(d.r)

    page = get(d.r, d.path("", third)).text

    assert P1 not in said_text(page) and not has_id(page, "adapt-units")
    assert has_id(page, "adapt-submit"), "the earlier answer is offered to it as well"
    assert snapshot(d.r) == before


def test_the_tasks_indicator_announces_a_finished_adaptation(d: AdaptDesk):
    d.ready_to_adapt()
    d.adapt("ok")

    indicator = d.r.client.get("/tasks/indicator", headers=d.r.run.run.htmx())

    assert indicator.status_code == 200
    assert "Adapt answer finished." in indicator.headers["HX-Trigger"]


# --- what is stored and where ---------------------------------------------------------------------


def test_adapted_and_old_answer_text_is_absent_from_task_rows_usage_rows_logs_and_events(
    tmp_path, monkeypatch, caplog
):
    raw = RawLogRecords()
    raw.install(monkeypatch)
    caplog.set_level(logging.DEBUG)
    old_marker = "FictionalOldMarkerLarkspur"
    unit_marker = "FictionalAdaptedMarkerPlover"
    edit_marker = "FictionalEditMarkerHeron"
    surfaces: list[str] = []
    with refining(tmp_path) as r:
        portal = r.client.portal
        assert portal is not None
        with portal.wrap_async_context_manager(
            open_stream(r.run.app, "/events", port=8000, headers=r.run.run.htmx())
        ) as stream:
            frames = [portal.call(stream.next_chunk)]
            desk = AdaptDesk(r)
            desk.practice(desk.src, f"{OLD} My notes say {old_marker} twice.")
            desk.adapt(
                data={
                    "units": [
                        {
                            "text": f"{P1} My notes say {unit_marker} twice.",
                            "fact_ids": ["F1@1"],
                            "added_details": [],
                        }
                    ]
                }
            )
            version = str(desk.version_id())
            desk.service.edit_adapted(
                desk.aid,
                desk.qid,
                version,
                desk.answer().units[0].id,
                f"{P1} Also {edit_marker}.",
            )
            while not any(b'"status":"succeeded"' in f.replace(b" ", b"") for f in frames):
                frames.append(portal.call(stream.next_chunk))
            surfaces.extend([repr(stream.headers), b"".join(frames).decode()])
        for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
            response = r.client.get(path, headers=r.run.run.htmx())
            assert response.status_code == 200
            surfaces.extend([response.text, repr(response.headers)])
        for table in ("tasks", "usage_records"):
            rows = r.run.rows(f"SELECT * FROM {table}")  # noqa: S608
            assert rows
            surfaces.append(repr(rows))
        # The controls: the text is stored, and only where each kind of text is kept.
        assert_stored_only_in(r.run, old_marker, frozenset({"practice_attempts"}))
        assert_stored_only_in(r.run, unit_marker, frozenset({"prep_units"}))
        assert_stored_only_in(r.run, edit_marker, frozenset({"prep_units"}))
        assert "prep_units" in tables_containing(r.run, unit_marker)
    surfaces.extend(text for _where, text in raw.entries)
    for marker in (old_marker, unit_marker, edit_marker):
        assert all(marker not in surface for surface in surfaces), marker


def test_adapting_leaves_every_table_it_does_not_own_untouched(d: AdaptDesk):
    d.ready_to_adapt()
    before = snapshot(d.r)

    d.adapt("ok")

    after = snapshot(d.r)
    for table in (
        "facts",
        "fact_versions",
        "fact_events",
        "skills",
        "status_history",
        "jobs",
        "events",
        "documents",
        "document_versions",
        "document_units",
        "bank_questions",
        "bank_question_sightings",
        "prep_items",
        "practice_attempts",
        "angles",
        "workspaces",
        "companies",
    ):
        assert before[table] == after[table], table
    assert len(after["prep_documents"]) == 1 and len(after["prep_version_facts"]) == 3


# --- prompt v2 review fixes: more adversarial outputs ---------------------------------------------

UNCITED = "I stay calm when a deadline moves and I keep everyone informed."
GENERALIZED_FACT = "Ran the Fernhill Bank payments migration."
GENERALIZED_OLD = "I ran the Fernhill Bank payments migration and planned 60 routes a week."
COMPANY_UNIT = "I worked at Other Fictional Co for 5 years, planning 60 delivery routes a week."
EMBELLISHED = "As a senior planner I led 60 delivery routes a week with a team of 3."
INFLATED = "I planned 600 delivery routes a week with a team of 3."

# A NEW list (the one above is unchanged): case -> (fixture, the old answer, flags as
# (kind, claim, unit text)). The embellishment case is not here: the checker cannot flag it.
ADVERSARIAL_V2 = {
    "uncited_paragraph": (
        "adversarial_uncited_paragraph",
        OLD,
        [("unsupported", UNCITED, UNCITED)],
    ),
    "company_claims": (
        "adversarial_company_claims",
        OLD,
        [("needs_check", "5", COMPANY_UNIT)],
    ),
    "same_fact_inflated_number": (
        "adversarial_same_fact_inflated_number",
        OLD,
        [("needs_check", "600", INFLATED)],
    ),
}


@pytest.mark.parametrize("case", list(ADVERSARIAL_V2), ids=list(ADVERSARIAL_V2))
def test_adapt_answer_v2_adversarial_flagged_and_approval_blocked(d: AdaptDesk, case: str):
    fixture, old_answer, expected = ADVERSARIAL_V2[case]
    d.ready_to_adapt(old_answer)

    d.adapt(fixture)

    (version,) = d.versions()
    assert d.flag_triples(version["id"]) == expected
    assert d.answer().approved is False
    with pytest.raises(ApprovalBlocked) as blocked:
        d.service.approve_adapted(d.aid, d.qid, version["id"])
    assert blocked.value.message == "1 paragraph needs your check."
    assert d.version_row(version["id"])["status"] == "draft"


def test_a_name_a_generalized_fact_states_is_flagged_even_when_the_old_answer_uses_it(
    d: AdaptDesk,
):
    d.r.facts.create(FactInput(text=GENERALIZED_FACT, privacy="generalize"))
    assert d.fact(6)["privacy"] == "generalize"
    d.ready_to_adapt(GENERALIZED_OLD)

    d.adapt("leak_generalized_name")

    (request,) = d.requests()
    assert "F6@1 (generalize names)" in request.user + request.system
    assert "Fernhill Bank" in block_of(request.user, "old_answer")
    (version,) = d.versions()
    assert d.flag_triples(version["id"]) == [
        ("needs_check", "Fernhill Bank", "I ran the Fernhill Bank payments migration.")
    ]
    with pytest.raises(ApprovalBlocked):
        d.service.approve_adapted(d.aid, d.qid, version["id"])


def test_known_gap_embellishment_and_employment_claims_are_not_flagged_by_the_checker(
    d: AdaptDesk,
):
    """KNOWN GAP, pinned so it is not forgotten. ``check_units`` compares numbers, names and
    skills with the cited facts. It cannot see "senior", "led" where the fact says "planned", or
    "I worked at <the target company>" (the target's name is an allowed name source). The prompt
    forbids all three and the person reads every word before approving, but no code stops
    them. When the checker learns to flag them, this test must fail and be replaced by a
    flagged-and-blocked case in ``ADVERSARIAL_V2``."""
    d.ready_to_adapt()

    d.adapt("adversarial_embellishment")
    (version,) = d.versions()

    assert d.flag_triples(version["id"]) == []
    assert d.unit_with(version["id"], EMBELLISHED)["cited"] == ["F1@1"]

    d.adapt(data={"units": [{
        "text": "I worked at Other Fictional Co planning delivery routes.",
        "fact_ids": ["F1@1"],
        "added_details": [],
    }]})  # fmt: skip

    assert d.flag_triples(d.versions()[1]["id"]) == []
