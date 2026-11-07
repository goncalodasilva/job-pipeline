"""M7.10 cross-cutting checks: AC-M1.4 extended to session text, and the response-time budget of
the session screens (spec 2.9 rule 13 and the M7.6 paragraph, 2.10, 2.11, 3.3, 3.8; ADR-117,
ADR-123, ADR-125, ADR-126).

What is planted, all fictional, in the real flows (a job asking for a labelled requirement, a
session started from the app's own routes, an answer typed by the person, the fake provider's
scripted question, why line and draft):

- an answer marker, typed by the person (kept as the original answer);
- a question marker and a why marker, in the question the provider is scripted to ask;
- a draft marker, in the wording of the draft the provider is scripted to return;
- a coverage label marker, in a job requirement (it is the label of a coverage cluster); a
  session focused on that cluster is run with the model's question forced to the code-owned
  fallback and its structuring forced to fail, so the question row that derives from the label is
  exercised and nothing else may hold it.

They may be stored only in the tables of ``ALLOWED`` below (an explicit allow-list: a marker in
any other table fails) and shown back to the person who typed them; they must never reach a
surface that is only about tasks and diagnostics: every log record as it was made (before
redaction), the log files, the console, the rows of ``tasks``, ``usage_records``,
``provider_usage_snapshots``, ``budget_notices`` and ``settings``, the event stream (headers and
frames), the response headers of the session, coverage and Today routes, the bodies of
``/health`` and the testing tools section, and the error text a
person sees when a question or a structuring request fails. The failures are forced through a
malformed answer and a provider that raises with the text in its detail and in a chained cause,
an unexpected exception and a timeout, for both session tasks, retried through the Tasks panel's
own route; and through an unexpected error while the session page is drawn.

``test_session_scan_catches_a_planted_marker`` is the scan's own control. The session flows read
the same text back to the person in escaped fields, so the response bodies of those pages are not
scanned, only their headers, the status and alert lines, and the diagnostic screens' bodies.

``test_session_errors_carry_no_answer_text`` forces every refusal a session route can give with an
answer marker in the request and scans the message each one shows. A refused form may redisplay
what was typed in its own field (the editor and a budget dialog do), so the scan reads the
messages, headers and plain-text bodies, and a control proves the redisplay is the only place the
text shows.

``test_session_query_strings_not_in_access_log`` runs the real CLI and fetches the session,
coverage and Today routes with a marker in every query value. Nothing of a query string may reach
the log files or the console.

``test_session_drafts_never_use_never_use_facts`` pins spec 3.1 for sessions: text of a never-use
fact reaches no request of either task, a model that repeats it anyway gets a replaced question
and a flagged draft, and a session cannot be aimed at it.

``test_ac_m7_fragments_under_150ms_with_1000_jobs_300_facts`` times the Coverage tab, the session
panel, Today with a suggestion and the start of a session (spec 3.8).

No network, no sleeps, ``FrozenClock`` where the harness uses one.
"""

import html
import json
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest

from app.domain.errors import NetworkUnavailable
from app.domain.facts import FactType
from app.domain.fit import Requirement
from app.modules.ai.providers.fake import ScriptedResponse
from app.modules.storage import Database, jobs_repo
from app.services.applications import ApplicationService, ApplicationStatuses
from app.services.facts import FactInput
from app.services.sessions import SessionEmpty, SessionFocus
from tests.board_pages import Seeder, stamp_after, ticker
from tests.coverage_pages import cluster_id
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_board import seeder
from tests.integration.test_m4_cross_cutting import (
    Surfaces,
    assert_stored_only_in,
    final_states,
    open_events,
    read_until_final,
    tables_containing,
    watched,
)
from tests.integration.test_session_answers import payload
from tests.integration.test_session_pages import post
from tests.integration.test_sessions import current, service
from tests.page_token import TOKEN_HEADER_NAME, page_token
from tests.refine_harness import Refining, refining
from tests.running_app import start_app
from tests.tailoring_pages import seed_summary
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
DEADLINE = 20.0
SEED_INSTANT = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # long before the app's own clock

ANSWER_MARKER = "canaryanswerquokka"  # no digits: a number absent from the answer would be flagged
QUESTION_MARKER = "canaryquestionwombat"
WHY_MARKER = "canarywhyheron"
DRAFT_MARKER = "canarydraftotter"
LABEL_MARKER = "canarylabelcormorant"
MARKERS = (ANSWER_MARKER, QUESTION_MARKER, WHY_MARKER, DRAFT_MARKER, LABEL_MARKER)

# Where each marker may legitimately be stored: the tables each one was observed to reach in the
# flows below, and no others. Every table not named here, whatever it holds, must not have it.
# The full text index of facts keeps its own copy of a draft's text; its shadow tables are named
# by SQLite, so only the two this build writes the text to are allowed.
ALLOWED: dict[str, frozenset[str]] = {
    # the original answer is kept with the answer row and on the draft's version
    ANSWER_MARKER: frozenset({"session_answers", "fact_versions"}),
    # the question the model asked, and the earlier wording the question row remembers
    QUESTION_MARKER: frozenset({"session_answers", "enrichment_questions"}),
    WHY_MARKER: frozenset({"session_answers"}),
    DRAFT_MARKER: frozenset({"fact_versions", "facts_fts", "facts_fts_content"}),
    # A requirement's text is job text. The cluster key it forms is the target of a session about
    # it, so the question row that a coverage-focused session creates holds it (that session is
    # forced to the code-owned question, which carries no label, so no answer row does).
    LABEL_MARKER: frozenset({"job_requirements", "enrichment_questions"}),
}


