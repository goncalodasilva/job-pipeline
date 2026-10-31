"""M7.9 entry points into a session: the gap check, interview events, the rejection follow-up, the
capture inbox and quality hints (AC-M7.1 capture notes raise and expiry; scope "entry points from
applications, interviews and rejections" and "capture inbox expansion"; design "Quality hints").

The assumed markup, routes and rules are in ``tests/entry_point_pages.py``. A real migrated SQLite
database in a temp folder, the fake provider through the spy, the frozen clock, no network; every
value is fictional.
"""

import json
from datetime import timedelta
from typing import Any

import pytest

from app.domain.facts import FactType
from app.domain.fit import Requirement
from app.modules.ai.providers.fake import ScriptedResponse
from app.services.facts import FactInput
from app.services.sessions import SessionEmpty, SessionFocus
from tests.board_pages import elements
from tests.entry_point_pages import (
    CONTINUE,
    ENGLISH,
    FACT_NO_NUMBER,
    FEEDBACK_MARKER,
    HINT_TEXTS,
    JOB_ID,
    LABELS,
    NEVER_USE_NO_NUMBER,
    NOTE_TEXT,
    OTHER_NOTE,
    PAST,
    ROUTES,
    SLOT_PREFIX,
    STORY_COMPLETE,
    STORY_NO_ACTION,
    STORY_NO_RESULT,
    TERRAFORM,
    UPCOMING,
    forms_of,
    hints_in,
    no_offer,
    offers,
    one_offer,
    read_offer,
    settle,
    submit,
)
from tests.facts_page import form_values, posts_to
from tests.integration.test_board import move, post_raw, snapshot, version_of
from tests.integration.test_inbox import get, post
from tests.integration.test_session_suggestion import save_sessions
from tests.reminder_desk import add_event
from tests.tailoring_ai_inputs import REQUIREMENTS
from tests.tailoring_pages import GAP_CHECK, Tailoring, seed_summary, tailoring_app
from tests.today_suggestion_pages import make_seeder, seed_backlog

pytestmark = pytest.mark.integration

ENRICHMENT_QUESTION = "enrichment_question"
FOREIGN = "http://evil.example"


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


# --- helpers -----------------------------------------------------------------------------------


def run_of(tw: Tailoring) -> Any:
    return tw.r.run


def sessions(tw: Tailoring) -> list[dict[str, Any]]:
    return tw.rows("SELECT * FROM sessions ORDER BY created_at, id")


def question_requests(tw: Tailoring) -> list[Any]:
    return tw.requests(ENRICHMENT_QUESTION)


def verdicts_data(*rows: tuple[str, str, tuple[str, ...]]) -> ScriptedResponse:
    """A scripted gap check answer: (requirement id, verdict, cited fact references)."""
    return ScriptedResponse(
        data={
            "requirements": [
                {"requirement_id": rid, "verdict": verdict, "fact_ids": list(cited)}
                for rid, verdict, cited in rows
            ],
            "framing_ideas": [],
        }
    )


def check_ok(tw: Tailoring, aid: str) -> None:
    """The fixture check (missing and partial requirements), a minute after the last thing: the
    clock is frozen, so without this two checks would share a time and "latest" be ambiguous."""
    run_of(tw).clock.advance(timedelta(minutes=1))
    tw.check(aid, "ok")


def check_with(tw: Tailoring, aid: str, answer: ScriptedResponse) -> None:
    run_of(tw).clock.advance(timedelta(minutes=1))
    tw.spy.script(GAP_CHECK, answer)
    started = tw.post(aid, 1, "check")
    assert started.status_code == 200, started.text[:300]
    tw.ended(GAP_CHECK, aid)


def started(response: Any) -> str:
    """The session id an accepted start redirects to."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    target = response.headers["HX-Redirect"]
    assert target.startswith("/sessions/"), target
    return target.removeprefix("/sessions/")


def note_ids(tw: Tailoring) -> list[str]:
    return [row["id"] for row in tw.rows("SELECT id FROM capture_notes ORDER BY rowid")]


def add_note(tw: Tailoring, text: str) -> str:
    response = tw.r.post("/facts/capture", {"text": text}, htmx=True)
    assert response.status_code == 200, response.text[:300]
    return note_ids(tw)[-1]


def story(tw: Tailoring, parts: tuple[str, str, str], **fields: Any) -> Any:
    situation, action, result = parts
    return tw.r.facts.create(
        FactInput(
            type=FactType.STORY,
            situation=situation,
            action=action or None,
            result=result or None,
            **fields,
        )
    )


def library(tw: Tailoring) -> str:
    response = tw.r.client.get("/facts", params={"tab": "library"})
    assert response.status_code == 200, response.text[:300]
    return response.text


def card(html: str, fact_id: str) -> Any:
    return by_id_or_fail(html, f"fact-{fact_id}")


def by_id_or_fail(html: str, element_id: str) -> Any:
    found = [e for e in elements(html) if e.attrs.get("id") == element_id]
    assert len(found) == 1, f"expected one #{element_id}, found {len(found)}"
    return found[0]


# --- gap check ---------------------------------------------------------------------------------


def test_gap_check_offers_session_on_missing_requirements(tw: Tailoring):
    app = tw.ready()
    quiet = snapshot(tw.r)

    # Before a check there is nothing to offer, and looking writes nothing.
    no_offer(tw.page(app.id, 1).text)
    no_offer(tw.fragment(app.id, 1).text)
    assert snapshot(tw.r) == quiet

    # The stored result has missing and partial requirements: one offer for the step, on the page
    # and in the fragment, aimed at this application's job.
    check_ok(tw, app.id)
    after_check = snapshot(tw.r)
    for shown in (tw.page(app.id, 1), tw.fragment(app.id, 1)):
        assert shown.status_code == 200
        offer = one_offer(shown.text, "gap")
        assert offer.focus == f"job:{JOB_ID}"
        assert offer.form.within(by_id_or_fail(shown.text, "tailoring"))
        assert LABELS["gap"] in offer.button.text
    assert snapshot(tw.r) == after_check, "showing the offer writes nothing"
    assert sessions(tw) == [] and question_requests(tw) == []

    # The offered length is the Sessions setting's default.
    save_sessions(run_of(tw), length=5)
    assert one_offer(tw.fragment(app.id, 1).text, "gap", length="5").values["length"] == "5"
    save_sessions(run_of(tw), length=3)

    # A partial requirement alone is enough; every requirement met is not.
    met = {"req-1": ("F1@1",), "req-2": ("F2@1",), "req-3": ("F3@1",), "req-4": ("F1@1",)}
    check_with(tw, app.id, verdicts_data(*((r, "met", c) for r, c in met.items())))
    no_offer(tw.fragment(app.id, 1).text)
    rows = [(r, "partial" if r == "req-4" else "met", c) for r, c in met.items()]
    check_with(tw, app.id, verdicts_data(*rows))
    assert one_offer(tw.fragment(app.id, 1).text, "gap").focus == f"job:{JOB_ID}"

    # Start from the latest check with missing requirements: the job's clusters with no evidence
    # come first, so the first question is about the missing Terraform requirement.
    check_ok(tw, app.id)
    offer = one_offer(tw.page(app.id, 1).text, "gap")
    session_id = started(submit(run_of(tw), offer))
    settle(run_of(tw))
    (row,) = sessions(tw)
    assert row["id"] == session_id
    assert (row["focus_kind"], row["focus_ref"], row["job_id"], row["length"]) == (
        "job",
        JOB_ID,
        JOB_ID,
        3,
    )
    (request,) = question_requests(tw)
    assert TERRAFORM in request.user
    assert ROUTES not in request.user and ENGLISH not in request.user


def test_gap_check_offer_absent_while_a_check_runs(tw: Tailoring):
    app = tw.ready()
    check_ok(tw, app.id)
    run_of(tw).hold.hold = True
    try:
        tw.spy.script(GAP_CHECK, ScriptedResponse(data={"requirements": [], "framing_ideas": []}))
        assert tw.post(app.id, 1, "check").status_code == 200
        shown = tw.fragment(app.id, 1)
        assert 'role="status"' in shown.text
        no_offer(shown.text)
    finally:
        run_of(tw).hold.open()
        settle(run_of(tw))


# --- interviews --------------------------------------------------------------------------------


def seed_events(tw: Tailoring, job_id: str = "interview-job") -> Any:
    app = tw.ready(job_id)
    db = tw.db
    add_event(db, "event-upcoming", app.id, "interview", UPCOMING)
    add_event(db, "event-past", app.id, "interview", PAST)
    add_event(db, "event-follow", app.id, "follow_up", UPCOMING)
    return app


def timeline_events(html: str) -> list[Any]:
    return [
        e for e in elements(html) if e.tag == "li" and e.attrs.get("data-timeline-kind") == "event"
    ]


def test_interview_event_offers_session_for_its_job(tw: Tailoring):
    app = seed_events(tw)
    quiet = snapshot(tw.r)
    drawer = get(tw.r, f"/applications/{app.id}/detail")
    page = tw.r.client.get(f"/applications/{app.id}")
    assert drawer.status_code == 200 and page.status_code == 200
    for html in (drawer.text, page.text):
        assert len(timeline_events(html)) == 3
        found = offers(html, "interview")
        # One for each interview, upcoming or past, and none for the follow-up.
        assert {o.slot_id for o in found} == {
            SLOT_PREFIX + "interview-event-upcoming",
            SLOT_PREFIX + "interview-event-past",
        }
        assert {o.focus for o in found} == {"job:interview-job"}
        items = timeline_events(html)
        for offer in found:
            holders = [item for item in items if offer.form.within(item)]
            assert len(holders) == 1 and offer.slot.within(holders[0])
        for other in ("gap", "rejection", "note", "hint"):
            assert forms_of(elements(html), other) == []
    assert snapshot(tw.r) == quiet, "showing the offers writes nothing"
    assert sessions(tw) == [] and question_requests(tw) == []

    # Start from the upcoming interview: a session about the job.
    offer = next(
        o for o in offers(drawer.text, "interview") if o.slot_id.endswith("event-upcoming")
    )
    session_id = started(submit(run_of(tw), offer))
    settle(run_of(tw))
    (row,) = sessions(tw)
    assert (row["id"], row["focus_kind"], row["focus_ref"]) == (
        session_id,
        "job",
        "interview-job",
    )
    assert len(question_requests(tw)) == 1


def test_cancelled_interview_event_has_no_offer(tw: Tailoring):
    app = tw.ready("interview-job")
    add_event(tw.db, "event-live", app.id, "interview", UPCOMING)
    add_event(tw.db, "event-cancelled", app.id, "interview", UPCOMING, status="cancelled")
    for html in (
        get(tw.r, f"/applications/{app.id}/detail").text,
        tw.r.client.get(f"/applications/{app.id}").text,
    ):
        assert len(timeline_events(html)) == 2
        found = offers(html, "interview")
        assert [o.slot_id for o in found] == [SLOT_PREFIX + "interview-event-live"]
        assert "event-cancelled" not in " ".join(
            str(e.attrs.get("id", "")) for e in elements(html) if e.tag in ("form", "div", "button")
        )


def test_interview_offer_not_on_applications_without_interviews(tw: Tailoring):
    app = tw.ready()
    add_event(tw.db, "event-deadline", app.id, "deadline", UPCOMING)
    for html in (
        get(tw.r, f"/applications/{app.id}/detail").text,
        tw.r.client.get(f"/applications/{app.id}").text,
    ):
        assert len(timeline_events(html)) == 1
        no_offer(html, "interview")


# --- rejections --------------------------------------------------------------------------------


def peer_job(tw: Tailoring) -> None:
    """A second saved job asking for the same requirements in the same words, so the rejected
    job's clusters still have a saved job behind them (ADR-117 item 4)."""
    tw._seeder.job("peer-job", "Route Planner", "Harbor Kite Fictional")
    peer = tuple(Requirement(f"peer-{r.id}", r.kind, r.text) for r in REQUIREMENTS)
    seed_summary(tw.db, "peer-job", peer, summary_id="summary-peer-job")