def settled(run: Any, sid: str, status: str = "succeeded") -> Any:
    """Wait until the session's latest task has ended as ``status``."""

    def ended() -> Any:
        task = service_of(run).view(sid).task
        return task if task is not None and str(task.status.value) in TERMINAL_STATES else None

    task = wait_until(ended, timeout=DEADLINE, message=f"session {sid} task ended")
    assert str(task.status.value) == status, (str(task.status.value), task.error)
    return task


TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")


def service_of(run: Any) -> Any:
    return run.app.state.sessions


def action_headers(run: Any) -> dict[str, str]:
    return run.run.action(**HTMX)


def status_lines(page: str) -> list[str]:
    """What a person is told: the text of every status and alert element, and of the field
    errors (the redisplayed field itself is not a message)."""
    found = re.findall(r'role="(?:status|alert)"[^>]*>([^<]*)<', page)
    found += re.findall(r'class="field-error"[^>]*>([^<]*)<', page)
    found += re.findall(r'id="session-status"[^>]*>([^<]*)<', page)
    return [html.unescape(line) for line in found]


def add_response(surfaces: Surfaces, where: str, response: Any, *, body: bool = False) -> None:
    surfaces.add_headers(where, response)
    if body:
        surfaces.add(f"{where} body", response.text)
    else:
        for line in status_lines(response.text):
            surfaces.add(f"{where} status line", line)


# --- the control -------------------------------------------------------------------------------


def test_session_scan_catches_a_planted_marker() -> None:
    """Planted in a header, a status line, a task column or the settings row, a marker is found;
    a clean run is clean; the redisplayed answer in a field is not a message."""
    surfaces = Surfaces()
    surfaces.add("tasks row 0 column error", f"Failed: {ANSWER_MARKER}")
    surfaces.add("settings row 3 column value", WHY_MARKER)
    assert surfaces.leaks(MARKERS) == [
        f"{ANSWER_MARKER!r} in tasks row 0 column error",
        f"{WHY_MARKER!r} in settings row 3 column value",
    ]
    page = (
        f'<p id="session-status" role="status">Saved {QUESTION_MARKER}</p>'
        f'<p class="field-error">Bad {DRAFT_MARKER}</p>'
        f'<textarea id="session-answer">{ANSWER_MARKER}</textarea>'
    )
    lines = status_lines(page)
    assert any(QUESTION_MARKER in line for line in lines)
    assert any(DRAFT_MARKER in line for line in lines)
    assert not any(ANSWER_MARKER in line for line in lines), "the field is not a message"
    clean = Surfaces()
    clean.add("x", "Take your time. You can skip any question.")
    assert clean.leaks(MARKERS) == []


# --- seeding -----------------------------------------------------------------------------------


def seed_flow(desk: Refining) -> dict[str, str]:
    """A saved job asking for a labelled requirement and for route planning, and two confirmed
    facts. Returns the ids the flows need."""
    seed = seeder(desk)
    seed.job("leak-job", "Route Planner", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "leak-job",
        (
            Requirement("leak-r1", "must_have", f"Experience with {LABEL_MARKER} planning"),
            Requirement("leak-r2", "key", "Five years of route planning"),
        ),
        summary_id="summary-leak-job",
    )
    with seed.db.transaction() as conn:
        conn.execute("UPDATE jobs SET triage_state='seen' WHERE id='leak-job'")
    target = desk.target()
    for n in range(3):  # enough items for a session of three questions and later ones
        desk.run.execute(
            "INSERT INTO capture_notes(id,text,created_at,updated_at) VALUES (?,?,?,?)",
            (
                f"leak-note-{n}",
                f"Fictional note {n} about depot shifts.",
                desk.run.clock.now().isoformat(),
                desk.run.clock.now().isoformat(),
            ),
        )
    return {"job": "leak-job", "fact": target.id}


def script_question(desk: Refining, *, question: str, why: str = WHY_MARKER + " is why.") -> None:
    desk.run.spy.script(
        "enrichment_question", ScriptedResponse(data={"question": question, "why": why})
    )


def start_session(desk: Refining, focus: str = "most_useful") -> str:
    response = desk.run.client.post(
        "/sessions", data={"focus": focus, "length": "3"}, headers=action_headers(desk.run)
    )
    assert response.status_code == 200, response.text[:300]
    return response.headers["HX-Redirect"].rsplit("/", 1)[-1]


# --- AC-M1.4 for session text ------------------------------------------------------------------