def dialog_of(html: str) -> Any:
    found = [e for e in elements(html) if e.tag == "dialog"]
    assert len(found) == 1, f"expected one dialog, found {len(found)}"
    return found[0]


def test_rejection_follow_up_offers_session_and_sends_no_feedback_text(tw: Tailoring):
    app = tw.ready("rejected-job")
    peer_job(tw)
    moved = move(tw.r, app, "rejected")
    assert moved.status_code == 200

    # The offer sits in the feedback dialog, next to the feedback form and not inside it.
    dialog = dialog_of(moved.text)
    feedback = by_id_or_fail(moved.text, "feedback-form")
    offer = one_offer(moved.text, "rejection", dialog)
    assert not offer.form.within(feedback) and offer.focus == "job:rejected-job"
    assert LABELS["rejection"] in offer.button.text

    # The feedback is kept with the application. It is not a field of the offer.
    saved = post(tw.r, f"/applications/{app.id}/feedback", text=FEEDBACK_MARKER)
    assert saved.status_code == 200
    (note,) = tw.rows("SELECT kind, text FROM application_notes")
    assert (note["kind"], note["text"]) == ("feedback", FEEDBACK_MARKER)
    assert FEEDBACK_MARKER not in json.dumps(offer.values)

    # Starting from the offer: a session about the role's requirements, which other saved jobs
    # still ask for. Nothing the user wrote as feedback reaches the AI, a task or a session row.
    session_id = started(submit(run_of(tw), offer))
    settle(run_of(tw))
    (row,) = sessions(tw)
    assert (row["id"], row["focus_kind"], row["focus_ref"]) == (
        session_id,
        "job",
        "rejected-job",
    )
    requests = question_requests(tw)
    assert len(requests) == 1
    assert FEEDBACK_MARKER not in repr(tw.spy.requests)
    for table, content in snapshot(tw.r).items():
        if table != "application_notes":
            assert FEEDBACK_MARKER not in json.dumps(content, default=str), table
    for focus in (SessionFocus("job", "rejected-job"), SessionFocus()):
        assert FEEDBACK_MARKER not in repr(run_of(tw).app.state.sessions.backlog(focus))


def test_rejection_session_scope_is_the_live_clusters_sharing_requirements(tw: Tailoring):
    """A peer saved job that asks for only SOME of the rejected job's requirements keeps only
    those clusters alive: the session is about exactly them (ADR-117 item 4), not the rest of the
    rejected job's requirements and not the peer's unrelated ones."""
    app = tw.ready("rejected-job")  # asks for ROUTES, TERRAFORM, ENGLISH and SAP
    tw._seeder.job("partial-peer", "Route Planner", "Harbor Kite Fictional")
    seed_summary(
        tw.db,
        "partial-peer",
        (
            Requirement("partial-r1", "must_have", TERRAFORM),
            Requirement("partial-r2", "key", ENGLISH),
            Requirement("partial-r3", "nice", "Keeping bees on a rooftop"),
        ),
        summary_id="summary-partial-peer",
    )
    moved = move(tw.r, app, "rejected")
    offer = one_offer(moved.text, "rejection")

    def requirements(items: Any) -> set[str]:
        return {text for item in items for text in json.loads(item.payload)["requirements"]}

    items = run_of(tw).app.state.sessions.backlog(SessionFocus("job", "rejected-job"))
    assert items and all(i.item.kind == "cluster" for i in items)
    assert requirements(items) == {TERRAFORM, ENGLISH}

    # The offer starts a session whose question is about those clusters only.
    started(submit(run_of(tw), offer))
    settle(run_of(tw))
    (request,) = question_requests(tw)
    assert TERRAFORM in request.user or ENGLISH in request.user
    assert ROUTES not in request.user and "bees" not in request.user