def test_session_text_never_in_logs_task_rows_usage_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("app.main.UuidIdFactory", SequentialIdFactory),
        watched(tmp_path, monkeypatch, capsys) as watch,
    ):
        run = watch.run
        desk = Refining(run)
        seed_flow(desk)
        portal, opened = open_events(watch)
        with opened as stream:
            frames = [portal.call(stream.next_chunk)]

            # 1. A whole session with the person's own words in it: an answer, a skip, a "not
            #    sure", the end, the summary and the confirmation of the draft.
            script_question(desk, question=f"What happened when you handled {QUESTION_MARKER}?")
            sid = start_session(desk)
            settled(run, sid)
            first = current(desk, sid).answers[0]
            assert not first.fallback, "the scripted question was kept, so it is stored"
            for path in (f"/sessions/{sid}", f"/sessions/{sid}/panel"):
                shown = run.client.get(path, headers=run.run.htmx())
                assert shown.status_code == 200
                add_response(watch.surfaces, path.rsplit("/", 1)[-1], shown)
            desk.run.spy.script(
                "structure_answer",
                ScriptedResponse(
                    data=payload(
                        text=f"Planned delivery routes and kept a log {DRAFT_MARKER}.", skills=[]
                    )
                ),
            )
            answered = post(
                desk,
                sid,
                "answers",
                answer=f"I planned delivery routes and kept a log {ANSWER_MARKER}.",
            )
            assert answered.status_code == 200, answered.text[:300]
            add_response(watch.surfaces, "answer", answered)
            settled(run, sid)
            for action in ("next", "skip", "next", "not-sure", "end"):
                if action == "next":
                    script_question(desk, question=f"Tell me about {QUESTION_MARKER} work.")
                reply = post(desk, sid, action)
                assert reply.status_code == 200, (action, reply.status_code, reply.text[:200])
                add_response(watch.surfaces, action, reply)
                if action == "next":
                    settled(run, sid)
            summary = run.client.get(f"/sessions/{sid}", headers=run.run.htmx())
            assert summary.status_code == 200
            add_response(watch.surfaces, "summary", summary)
            draft_id = current(desk, sid).answers[0].draft_fact_id
            assert draft_id, "the answer produced a draft"
            draft = desk.facts.get(draft_id)
            confirmed = run.client.post(
                f"/sessions/{sid}/drafts/{draft_id}/confirm",
                data={"version": str(draft.version)},
                headers=action_headers(run),
            )
            assert confirmed.status_code == 200, confirmed.text[:300]
            add_response(watch.surfaces, "confirm", confirmed)

            # 2. Coverage, Today, the new-session screen and the Library with hints, by their
            #    own routes (headers and messages; the pages show the text by design).
            everything = " ".join(MARKERS)
            cid = cluster_id(
                "text:" + " ".join(sorted([LABEL_MARKER, "planning"]))
            )  # stop words out
            for path in (
                "/facts/coverage",
                "/facts/coverage?page=1",
                "/today/content",
                "/sessions/new",
                "/facts/library",
            ):
                shown = run.client.get(path, headers=run.run.htmx())
                assert shown.status_code == 200, (path, shown.status_code)
                add_response(watch.surfaces, path.split("?")[0], shown)
            cluster = run.client.get(f"/facts/coverage/{cid}", headers=run.run.htmx())
            assert cluster.status_code == 200, cluster.status_code
            assert LABEL_MARKER in cluster.text, "the cluster of the labelled requirement"
            # The cluster's label is announced in the live region by design ("Showing the facts
            # behind <label>.", the same words as the heading on the page), so that one header is
            # checked for exactly that shape and every other header and message is scanned.
            announced = json.loads(cluster.headers["HX-Trigger"])
            assert list(announced) == ["announce"], announced
            assert announced["announce"].startswith("Showing the facts behind ")
            assert LABEL_MARKER in announced["announce"], "the control: the label is announced"
            for name, value in cluster.headers.multi_items():
                if name.lower() != "hx-trigger":
                    watch.surfaces.add(f"coverage cluster header {name}", f"{name}: {value}")
            for line in status_lines(cluster.text):
                watch.surfaces.add("coverage cluster status line", line)

            # 2b. A session about that cluster. The question the model is scripted to ask makes a
            #     claim (a quantity and an employer, with the label in it), so the app replaces it
            #     with its own wording; then the structuring of the answer fails. The question row
            #     keeps the cluster key, which derives from the label.
            script_question(
                desk,
                question=f"How did you save 90% at Globex Corporation with {LABEL_MARKER}?",
                why=f"Because of {LABEL_MARKER}.",
            )
            clustered = start_session(desk, f"cluster:{cid}")
            settled(run, clustered)
            asked = current(desk, clustered).answers[0]
            assert asked.fallback, "the claim in the question made the app use its own wording"
            assert LABEL_MARKER not in asked.question + asked.why
            run.spy.script(
                "structure_answer", ScriptedResponse(raw_text=f"not json {everything} {{")
            )
            answered = post(desk, clustered, "answers", answer="I planned the routes myself.")
            assert answered.status_code == 200, answered.text[:300]
            assert settled(run, clustered, "failed").error == "InvalidModelOutput"
            shown = run.client.get(f"/sessions/{clustered}/panel", headers=run.run.htmx())
            assert "couldn't finish" in " ".join(status_lines(shown.text))
            add_response(watch.surfaces, "cluster session failed panel", shown)
            assert post(desk, clustered, "end").status_code == 200

            # 3. Failures, a quarter of a year later so the earlier answers' cooldowns have
            #    passed. The question task first, then the structuring task: a malformed answer
            #    holding every marker, then a provider that raises with the text in its detail
            #    and in a chained cause, an unexpected error and a timeout, each retried
            #    through the Tasks route.
            run.clock.advance(timedelta(days=90))
            original = run.spy.run

            async def network(request: object, cancel: object) -> object:
                raise NetworkUnavailable(detail=everything) from RuntimeError(everything)

            async def unexpected(request: object, cancel: object) -> object:
                raise RuntimeError(f"unexpected {everything}")

            async def timeout(request: object, cancel: object) -> object:
                raise TimeoutError(f"timeout {everything}")

            def retry_with(sid2: str, behaviour: Any) -> None:
                monkeypatch.setattr(run.spy, "run", behaviour)
                task = service_of(run).view(sid2).task
                assert task is not None
                retried = run.client.post(f"/tasks/{task.id}/retry", headers=action_headers(run))
                assert retried.status_code == 200, retried.text[:300]
                add_response(watch.surfaces, "retry", retried)
                settled(run, sid2, "failed")
                shown = run.client.get(f"/sessions/{sid2}/panel", headers=run.run.htmx())
                add_response(watch.surfaces, "failed panel", shown)
                assert "couldn't finish" in " ".join(status_lines(shown.text))

            run.spy.script(
                "enrichment_question", ScriptedResponse(raw_text=f"not json {everything} {{")
            )
            failing = start_session(desk)
            assert settled(run, failing, "failed").error == "InvalidModelOutput"
            shown = run.client.get(f"/sessions/{failing}/panel", headers=run.run.htmx())
            add_response(watch.surfaces, "failed question panel", shown)
            for behaviour in (network, unexpected, timeout):
                retry_with(failing, behaviour)
            monkeypatch.setattr(run.spy, "run", original)
            assert post(desk, failing, "end").status_code == 200

            script_question(desk, question=f"Tell me about {QUESTION_MARKER} routes.")
            structuring = start_session(desk)
            settled(run, structuring)
            run.spy.script(
                "structure_answer", ScriptedResponse(raw_text=f"not json {everything} {{")
            )
            answered = post(desk, structuring, "answers", answer=f"I did {ANSWER_MARKER}.")
            assert answered.status_code == 200, answered.text[:300]
            assert settled(run, structuring, "failed").error == "InvalidModelOutput"
            shown = run.client.get(f"/sessions/{structuring}/panel", headers=run.run.htmx())
            add_response(watch.surfaces, "failed structuring panel", shown)
            for behaviour in (network, unexpected, timeout):
                retry_with(structuring, behaviour)
            monkeypatch.setattr(run.spy, "run", original)
            assert post(desk, structuring, "end").status_code == 200

            # 4. The diagnostic screens, and an unexpected error while a session is drawn (the
            #    body the person gets is what is scanned: the in-process client does not go
            #    through uvicorn's logger).
            for path in (
                "/tasks/panel",
                "/tasks/indicator",
                "/settings/usage",
                "/health",
                "/settings/testing-tools",
            ):
                shown = run.client.get(path, headers=run.run.htmx())
                assert shown.status_code == 200, (path, shown.status_code)
                watch.surfaces.add(f"{path} body", shown.text)
                watch.surfaces.add_headers(path, shown)

            def panel_fails(*args: object, **kwargs: object) -> object:
                raise RuntimeError(f"panel {everything}")

            monkeypatch.setattr(run.app.state.session_screens, "panel", panel_fails)
            broken = run.client.get(f"/sessions/{sid}")
            assert broken.status_code == 500
            watch.surfaces.add("unexpected error body", broken.text)
            watch.surfaces.add_headers("unexpected error", broken)

            # 5. The stream, through the last task.
            states = final_states(run)
            assert list(states.values()).count("failed") >= 2, states
            frames.append(read_until_final(portal, stream, states))
            watch.surfaces.add("event stream headers", repr(stream.headers))
            watch.surfaces.add("event stream frames", b"".join(frames).decode("utf-8", "replace"))

        # Control: the flows stored the text, and only where it belongs.
        for marker in MARKERS:
            assert_stored_only_in(run, marker, ALLOWED[marker])
        assert "session_answers" in tables_containing(run, ANSWER_MARKER)
        assert "fact_versions" in tables_containing(run, DRAFT_MARKER)
        assert "enrichment_questions" in tables_containing(run, LABEL_MARKER), (
            "the coverage-focused session stored the cluster the label forms"
        )
        kinds = {r["type"] for r in run.rows("SELECT type FROM tasks WHERE status='failed'")}
        assert kinds == {"enrichment_question", "structure_answer"}, kinds
    watch.surfaces.assert_clean(MARKERS)


# --- the error text a person sees --------------------------------------------------------------


def messages_of(response: Any) -> list[str]:
    """Every text a refusal shows apart from a field redisplaying what was sent: the status and
    alert lines, field errors, the headers, and the whole body when it is not a page."""
    lines = [f"{name}: {value}" for name, value in response.headers.multi_items()]
    if "<html" not in response.text and "<" not in response.text:
        lines.append(response.text)
    lines.extend(status_lines(response.text))
    return lines


def test_session_errors_carry_no_answer_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = ANSWER_MARKER
    with refining(tmp_path) as desk:
        seed_flow(desk)
        run = desk.run
        script_question(desk, question="What happened when you planned routes?")
        sid = start_session(desk)
        settled(run, sid)
        view = current(desk, sid)
        answer = view.answers[-1]
        base = {
            "session_version": str(view.session.version),
            "answer_id": answer.id,
            "version": str(answer.version),
        }
        shown: list[tuple[str, Any]] = []

        def send(label: str, path: str, data: dict[str, str], expect: set[int]) -> Any:
            response = run.client.post(path, data=data, headers=action_headers(run))
            assert response.status_code in expect, (
                label,
                response.status_code,
                response.text[:200],
            )
            shown.append((label, response))
            return response

        answers = f"/sessions/{sid}/answers"
        # Refused values: blank, too long, a NUL byte.
        send("blank", answers, {**base, "answer": "   "}, {422})
        send("too long", answers, {**base, "answer": marker + "x" * 4001}, {422})
        send("nul byte", answers, {**base, "answer": f"{marker}\x00"}, {422})
        # An unusable tier and a bad later date carrying the marker.
        send("tier", answers, {**base, "answer": marker, "tier": marker}, {422})
        send(
            "later date",
            f"/sessions/{sid}/later",
            {**base, "later_choice": "date", "later_date": marker},
            {422},
        )
        # Stale versions: the session and the answer.
        send("stale answer", answers, {**base, "answer": marker, "version": "99"}, {409})
        send("stale session", answers, {**base, "answer": marker, "session_version": "99"}, {409})
        # Fields that are not part of the form, a repeated field, an unknown action.
        send("unknown field", answers, {**base, "answer": marker, marker: marker}, {400})
        send("unknown action", f"/sessions/{sid}/{marker}", {**base, "answer": marker}, {404})
        # An answer for a session that does not exist or is not safe.
        send(
            "missing session",
            f"/sessions/missing-{marker}/answers",
            {**base, "answer": marker},
            {404},
        )
        # A start the budget refuses: the message is the gate's own.
        from app.services.budget_gate import BlockedAction

        monkeypatch.setattr(
            run.app.state.budget_gate,
            "check",
            lambda *args, **kwargs: BlockedAction("Fictional limit reached. Resets on 1 May."),
        )
        send("blocked answer", answers, {**base, "answer": marker}, {409})
        monkeypatch.undo()
        # The session ended: an answer is no longer possible.
        assert post(desk, sid, "end").status_code == 200
        send("ended session", answers, {**base, "answer": marker}, {409, 404})

        # An unexpected error from a service, with the text in its message and cause.
        def explode(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"service {marker}") from ValueError(marker)

        monkeypatch.setattr(run.app.state.sessions, "answer", explode)
        crashed = run.client.post(
            answers, data={**base, "answer": marker}, headers=action_headers(run)
        )
        assert crashed.status_code == 500
        shown.append(("unexpected", crashed))

        leaked = [
            f"{label}: {line[:80]!r}"
            for label, response in shown
            for line in messages_of(response)
            if marker in line
        ]
        assert not leaked, "answer text in an error message:\n" + "\n".join(leaked)

        # Control: the editor does redisplay the text in its field, so a scan of the whole body
        # would have found it; only the messages are free of it.
        redisplayed = [r for label, r in shown if label in {"blank", "too long", "stale answer"}]
        assert any(marker in r.text for r in redisplayed), "the answer is shown back in its field"
        assert all(status_lines(r.text) for r in redisplayed if r.status_code in (409, 422))

    # The same for the errors the service raises itself: no answer text in str, repr or args.
    with refining(tmp_path / "service") as desk:
        seed_flow(desk)
        script_question(desk, question="What happened when you planned routes?")
        sid = start_session(desk)
        settled(desk.run, sid)
        view = current(desk, sid)
        last = view.answers[-1]
        raised = []
        for text, versions in (
            (" ", (last.version, view.session.version)),
            (marker * 400, (last.version, view.session.version)),
            (f"{marker}\x00", (last.version, view.session.version)),
            (marker, (99, view.session.version)),
            (marker, (last.version, 99)),
        ):
            try:
                service(desk).answer(
                    sid, last.id, text, expected_version=versions[0], session_version=versions[1]
                )
            except Exception as error:
                raised.append(error)
        assert len(raised) == 5, "every refused call raised"
        for error in raised:
            texts = [str(error), repr(error), *map(str, error.args), str(error.__cause__)]
            texts += [getattr(error, "user_message", ""), str(getattr(error, "detail", ""))]
            assert not [t for t in texts if marker in t], type(error).__name__