def test_rejection_offer_is_a_separate_block_that_warns_to_save_the_feedback_first(
    tw: Tailoring,
):
    """The offer is not a third dialog action beside Save and Skip, and it says in plain words
    that starting a session leaves the page (an htmx redirect drops unsaved feedback)."""
    app = tw.ready("rejected-job")
    moved = move(tw.r, app, "rejected")
    parsed = elements(moved.text)
    dialog = dialog_of(moved.text)
    offer = one_offer(moved.text, "rejection", dialog)
    actions = [e for e in parsed if "modal-actions" in str(e.attrs.get("class", "")).split()]
    assert actions and not any(offer.form.within(a) for a in actions), "not beside Save and Skip"
    (block,) = [
        e
        for e in parsed
        if "session-offer-block" in str(e.attrs.get("class", "")).split() and offer.form.within(e)
    ]
    assert block.within(dialog) and not block.within(by_id_or_fail(moved.text, "feedback-form"))
    (note,) = [e for e in parsed if e.within(block) and e.tag == "p"]
    assert note.text == "Save your feedback first. Starting a session leaves this page."
    assert offer.button.attrs["aria-describedby"] == note.attrs["id"]
    # The feedback text is still not a field of the offer.
    assert set(offer.values) == {"focus", "length"}


def test_rejection_offer_in_drawer_and_not_in_interview_dialog(tw: Tailoring):
    rejected = tw.ready("rejected-job")
    other = tw.another_application("interviewing-job")

    # Moved from the drawer or page: the dialog is rendered inside the detail, same offer.
    moved = post_raw(
        tw.r,
        f"/applications/{rejected.id}/status",
        {"version": str(version_of(tw.r, rejected.id)), "status": "rejected", "source": "menu"},
        **{"HX-Target": "application-detail"},
    )
    assert moved.status_code == 200
    dialog = dialog_of(moved.text)
    offer = one_offer(moved.text, "rejection", dialog)
    assert offer.form.within(by_id_or_fail(moved.text, "application-detail"))
    assert not offer.form.within(by_id_or_fail(moved.text, "feedback-form"))

    # The interview-date dialog is not a rejection: no offer.
    interviewing = move(tw.r, other, "interviewing")
    assert dialog_of(interviewing.text) is not None
    no_offer(interviewing.text)


def test_rejection_offer_with_nothing_left_to_ask_is_a_reason_in_its_slot(tw: Tailoring):
    """No other saved job asks for the rejected job's requirements, so the clusters have expired
    (ADR-117 item 4): the start is refused with the reason in the offer's own slot."""
    app = tw.ready("lonely-job")
    moved = move(tw.r, app, "rejected")
    offer = one_offer(moved.text, "rejection")
    answer = submit(run_of(tw), offer)
    assert answer.status_code == 409
    assert "<html" not in answer.text and "Nothing to ask right now" in answer.text
    assert [e for e in elements(answer.text) if e.attrs.get("id") == offer.slot_id]
    assert sessions(tw) == [] and question_requests(tw) == []


# --- capture notes -----------------------------------------------------------------------------


def capture_page(tw: Tailoring) -> str:
    response = tw.r.client.get("/facts", params={"tab": "capture"})
    assert response.status_code == 200, response.text[:300]
    return response.text