# --- the access log ----------------------------------------------------------------------------


def test_session_query_strings_not_in_access_log(tmp_path: Path) -> None:
    from app.services.facts import FactService
    from tests.coverage_pages import seed_coverage

    marker = "CanarySessionQueryQ97"
    app = start_app(tmp_path)
    try:
        clock = FrozenClock(SEED_INSTANT)
        with Database.open(app.data_dir / "app.db") as db:
            seed = Seeder.make(
                db,
                ApplicationService(lambda: db, clock, SequentialIdFactory("seed")),
                ApplicationStatuses(lambda: db, clock, SequentialIdFactory("seedstatus")),
                ticker(clock),
                stamp_after(clock),
            )
            made = seed_coverage(seed, FactService(db, clock, SequentialIdFactory("seedfact")))
        with app.client() as client:
            token = page_token(client.get("/").text)
            headers = {TOKEN_HEADER_NAME: token, "HX-Request": "true"}
            posted = client.post(
                "/sessions",
                data={"focus": "most_useful", "length": "3"},
                headers={**headers, "Origin": app.origin},
            )
            assert posted.status_code == 200, posted.text[:300]
            sid = posted.headers["HX-Redirect"].rsplit("/", 1)[-1]

            def asked() -> bool:
                page = client.get(f"/sessions/{sid}", headers=headers)
                return page.status_code == 200 and 'id="session-answer"' in page.text

            wait_until(asked, timeout=DEADLINE, message="the first question was asked")
            cid = cluster_id("skill:terraform")
            pages = [
                f"/sessions/new?focus={marker}&length={marker}&q={marker}",
                f"/sessions/{sid}?{marker}={marker}",
                f"/facts?tab=coverage&page={marker}&q={marker}&sort={marker}",
                f"/facts?tab=coverage&{marker}={marker}",
                f"/?q={marker}&{marker}={marker}",
                f"/facts/{made['rust_draft']}/merge?target={marker}",
            ]
            for path in pages:
                response = client.get(path)
                assert response.status_code in (200, 404), (
                    path.split("?")[0],
                    response.status_code,
                )
            fragments = [
                f"/sessions/{sid}/panel?was={marker}",
                f"/sessions/{sid}/panel?{marker}={marker}",
                f"/facts/coverage?page={marker}&{marker}={marker}",
                f"/facts/coverage/{cid}?{marker}={marker}",
                f"/today/content?{marker}={marker}",
                f"/sessions/new?focus={marker}",
            ]
            for path in fragments:
                response = client.get(path, headers=headers)
                assert response.status_code in (200, 204, 404), (
                    path.split("?")[0],
                    response.status_code,
                )
    finally:
        app.stop()
    output = app.stdout_path.read_text() + app.stderr_path.read_text()
    output += "".join(p.read_text() for p in app.log_dir.glob("*.log*"))
    assert "GET /sessions/new HTTP/1.1" in output
    assert f"GET /sessions/{sid} HTTP/1.1" in output
    assert "GET /facts HTTP/1.1" in output
    assert marker not in output
    for fragment in ("?focus=", "?tab=", "&page=", "?was=", "?q=", "?page=", "?target="):
        assert fragment not in output, fragment


# --- never-use facts ---------------------------------------------------------------------------


NEVER_WORDS = "Brightwater audit of 950 pallets"
NEVER_TEXT = f"Confidential {NEVER_WORDS} for the fictional depot."


def test_session_drafts_never_use_never_use_facts(tmp_path: Path) -> None:
    with refining(tmp_path, ids=SequentialIdFactory("privacy")) as desk:
        run = desk.run
        desk.run.app.state.skills.add("Terraform")
        hidden = desk.typed(NEVER_TEXT, privacy="never_use", skills=("Terraform",))
        # TD-321: ids come from the sequential factory, so no random id can hold "950".
        assert hidden.id.startswith("privacy-"), hidden.id
        shown = desk.typed(
            "Wrote Terraform modules for the fictional depot.", skills=("Terraform",)
        )
        seed = seeder(desk)
        seed.job("never-job", "Platform Engineer", "Harbor Kite Fictional")
        seed_summary(
            seed.db,
            "never-job",
            (Requirement("never-r1", "must_have", "Experience with Terraform"),),
            summary_id="summary-never-job",
        )

        # A session cannot be aimed at the never-use fact, and it is not on offer.
        with pytest.raises(SessionEmpty):
            service(desk).start(SessionFocus("fact", hidden.id), 3)
        assert not run.spy.requests, "the refused start asked the provider nothing"
        offered = run.client.get("/sessions/new")
        assert f"fact:{hidden.id}" not in offered.text and NEVER_WORDS not in offered.text

        # A model that repeats the never-use text anyway: the question is replaced, and the
        # draft is flagged (the draft is checked against the answer and the chosen fact only).
        run.spy.script(
            "enrichment_question",
            ScriptedResponse(
                data={
                    "question": "How did you carry out the Brightwater audit of 950 pallets?",
                    "why": "To explore the Brightwater audit of 950 pallets.",
                }
            ),
        )
        made = service(desk).start(SessionFocus("skill", "Terraform"), 3)
        run.wait_terminal(made.task.id)
        first = current(desk, made.session_id).answers[0]
        assert first.fallback and "Brightwater" not in first.question + first.why
        run.spy.script(
            "structure_answer",
            ScriptedResponse(
                data=payload(
                    text="Led the Brightwater audit of 950 pallets with Terraform.",
                    skills=["Terraform"],
                )
            ),
        )
        reply = post(desk, made.session_id, "answers", answer="I wrote some modules.")
        assert reply.status_code == 200, reply.text[:300]
        run.wait_terminal(current(desk, made.session_id).task.id)
        answer = current(desk, made.session_id).answers[-1]
        draft = desk.facts.get(answer.draft_fact_id)
        assert draft.needs_check and answer.flagged_claims, "the repeated text was flagged"
        assert any(
            "950" in item["text"] or "Brightwater" in item["text"] for item in answer.flagged_claims
        )
        assert draft.id != hidden.id and draft.status.value == "draft"

        # Neither task's requests held the never-use text, its number reference or its id.
        assert run.spy.requests, "the session made requests"
        for request in run.spy.requests:
            text = "\n".join((request.system, request.user, json.dumps(request.output_schema)))
            assert "Brightwater" not in text and "950" not in text, request.task
            assert f"F{hidden.number}@" not in text, request.task
            assert hidden.id not in text, request.task
        assert any(f"F{shown.number}@" in r.user for r in run.spy.requests), (
            "ordinary facts are used"
        )


# --- the inputs of the opt-in live test, built offline ---------------------------------------


def test_m7_live_inputs_build_offline_and_the_question_check_has_teeth(tmp_path: Path) -> None:
    """The live test (``tests/live/test_m7_live.py``, user-run) cannot run in CI, so its inputs
    are proved here against the fake provider: five distinct question requests and one structuring
    request, schemas that are valid and accept the fake's own answers, and a check that accepts an
    open question and names the problem of a leading or claiming one."""
    from jsonschema import Draft202012Validator

    from tests.session_live_inputs import (
        ANSWER,
        QUESTION_COUNT,
        built_inputs,
        open_question_problems,
    )

    built = built_inputs(tmp_path)
    assert len(built.questions) == QUESTION_COUNT == len(built.allowed)
    assert {r.task for r in built.questions} == {"enrichment_question"}
    assert built.structure.task == "structure_answer"
    assert len({r.user for r in built.questions}) == QUESTION_COUNT, "five different items"
    assert ANSWER in built.structure.user, "the fictional answer is what is structured"
    assert all('"outcome": "skipped"' in r.user for r in built.questions[2:]), (
        "later questions carry the session so far"
    )
    for request in (*built.questions, built.structure):
        Draft202012Validator.check_schema(request.output_schema)
        assert request.allow_web_search is False
    fake_question = {
        "question": "Tell me about an experience you would like to keep.",
        "why": "Practice data: this is a fictional example question.",
    }
    for request, allowed in zip(built.questions, built.allowed, strict=True):
        assert not list(Draft202012Validator(request.output_schema).iter_errors(fake_question))
        assert open_question_problems(**fake_question, allowed=allowed) == []
        assert "yes_no" in open_question_problems(
            "Did you plan the routes?", fake_question["why"], allowed
        )
        assert "tag" in open_question_problems(
            "You planned the routes, right?", fake_question["why"], allowed
        )
        assert "new_claims" in open_question_problems(
            "How did you save 90% at Globex Corporation?", fake_question["why"], allowed
        )
        assert "form" in open_question_problems("Plan the routes.", fake_question["why"], allowed)
        assert "why_new_claims" in open_question_problems(
            fake_question["question"], "Because you saved 90% at Globex Corporation.", allowed
        )