def test_capture_note_expand_starts_session_on_note(tw: Tailoring):
    first = add_note(tw, NOTE_TEXT)
    second = add_note(tw, OTHER_NOTE)
    quiet = snapshot(tw.r)
    html = capture_page(tw)
    found = {o.focus: o for o in offers(html, "note")}
    assert set(found) == {f"capture_note:{first}", f"capture_note:{second}"}
    parsed = elements(html)
    for note_id, offer in (
        (first, found[f"capture_note:{first}"]),
        (second, found[f"capture_note:{second}"]),
    ):
        item = by_id_or_fail(html, f"note-{note_id}")
        assert offer.form.within(item) and offer.slot.within(item)
        assert offer.slot_id == f"{SLOT_PREFIX}note-{note_id}"
        assert offer.button.attrs["aria-describedby"] == f"capture-text-{note_id}"
        # Beside the existing actions, which stay.
        labels = {e.text for e in parsed if e.within(item) and e.tag in ("a", "button")}
        assert {"Turn into a fact", "Delete", LABELS["note"]} <= labels
    assert snapshot(tw.r) == quiet, "showing the offers writes nothing"

    # Every answer that swaps the inbox carries the offers (here: an added note and a delete).
    answer = tw.r.post("/facts/capture", {"text": "A third fictional note."}, htmx=True)
    assert len(offers(answer.text, "note")) == 3
    third = note_ids(tw)[-1]
    deleted = tw.r.post(f"/facts/capture/{third}/delete", htmx=True)
    assert {o.focus for o in offers(deleted.text, "note")} == set(found)

    # Start on the first note: the session is focused on it and its first question is about it.
    session_id = started(submit(run_of(tw), found[f"capture_note:{first}"]))
    settle(run_of(tw))
    (row,) = sessions(tw)
    assert (row["id"], row["focus_kind"], row["focus_ref"]) == (session_id, "capture_note", first)
    (stored,) = tw.rows("SELECT capture_note_id, kind FROM enrichment_questions")
    assert (stored["kind"], stored["capture_note_id"]) == ("capture_note", first)
    (request,) = question_requests(tw)
    assert NOTE_TEXT in request.user and OTHER_NOTE not in request.user


def test_capture_note_without_notes_or_after_delete_has_no_offer(tw: Tailoring):
    no_offer(capture_page(tw), "note")
    note = add_note(tw, NOTE_TEXT)
    assert len(offers(capture_page(tw), "note")) == 1
    deleted = tw.r.post(f"/facts/capture/{note}/delete", htmx=True)
    no_offer(deleted.text, "note")
    restored = tw.r.post(f"/facts/capture/{note}/undo-delete", htmx=True)
    assert [o.focus for o in offers(restored.text, "note")] == [f"capture_note:{note}"]


# --- AC-M7.1: backlog priority and expiry -------------------------------------------------------


def keys(tw: Tailoring, focus: SessionFocus | None = None) -> list[str]:
    return [item.item.key for item in run_of(tw).app.state.sessions.backlog(focus)]


def test_ac_m7_1_capture_notes_raise_backlog_priority(tw: Tailoring):
    seed_backlog(make_seeder(run_of(tw).app, run_of(tw).clock))
    before = run_of(tw).app.state.sessions.backlog()
    assert before and all(i.item.kind == "cluster" for i in before)
    top_cluster = before[0].item.key

    # A note added in the inbox joins the backlog and goes ahead of the clusters.
    note = add_note(tw, NOTE_TEXT)
    after = keys(tw)
    assert after[0] == f"capture_note:{note}"
    assert after[1:] == [i.item.key for i in before], "the rest keeps its order"
    assert top_cluster in after

    # Reading the backlog writes nothing.
    quiet = snapshot(tw.r)
    keys(tw)
    assert snapshot(tw.r) == quiet

    # A deleted note leaves it; an undone delete brings it back; a note is one item each.
    assert tw.r.post(f"/facts/capture/{note}/delete", htmx=True).status_code == 200
    assert keys(tw) == [i.item.key for i in before]
    assert tw.r.post(f"/facts/capture/{note}/undo-delete", htmx=True).status_code == 200
    assert keys(tw)[0] == f"capture_note:{note}"
    other = add_note(tw, OTHER_NOTE)
    assert set(keys(tw)[:2]) == {f"capture_note:{note}", f"capture_note:{other}"}

    # A note outranks a quality hint too (a confirmed fact with nothing to add a number to).
    tw.r.typed(FACT_NO_NUMBER)
    ranked = keys(tw)
    hint = next(k for k in ranked if k.startswith("quality_hint:"))
    assert ranked.index(f"capture_note:{note}") < ranked.index(hint)


def test_ac_m7_1_related_jobs_removed_or_closed_expire_questions(tw: Tailoring):
    seeder = tw._seeder
    seeder.job("expire-saved", "Route Planner", "Harbor Kite Fictional")
    seed_summary(
        tw.db,
        "expire-saved",
        (Requirement("expire-r1", "must_have", ROUTES),),
        summary_id="summary-expire-saved",
    )
    active = seeder.application("expire-active", "Depot Supervisor", None, "applied")
    seed_summary(
        tw.db,
        "expire-active",
        (Requirement("expire-r2", "must_have", TERRAFORM),),
        summary_id="summary-expire-active",
    )
    note = add_note(tw, NOTE_TEXT)
    hint_fact = tw.r.typed(FACT_NO_NUMBER)
    service = run_of(tw).app.state.sessions
    labels = {i.label for i in service.backlog()}
    assert {ROUTES, TERRAFORM} <= labels
    assert {f"capture_note:{note}", f"quality_hint:{hint_fact.id}"} <= set(keys(tw))

    # A stored question exists for the saved job's cluster: asked and skipped (a cooldown).
    cluster = next(i for i in service.backlog() if i.label == ROUTES)
    made = service.start(SessionFocus("cluster", cluster.cluster_id), 3)
    run_of(tw).wait_terminal(made.task.id)
    view = service.view(made.session_id)
    answer = view.answers[0]
    service.outcome(
        made.session_id,
        answer.id,
        "skipped",
        expected_version=answer.version,
        session_version=view.session.version,
    )
    service.end(made.session_id, expected_version=service.view(made.session_id).session.version)
    assert len(tw.rows("SELECT id FROM enrichment_questions WHERE kind='cluster'")) == 1

    # Removing the saved job expires its cluster on the next read, cooldown or not.
    tw.r.run.execute("UPDATE jobs SET triage_state='removed' WHERE id='expire-saved'")
    assert ROUTES not in {i.label for i in service.backlog()}
    assert TERRAFORM in {i.label for i in service.backlog()}

    # Closing the application of the other job expires its cluster the same way.
    assert move(tw.r, active, "rejected").status_code == 200
    assert TERRAFORM not in {i.label for i in service.backlog()}

    # The note and the fact expire when they are no longer usable; nothing is then left to ask.
    assert tw.r.post(f"/facts/capture/{note}/delete", htmx=True).status_code == 200
    tw.r.run.execute("UPDATE facts SET privacy='never_use' WHERE id=?", (hint_fact.id,))
    assert service.backlog() == ()
    with pytest.raises(SessionEmpty):
        service.start(SessionFocus(), 3)

    # Reads wrote nothing: the stored question rows are as the skip left them (still open).
    assert {r["status"] for r in tw.rows("SELECT status FROM enrichment_questions")} == {"open"}

    # Demand that returns makes the item eligible again.
    tw.r.run.execute("UPDATE jobs SET triage_state='new' WHERE id='expire-saved'")
    assert ROUTES in {i.label for i in service.backlog()}


# --- quality hints -----------------------------------------------------------------------------


def seed_hint_facts(tw: Tailoring) -> dict[str, str]:
    from app.services.facts import DraftInput, DraftOrigin

    made = {
        "no_result": story(tw, STORY_NO_RESULT).id,
        "no_number": tw.r.typed(FACT_NO_NUMBER).id,
        "no_action": story(tw, STORY_NO_ACTION).id,
        "two": story(tw, ("The depot had no roster.", "Built a roster.", "")).id,
        "complete": story(tw, STORY_COMPLETE).id,
        "never_use": tw.r.typed(NEVER_USE_NO_NUMBER, privacy="never_use").id,
    }
    origin = DraftOrigin(source="cv_import", provider="fake", model="fake-thorough")
    (draft,) = tw.r.facts.create_drafts(
        origin, [DraftInput(text="A drafted note with no figures.", original_answer="x")]
    )
    made["draft"] = draft.id
    return made


EXPECTED_HINTS = {
    "no_result": ["Add a result?"],
    "no_number": ["Add a number?"],
    "no_action": ["Add what you did?"],
    "two": ["Add a result?", "Add a number?"],
    "complete": [],
    "never_use": [],
}


def test_quality_hints_shown_non_blocking_on_library_and_editor(tw: Tailoring):
    made = seed_hint_facts(tw)
    quiet = snapshot(tw.r)
    html = library(tw)
    fragment = tw.r.client.get("/facts/library", headers=run_of(tw).run.htmx()).text
    for text in (html, fragment):
        for name, expected in EXPECTED_HINTS.items():
            shown = card(text, made[name])
            assert hints_in(text, shown) == expected, name
            found = offers(text, "hint", shown)
            if expected:
                (offer,) = found
                assert offer.focus == f"fact:{made[name]}"
                assert offer.slot_id == f"{SLOT_PREFIX}hint-{made[name]}"
                assert offer.slot.within(shown)
            else:
                assert found == [], name
        everywhere = [e.text for e in elements(text) if "data-quality-hint" in e.attrs]
        assert set(everywhere) <= set(HINT_TEXTS)
    assert snapshot(tw.r) == quiet, "hints are read-only: showing them writes nothing"
    for hint in (e for e in elements(html) if "data-quality-hint" in e.attrs):
        assert hint.attrs.get("role") != "alert" and "aria-invalid" not in hint.attrs

    # The editor: the saved wording's hints, outside the form, and the form stays usable.
    for name, expected in EXPECTED_HINTS.items():
        page = tw.r.client.get(f"/facts/{made[name]}/edit")
        assert page.status_code == 200
        scope = by_id_or_fail(page.text, "fact-editor-page")
        form = by_id_or_fail(page.text, "fact-editor")
        assert hints_in(page.text, scope) == expected, name
        found = offers(page.text, "hint", scope)
        assert len(found) == (1 if expected else 0), name
        if found:
            assert found[0].focus == f"fact:{made[name]}"
            assert not found[0].form.within(form), "no form inside the editor's form"
            assert all(
                not h.within(form) for h in elements(page.text) if "data-quality-hint" in h.attrs
            )
            blockers = [
                e
                for e in elements(page.text)
                if e.within(form)
                and ("disabled" in e.attrs or e.attrs.get("aria-disabled") == "true")
                and e.tag in ("button", "input", "textarea", "select")
            ]
            assert blockers == [], "a hint does not block the form"
    for name in ("draft",):
        page = tw.r.client.get(f"/facts/{made[name]}/edit")
        assert page.status_code == 200 and hints_in(page.text, elements(page.text)[0]) == []
        no_offer(page.text, "hint")

    # Saving through the editor works, and the answer drops the hint that no longer applies.
    page = tw.r.client.get(f"/facts/{made['no_result']}/edit")
    parsed = elements(page.text)
    form = by_id_or_fail(page.text, "fact-editor")
    values = dict(form_values(parsed, form))
    values["result"] = "Cut manual steps by half."
    saved = tw.r.client.post(
        posts_to(form), data=values, headers=run_of(tw).run.action(**{"HX-Request": "true"})
    )
    assert saved.status_code == 200, saved.text[:300]
    scope = by_id_or_fail(saved.text, "fact-editor-page")
    assert "Add a result?" not in hints_in(saved.text, scope)
    assert offers(saved.text, "hint", scope) == []