def test_m7_live_inputs_hold_what_the_live_test_says_they_hold(tmp_path: Path) -> None:
    """The live test's docstring, as facts about the requests: the five items by kind, the
    earlier wording on the stocktake note's request, the session so far holding only the skipped
    canned question (the answer never in a later request), and an answer that is about the note
    it structures."""
    from collections import Counter

    from tests.session_live_inputs import ANSWER, NOTES, built_inputs

    built = built_inputs(tmp_path)
    assert Counter(built.kinds) == {"capture_note": 2, "cluster": 2, "quality_hint": 1}

    def block(request: Any, name: str) -> Any:
        found = re.search(
            rf'<untrusted_document source="{name}"[^>]*>\s*(.*?)\s*</untrusted_document>',
            request.user,
            re.DOTALL,
        )
        assert found is not None, name
        return json.loads(found.group(1))

    wordings = [block(request, "earlier_wordings") for request in built.questions]
    assert [bool(w) for w in wordings].count(True) == 1, "one request carries an earlier wording"
    carrying = next(i for i, w in enumerate(wordings) if w)
    assert NOTES[0] in built.questions[carrying].user, "it is the stocktake note's request"
    assert built.kinds[carrying] == "capture_note"

    assert all(ANSWER not in request.user for request in built.questions), (
        "the answer is in no question request: an answered item leaves the session history"
    )
    for request in built.questions:
        assert all(e["outcome"] == "skipped" for e in block(request, "session_history"))

    assert built.answer_note == NOTES[0] and built.answer_note in built.structure.user
    assert "stocktake" in ANSWER and "stocktake" in built.answer_note, (
        "the answer is about the note"
    )
    assert built.known_skills == ("Terraform",)


def test_m7_live_draft_check_accepts_a_faithful_draft_and_names_what_is_wrong(
    tmp_path: Path,
) -> None:
    """The check the live test runs on the real structuring answer, on scripted outputs: a clean
    draft passes; an added number, a role that was not offered, a skill the answer does not name,
    a listed added detail and an employer out of nowhere each give a problem code."""
    from tests.integration.test_session_answers import payload
    from tests.session_live_inputs import built_inputs, draft_problems

    built = built_inputs(tmp_path)

    def problems(**changes: Any) -> list[str]:
        return draft_problems(built, payload(skills=[], **changes))

    clean = "Ran the spring stocktake at the depot with a partner in two days."
    assert problems(text=clean) == []
    assert any("number" in code for code in problems(text=clean.replace("two", "12")))
    assert "flag_unknown_role" in problems(text=clean, role_key="role-not-offered")
    assert "flag_skill" in draft_problems(built, payload(text=clean, skills=["Terraform"]))
    assert "flag_added_detail" in problems(text=clean, added_details=["a partner named Sam"])
    assert problems(text="Ran the stocktake at Globex Corporation.") != []
    story = {
        "type": "story",
        "text": None,
        "situation": "Ran the spring stocktake at the depot.",
        "action": "Counted with a partner.",
        "result": "Finished it in two days.",
    }
    assert problems(**story) == [], "a story is judged by all three of its parts"
    assert any("number" in code for code in problems(**{**story, "result": "Finished in 12 days."}))


def test_m7_live_requirement_questions_that_presume_are_named() -> None:
    """The live-only heuristic for the two requirement items (not part of the app's own rule)."""
    from tests.session_live_inputs import presumes

    for presuming in (
        "How did you plan routes for the depot?",
        "What did you do to learn Terraform?",
        "When have you planned a route?",
        "Where do you plan routes?",
        "Which was you favourite route plan?",
        "  why did you plan routes?",
        "You planned routes. Tell me more.",
        "you have planned routes before.",
    ):
        assert presumes(presuming), presuming
    for open_one in (
        "Tell me about a time you planned a route.",
        "What happened when you planned a route?",
        "How would you plan a route for a new depot?",
        "What does route planning involve for you?",
        "Where would you start with Terraform?",
        "Young drivers need a plan. How would you help?",
        "How did young drivers get routes?",
        "",
    ):
        assert not presumes(open_one), open_one


# --- performance -------------------------------------------------------------------------------

TOPICS = (
    "route planning",
    "depot scheduling",
    "fleet maintenance",
    "pallet handling",
    "dock safety",
    "cold chain monitoring",
    "driver rostering",
    "customs paperwork",
    "inventory counting",
    "forklift operation",
    "warehouse layout",
    "supplier negotiation",
    "returns processing",
    "load balancing",
    "carrier contracts",
    "yard management",
    "parcel sorting",
    "dispatch coordination",
    "fuel budgeting",
    "incident reporting",
)
QUALIFIERS = (
    "in a busy depot",
    "across several sites",
    "during peak season",
    "with a small team",
    "under tight deadlines",
    "for regional customers",
    "on night shifts",
    "with new starters",
    "for fragile goods",
    "in cold storage",
    "for overseas orders",
    "with limited tools",
    "after a merger",
    "for a growing fleet",
    "with strict audits",
    "in a new warehouse",
    "for seasonal staff",
    "during a system change",
    "for hazardous goods",
    "with shared vehicles",
)
SKILLS = ("Python", "Terraform", "Kotlin", "SQL", "Excel", "Docker", "Linux", "Rust")
TEMPLATES = (
    "Experience with {}",
    "Five years of {}",
    "Strong {} skills",
    "{} certificate",
    "Able to lead {} work",
)
BUDGET_SECONDS = 0.150