def count_reads(monkeypatch) -> dict[str, int]:
    """Count the Sessions setting loads and the write transactions a request takes."""
    from app.modules.storage import Database
    from app.services.session_preferences import SessionPreferences

    seen = {"loads": 0, "write_transactions": 0}
    load, transaction = SessionPreferences.load, Database.transaction

    def counting_load(self: Any) -> Any:
        seen["loads"] += 1
        return load(self)

    def counting_transaction(self: Any) -> Any:
        seen["write_transactions"] += 1
        return transaction(self)

    monkeypatch.setattr(SessionPreferences, "load", counting_load)
    monkeypatch.setattr(Database, "transaction", counting_transaction)
    return seen


def test_offers_read_the_default_length_once_per_page_and_take_no_write_lock(
    tw: Tailoring, monkeypatch
):
    """Many offers on one page cost one read of the setting (not one per card), and showing them
    takes no write lock (M7.9 review)."""
    names = "abcdefghijklmnopqrst"  # no digits: a figure would drop the "Add a number?" hint
    for name in names:
        tw.r.typed(f"Planned fictional delivery routes for harbor depot {name * 3}.")
    for name in names[:5]:
        add_note(tw, f"Fictional capture note {name * 3} about a depot task.")
    seen = count_reads(monkeypatch)
    for url in ("/facts/library", "/facts?tab=capture", "/facts?tab=library"):
        seen.update(loads=0, write_transactions=0)
        response = tw.r.client.get(url, headers=run_of(tw).run.htmx())
        assert response.status_code == 200, url
        assert len(offers(response.text, "hint" if "library" in url else "note")) >= 5, url
        assert seen["loads"] <= 1, (url, seen)
        assert seen["write_transactions"] == 0, (url, seen)


def test_hint_button_is_described_by_the_fact_wording_then_its_hints(tw: Tailoring):
    made = seed_hint_facts(tw)
    editor = tw.r.client.get(f"/facts/{made['two']}/edit").text
    for html, scope in (
        (library(tw), "fact-" + made["two"]),
        (editor, "fact-editor-page"),
    ):
        holder = by_id_or_fail(html, scope)
        (offer,) = offers(html, "hint", holder)
        ids = offer.button.attrs["aria-describedby"].split()
        parsed = elements(html)
        hints = [e for e in parsed if e.within(holder) and "data-quality-hint" in e.attrs]
        assert [h.text for h in hints] == ["Add a result?", "Add a number?"]
        assert ids[1:] == [h.attrs["id"] for h in hints], "the wording first, then each hint"
        assert len(ids) == 3 and len(set(ids)) == 3


# --- tokens, origin and refusals ---------------------------------------------------------------


def all_entry_points(tw: Tailoring) -> dict[str, Any]:
    """One offer of every kind from one workspace, read from the pages that show them."""
    app = tw.ready("entry-job")
    check_ok(tw, app.id)
    add_event(tw.db, "event-entry", app.id, "interview", UPCOMING)
    note = add_note(tw, NOTE_TEXT)
    hint = tw.r.typed(FACT_NO_NUMBER)
    rejected = tw.another_application("entry-rejected-job")
    # The rejected role asks for what the live job asks for, so the session has clusters to use.
    seed_summary(
        tw.db,
        "entry-rejected-job",
        tuple(Requirement(f"rej-{r.id}", r.kind, r.text) for r in REQUIREMENTS),
        summary_id="summary-entry-rejected-job",
    )
    found = {
        "gap": one_offer(tw.page(app.id, 1).text, "gap"),
        "interview": one_offer(get(tw.r, f"/applications/{app.id}/detail").text, "interview"),
        "rejection": one_offer(move(tw.r, rejected, "rejected").text, "rejection"),
        "note": one_offer(
            capture_page(tw), "note", by_id_or_fail(capture_page(tw), f"note-{note}")
        ),
        "hint": one_offer(library(tw), "hint", card(library(tw), hint.id)),
    }
    assert set(found) == set(LABELS)
    return found