def seed_scale(desk: Refining, jobs: int = 1000, facts: int = 300) -> None:
    """``jobs`` saved jobs with five requirements each (kinds mixed, wording from a shared pool
    so the clusters overlap) in one transaction, then ``facts`` confirmed facts, a few stories,
    skill tags, drafts and never-use facts, through the app's own service."""
    rng = random.Random(20261031)  # noqa: S311 - a fixed seed for a reproducible fixture
    # About 2,000 distinct wordings that share words (so similarity has real work to do).
    pool = [
        template.format(f"{topic} {qualifier}")
        for topic in TOPICS
        for template in TEMPLATES
        for qualifier in QUALIFIERS
    ]
    pool += [f"{skill} scripting" for skill in SKILLS]
    kinds = ("must_have", "key", "nice")
    now = desk.run.clock.now().isoformat()
    with desk.run.app.state.workspace.db.transaction() as conn:
        for n in range(jobs):
            job_id = f"scale-job-{n:04d}"
            jobs_repo.insert_job(
                conn,
                id=job_id,
                title=f"Fictional Role {n}",
                snapshot_text=f"Fictional posting number {n}.",
                captured_at=now,
                found_at=now,
                now=now,
            )
            conn.execute("UPDATE jobs SET triage_state='seen' WHERE id=?", (job_id,))
            summary_id = f"scale-summary-{n:04d}"
            conn.execute(
                "INSERT INTO job_summaries (id,job_id,summary,fields_json,provider,model,"
                "prompt_id,prompt_version,task_id,created_at) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
                (
                    summary_id,
                    job_id,
                    "Fictional summary.",
                    "{}",
                    "fake",
                    "fake-thorough",
                    "summarize_job",
                    "1",
                    now,
                ),
            )
            for position, text in enumerate(rng.sample(pool, 5), 1):
                conn.execute(
                    "INSERT INTO job_requirements (id,summary_id,position,kind,text) "
                    "VALUES (?,?,?,?,?)",
                    (
                        f"scale-r-{n:04d}-{position}",
                        summary_id,
                        position,
                        kinds[position % 3],
                        text,
                    ),
                )
    for skill in SKILLS:
        desk.run.app.state.skills.add(skill)
    for n in range(facts):
        topic = TOPICS[n % len(TOPICS)]
        skills = (SKILLS[n % len(SKILLS)],) if n % 3 == 0 else ()
        if n % 7 == 0:
            desk.facts.create(
                FactInput(
                    type=FactType.STORY,
                    situation=f"The depot needed better {topic}.",
                    action=f"Improved {topic} for {n % 9 + 2} teams.",
                    result="Cut manual steps by half.",
                    skills=skills,
                )
            )
        elif n % 41 == 0:
            desk.facts.create(FactInput(text=f"Private note {n} on {topic}.", privacy="never_use"))
        else:
            desk.facts.create(
                FactInput(text=f"Handled {topic} for {n % 9 + 2} fictional depots.", skills=skills)
            )


def measure(
    results: dict[str, float],
    label: str,
    request: Callable[[], Any],
    ok: Callable[[Any], bool],
    *,
    after: Callable[[Any], None] | None = None,
) -> None:
    times = []
    for _ in range(5):
        begin = perf_counter()
        response = request()
        times.append(perf_counter() - begin)
        assert ok(response), (label, response.status_code, response.text[:200])
        if after is not None:
            after(response)
    results[label] = median(times)


@pytest.mark.slow
def test_ac_m7_fragments_under_150ms_with_1000_jobs_300_facts(
    tmp_path: Path, record_property: Any
) -> None:
    with refining(tmp_path) as desk:
        seed_scale(desk)
        run = desk.run
        assert desk.run.rows("SELECT count(*) AS n FROM jobs")[0]["n"] == 1000
        assert desk.run.rows("SELECT count(*) AS n FROM facts")[0]["n"] >= 300
        sessions = service_of(run)
        assert sessions.backlog(), "the fixture has something to ask"
        assert desk.run.rows("SELECT count(*) AS n FROM job_requirements")[0]["n"] == 5000
        first_page = run.client.get("/facts/coverage", headers=run.run.htmx()).text
        assert first_page.count("data-cluster-id=") == 50, "a full first page of clusters"
        assert 'aria-label="Coverage pages"' in first_page
        client, htmx = run.client, run.run.htmx()
        results: dict[str, float] = {}

        # The Coverage tab (its first page and its second) and the new-session screen.
        measure(
            results,
            "GET /facts/coverage",
            lambda: client.get("/facts/coverage", headers=htmx),
            lambda r: r.status_code == 200 and "coverage-table" in r.text,
        )
        measure(
            results,
            "GET /facts/coverage?page=2",
            lambda: client.get("/facts/coverage?page=2", headers=htmx),
            lambda r: r.status_code == 200,
        )
        measure(
            results,
            "GET /sessions/new",
            lambda: client.get("/sessions/new", headers=htmx),
            lambda r: r.status_code == 200 and "Most useful" in r.text,
        )

        # Today with a suggestion (no reset stored and the weekly default: due).
        measure(
            results,
            "GET /today/content",
            lambda: client.get("/today/content", headers=htmx),
            lambda r: r.status_code == 200 and "Suggested session" in r.text,
        )

        # Starting a session: only the request is timed; the question is awaited and the session
        # ended before the next start.
        def start_request() -> Any:
            return client.post(
                "/sessions",
                data={"focus": "most_useful", "length": "3"},
                headers=action_headers(run),
            )

        def end_after(response: Any) -> None:
            sid = response.headers["HX-Redirect"].rsplit("/", 1)[-1]
            settled(run, sid)
            version = current(desk, sid).session.version
            sessions.end(sid, expected_version=version)

        measure(
            results,
            "POST /sessions",
            start_request,
            lambda r: r.status_code == 200 and "HX-Redirect" in r.headers,
            after=end_after,
        )

        # The session panel with a question and a draft in it.
        sid = start_session(desk)
        settled(run, sid)
        assert post(desk, sid, "answers", answer="I planned 12 routes.").status_code == 200
        settled(run, sid)
        measure(
            results,
            "GET /sessions/{sid}/panel",
            lambda: client.get(f"/sessions/{sid}/panel", headers=htmx),
            lambda r: r.status_code == 200 and "session-panel" in r.text,
        )
        measure(
            results,
            "GET /sessions/{sid} (fragment)",
            lambda: client.get(f"/sessions/{sid}", headers=htmx),
            lambda r: r.status_code == 200 and "session-panel" in r.text,
        )

        for label, seconds in results.items():
            record_property(label, round(seconds * 1000, 2))
        slow = {label: round(s * 1000, 2) for label, s in results.items() if s >= BUDGET_SECONDS}
        assert not slow, f"over the {BUDGET_SECONDS * 1000:.0f} ms budget (median of 5): {slow}"