def test_entry_point_posts_need_token_and_origin(tw: Tailoring):
    found = all_entry_points(tw)
    run = run_of(tw)
    before = snapshot(tw.r)
    tasks_before = tw.rows("SELECT id FROM tasks")
    requests_before = len(tw.spy.requests)
    token = run.run.with_token
    for place, offer in found.items():
        attempts = {
            "nothing": {},
            "token only": token(),
            "token, htmx, no origin": token(**{"HX-Request": "true", "HX-Target": offer.slot_id}),
            "foreign origin": token(origin=FOREIGN),
            "foreign origin, htmx": token(
                origin=FOREIGN, **{"HX-Request": "true", "HX-Target": offer.slot_id}
            ),
            "origin only": {"origin": run.run.action()["origin"]},
        }
        for label, headers in attempts.items():
            response = run.client.post("/sessions", data=offer.values, headers=headers)
            assert response.status_code == 403, (place, label, response.status_code)
    assert snapshot(tw.r) == before, "a refused post changes nothing"
    assert tw.rows("SELECT id FROM tasks") == tasks_before
    assert len(tw.spy.requests) == requests_before and sessions(tw) == []

    # The offers' forms carry no token and no secret: the page's own headers do that.
    for offer in found.values():
        assert run.run.token not in json.dumps(offer.values)

    # With the token and the app's own origin, and no script, a post still works: a redirect.
    plain = submit(run, found["note"], htmx=False)
    assert plain.status_code == 303 and plain.headers["location"].startswith("/sessions/")
    settle(run)
    (row,) = sessions(tw)
    assert row["focus_kind"] == "capture_note"


@pytest.mark.parametrize("why", ["no_provider", "open_session", "not_aimed_at_the_slot"])
def test_entry_point_refusal_shows_only_the_reason_in_its_slot(tw: Tailoring, monkeypatch, why):
    found = all_entry_points(tw)
    run = run_of(tw)
    if why == "no_provider":
        monkeypatch.setattr(run.app.state.sessions, "_settings", lambda: None)
    elif why == "open_session":
        opened = started(submit(run, found["note"]))
        settle(run)
    else:
        monkeypatch.setattr(run.app.state.sessions, "_settings", lambda: None)
    for place, offer in found.items():
        if why == "open_session" and place == "note":
            continue
        before = len(sessions(tw))
        answer = submit(run, offer, target=why != "not_aimed_at_the_slot")
        assert answer.status_code == 409, (place, answer.status_code, answer.text[:200])
        parsed = elements(answer.text)
        if why == "not_aimed_at_the_slot":
            assert "<h1" in answer.text, "without the slot it is the whole screen as before"
            continue
        assert "<html" not in answer.text
        assert not [e for e in parsed if e.tag in ("h1", "h2")], place
        assert not [e for e in parsed if e.tag in ("select", "form")], place
        (slot,) = [e for e in parsed if e.attrs.get("id") == offer.slot_id]
        assert slot.tag == "div", "the answer keeps the slot, so a next refusal has a target"
        assert len([e for e in parsed if "data-autofocus" in e.attrs]) == 1
        if why == "open_session":
            links = [e for e in parsed if e.tag == "a" and e.text == CONTINUE]
            assert [link.attrs["href"] for link in links] == [f"/sessions/{opened}"], place
        assert len(sessions(tw)) == before


def test_entry_point_refusal_budget_dialog_posts_the_forms_own_fields_to_its_slot(
    tw: Tailoring, monkeypatch
):
    from app.services.budget_gate import ShowWarning

    found = all_entry_points(tw)
    run = run_of(tw)
    monkeypatch.setattr(
        run.app.state.budget_gate,
        "check",
        lambda *args, **kwargs: ShowWarning(
            "First warning",
            "Fictional request limit.",
            {
                "budget_service": "fake",
                "budget_threshold": "first",
                "budget_cycle_start": "2026-04-01",
            },
        ),
    )
    for place, offer in found.items():
        answer = submit(run, offer)
        assert answer.status_code == 200, (place, answer.text[:200])
        parsed = elements(answer.text)
        assert not [e for e in parsed if e.tag in ("h1", "h2") and "session" in e.text.lower()]
        (dialog,) = [e for e in parsed if e.tag == "dialog"]
        (form,) = [e for e in parsed if e.tag == "form" and e.within(dialog)]
        assert form.attrs["hx-target"] == f"#{offer.slot_id}"
        fields = form_values(parsed, form)
        assert fields["focus"] == offer.focus and fields["length"] == "3"
        assert fields["budget_service"] == "fake"
        assert [e.attrs["id"] for e in parsed if e.attrs.get("id") == offer.slot_id] == [
            offer.slot_id
        ]
    assert sessions(tw) == []


def test_read_offer_is_strict_about_its_form():
    """The shared reader fails on a form that is missing its slot, so the tests above cannot pass
    on a half-built entry point."""
    html = (
        '<form data-session-offer="note" method="post" action="/sessions" hx-post="/sessions"'
        ' hx-target="#session-refusal-note-x" hx-swap="outerHTML">'
        '<input type="hidden" name="focus" value="capture_note:x">'
        '<input type="hidden" name="length" value="3">'
        f'<button type="submit">{LABELS["note"]}</button></form>'
    )
    parsed = elements(html)
    (form,) = forms_of(parsed, "note")
    with pytest.raises(AssertionError):
        read_offer(parsed, form)
