"""M8.3 part C cross-cutting checks: AC-M1.4 extended to interview-preparation text, never-use facts
in every M8 request, and the response-time budget of the Prep, board, Today, drawer, debrief and
company screens (spec 2.9 rules 3, 9 and 13, 2.10, 2.11, 3.3, 3.8; ADR-124 item 11, ADR-127).

What is planted, all fictional, in the real flows (the app's own routes, the fake provider's
scripted answers, a person's typed text):

- a practice answer, typed by the person (kept as the attempt's answer);
- a feedback comment, in the answer the provider is scripted to give (kept with the attempt);
- a bank question, in a generated question, and a source link, in a question reported online;
- the questions asked, how it went and an experience, typed into a debrief; and the wording of
  the draft fact the provider is scripted to write from that experience;
- a narrative card paragraph and an adapted answer paragraph, in the provider's scripted answers;
- a company brief paragraph and its source link, in the provider's scripted answer.

They may be stored only in the tables of ``ALLOWED`` below (an explicit allow-list: a marker in
any other table fails) and shown back to the person who typed them; they must never reach a
surface that is only about tasks and diagnostics: every log record as it was made (before
redaction), the log files, the console, the rows of ``tasks``, ``usage_records``,
``provider_usage_snapshots``, ``budget_notices`` and ``settings``, the event stream (headers and
frames), the response headers and status lines of the M8 routes, the bodies of ``/health``, the
Tasks panel and the testing tools section, and the error text a person sees when something fails.
The failures are forced for each of the seven M8 tasks (the six AI tasks and the debrief's draft
facts) through a malformed answer, then a provider that raises with the text in its detail and in
a chained cause, an unexpected exception and a timeout, each retried through the Tasks panel's
own route; and through an unexpected error while each M8 screen is drawn.

``test_prep_scan_catches_a_planted_marker`` is the scan's own control. The M8 screens read the
same text back to the person in escaped fields, so the bodies of those pages are not scanned, only
their headers, the status, alert and notice lines (``.notice``, ``#...-message``) and the
diagnostic screens' bodies. Every response the successful flows get is scanned that way
(``World.post`` and ``World.get`` record it), among them the small actions: mark a question ready,
add a claim as a draft fact, edit and approve a card, dismiss a debrief.

``test_prep_errors_carry_no_answer_text`` forces the refusals an M8 route can give with a marker
in the request and scans the message each one shows. Each probe sends a valid body (a real version,
claim, unit and field names) so that it reaches the branch it is named for, and its exact status is
pinned; the refusals of a malformed body (always 400) are a separate group. A refused form may
redisplay what was typed in its own field, so the scan reads the messages, headers and plain-text
bodies, and a control proves the redisplay is the only place the text shows.

``test_prep_query_strings_not_in_access_log`` runs the real CLI and fetches the Prep, practice,
cards, debrief, drawer and company routes with a marker in every query value (``q``, ``type``,
``focus`` and the rest). Nothing of a query string may reach the log files or the console.

``test_never_use_facts_absent_from_every_m8_request`` pins spec 3.1 for the M8 tasks: the text of
a never-use fact, its skill and its role, its number reference and its id reach no request of any
of them (the debrief's ``structure_answer`` included), and the requests that need no facts carry
no fact text at all.

``test_ac_m8_fragments_under_150ms`` times the screens of spec 3.8 with 1,000 bank questions, 500
applications and 300 facts.

No network, no sleeps, ``FrozenClock`` where the harness uses one.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from app.domain.errors import NetworkUnavailable
from app.modules.ai.providers.fake import ScriptedResponse
from app.modules.storage import Database
from app.services.debrief import DebriefEntry
from app.services.facts import FactInput, NewRole
from tests.board_pages import Seeder, elements, stamp_after, ticker
from tests.brief_pages import COMPANY, INDUSTRY, WEBSITE, company_id, company_version, searching
from tests.debrief_pages import EVENT_END, EVENT_START, NOW, base
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_m4_cross_cutting import (
    Surfaces,
    assert_stored_only_in,
    final_states,
    open_events,
    read_until_final,
    tables_containing,
    watched,
)
from tests.integration.test_m7_cross_cutting import BUDGET_SECONDS, measure, seed_scale
from tests.integration.test_prep_practice import FIXTURES as FEEDBACK_FIXTURES
from tests.integration.test_prep_practice import GROUNDED, TWO_CLAIMS
from tests.integration.test_session_answers import payload
from tests.page_token import TOKEN_HEADER_NAME, page_token
from tests.refine_harness import Refining, refining
from tests.reminder_desk import add_event
from tests.running_app import start_app
from tests.tailoring_ai_inputs import FACT_1, FACT_2, FACT_3, HIDDEN, KEPT_TEXT
from tests.tailoring_pages import DRAFT_TEXT, Tailoring

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
DEADLINE = 20.0

# Letters only (TD-321: a random id can hold digits, and a number check can trip on them).
ANSWER_MARKER = "canarypracticelynx"
FEEDBACK_MARKER = "canaryfeedbackmoth"
BANK_MARKER = "canarybankquestionheron"
WEBURL_MARKER = "canarywebsourcestoat"
ASKED_MARKER = "canaryaskedcrane"
HOW_MARKER = "canaryhowitwentlark"
EXPERIENCE_MARKER = "canaryexperiencevole"
DRAFT_MARKER = "canarydebriefdraftwren"
NARRATIVE_MARKER = "canarynarrativeibis"
ADAPTED_MARKER = "canaryadaptedtern"
BRIEF_MARKER = "canarybrieftextkite"
BRIEFURL_MARKER = "canarybriefurlpipit"
MARKERS = (
    ANSWER_MARKER,
    FEEDBACK_MARKER,
    BANK_MARKER,
    WEBURL_MARKER,
    ASKED_MARKER,
    HOW_MARKER,
    EXPERIENCE_MARKER,
    DRAFT_MARKER,
    NARRATIVE_MARKER,
    ADAPTED_MARKER,
    BRIEF_MARKER,
    BRIEFURL_MARKER,
)

WEB_URL = f"https://example.test/{WEBURL_MARKER}"
BRIEF_URL = f"https://example.test/{BRIEFURL_MARKER}"

# Where each marker may legitimately be stored: the tables it was observed to reach in the flows
# below, and no others. Every table not named here, whatever it holds, must not have it. The
# full text indexes keep their own copy of a bank question's or a fact's text; their shadow tables
# are named by SQLite, so only the two this build writes the text to are allowed.
BANK_TABLES = frozenset({"bank_questions", "bank_questions_fts", "bank_questions_fts_content"})
ALLOWED: dict[str, frozenset[str]] = {
    # the answer is kept with the attempt
    ANSWER_MARKER: frozenset({"practice_attempts"}),
    # the feedback is kept with the attempt (its ``feedback`` column)
    FEEDBACK_MARKER: frozenset({"practice_attempts"}),
    BANK_MARKER: BANK_TABLES,
    # a question reported online keeps the link with the question and with its sighting
    WEBURL_MARKER: frozenset({"bank_questions", "bank_question_sightings"}),
    # asked questions become bank questions (and are kept as typed with the debrief)
    ASKED_MARKER: BANK_TABLES | {"debriefs"},
    # how it went is kept locally, never sent to the AI
    HOW_MARKER: frozenset({"debriefs"}),
    # the experience is kept with the debrief and as the original answer of the draft fact
    EXPERIENCE_MARKER: frozenset({"debriefs", "fact_versions"}),
    DRAFT_MARKER: frozenset({"fact_versions", "facts_fts", "facts_fts_content"}),
    NARRATIVE_MARKER: frozenset({"prep_units"}),
    ADAPTED_MARKER: frozenset({"prep_units"}),
    BRIEF_MARKER: frozenset({"company_briefs"}),
    BRIEFURL_MARKER: frozenset({"company_briefs"}),
}

FAILING = (
    "interview_questions",
    "interview_web_questions",
    "answer_feedback",
    "draft_narrative",
    "adapt_answer",
    "company_brief",
    "debrief_facts",
)
FAKE_QUESTION_TEXT = "Tell me about a time you planned under pressure."


# --- reading what a person is told -------------------------------------------------------------


def status_lines(page: str) -> list[str]:
    """What a person is told: the text of every status and alert element, notice and
    ``...-message`` element and of the field errors (a redisplayed field itself is not a
    message)."""
    return [
        e.text
        for e in elements(page)
        if e.attrs.get("role") in {"alert", "status"}
        or "field-error" in e.classes
        or "notice" in e.classes
        or str(e.attrs.get("id", "")).endswith("-message")
    ]


def messages_of(response: Any) -> list[str]:
    """Every text a response shows apart from a field redisplaying what was sent: the headers
    (announcements included), the status and alert lines, and the body when it is not a page."""
    lines = [f"{name}: {value}" for name, value in response.headers.multi_items()]
    if "<" not in response.text:
        lines.append(response.text)
    lines.extend(status_lines(response.text))
    return lines


def add_response(surfaces: Surfaces, where: str, response: Any) -> None:
    for line in messages_of(response):
        surfaces.add(f"{where} message", line)


# --- the control -------------------------------------------------------------------------------


def never_use_problems(
    request: Any, hidden: str, words: tuple[str, ...], fact_id: str
) -> list[str]:
    """What of a never-use fact a request holds: its text, a word of it, its number reference
    (``F4@``, ``F4``) or its id."""
    text = "\n".join((request.system, request.user, json.dumps(request.output_schema)))
    found = [f"text:{hidden[:20]}" for _ in (0,) if hidden in text]
    found += [f"word:{word}" for word in words if word.lower() in text.lower()]
    found += ["reference"] if re.search(r"\bF4\b|F4@", text) else []
    found += ["id"] if fact_id in text else []
    return found


def test_prep_scan_catches_a_planted_marker() -> None:
    """Planted in a header, a status line, a notice, a ``-message`` element, a task column or the
    settings row, a marker is found; a clean run is clean; the redisplayed text in a field is not a
    message; a request holding a never-use fact is named.

    The log, console and file surfaces are controlled by
    ``test_surface_scan_catches_a_planted_marker`` in ``test_m4_cross_cutting.py``; this control
    covers the parts that are specific to the M8 scan."""
    surfaces = Surfaces()
    surfaces.add("tasks row 0 column error", f"Failed: {ANSWER_MARKER}")
    surfaces.add("settings row 3 column value", BRIEF_MARKER)
    assert surfaces.leaks(MARKERS) == [
        f"{ANSWER_MARKER!r} in tasks row 0 column error",
        f"{BRIEF_MARKER!r} in settings row 3 column value",
    ]
    page = (
        f'<p id="debrief-message">Saved {ASKED_MARKER}</p>'
        f'<p class="field-error">Bad {DRAFT_MARKER}</p>'
        f'<p class="notice">Typed {FEEDBACK_MARKER}</p>'
        f'<p role="alert">Failed {BRIEF_MARKER}</p>'
        f'<textarea id="practice-answer">{ANSWER_MARKER}</textarea>'
    )
    lines = status_lines(page)
    assert any(ASKED_MARKER in line for line in lines), "a ...-message element is a message"
    assert any(DRAFT_MARKER in line for line in lines)
    assert any(FEEDBACK_MARKER in line for line in lines), "a notice is a message"
    assert any(BRIEF_MARKER in line for line in lines)
    assert not any(ANSWER_MARKER in line for line in lines), "the field is not a message"
    reply = SimpleNamespace(
        headers=SimpleNamespace(
            multi_items=lambda: [("HX-Trigger", f'{{"announce": "{HOW_MARKER}"}}')]
        ),
        text=page,
    )
    shown = messages_of(reply)
    assert any(HOW_MARKER in line for line in shown), "an announcement is a message"
    plain = SimpleNamespace(headers=SimpleNamespace(multi_items=lambda: []), text=EXPERIENCE_MARKER)
    assert messages_of(plain) == [EXPERIENCE_MARKER], "a plain-text body is a message"
    clean = Surfaces()
    clean.add("x", "Question generation started.")
    assert clean.leaks(MARKERS) == []

    hidden_request = SimpleNamespace(
        system="s", user=f"F4@1: {HIDDEN}", output_schema={"x": "fact-id-fictional"}
    )
    assert never_use_problems(hidden_request, HIDDEN, ("Brightwater",), "fact-id-fictional")
    clean_request = SimpleNamespace(system="s", user=f"F1@1: {FACT_1}", output_schema={})
    assert never_use_problems(clean_request, HIDDEN, ("Brightwater",), "fact-id-fictional") == []


# --- the world the flows run in ----------------------------------------------------------------


def feedback_data(comment: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FEEDBACK_FIXTURES / "ok.json").read_text())["data"]
    data["checklist"]["answers"]["comment"] = comment
    return data


@dataclass
class World:
    """Three applications (the first has two finished interviews), facts F1 to F5 (F4 never-use),
    a company with a website and industry, a search provider that may keep results."""

    r: Refining
    tw: Tailoring
    aid: str
    other: str
    third: str
    cid: str
    requirement: str
    events: tuple[str, str]
    spare: str  # a third finished interview, for the debrief that is dismissed
    recorder: Callable[[str, Any], None] | None = None

    @classmethod
    def build(cls, r: Refining) -> "World":
        tw = Tailoring(r)
        aid = str(tw.ready("m8-leak-job").id)
        other = str(tw.another_application("m8-other-job").id)
        third = str(tw.another_application("m8-third-job").id)
        cid = company_id(r)
        r.run.execute(
            "UPDATE companies SET website=?, industry=? WHERE id=?", (WEBSITE, INDUSTRY, cid)
        )
        searching(r, True)
        db = r.run.app.state.workspace.db
        first = add_event(
            db, "m8-event-one", aid, "interview", EVENT_START, EVENT_END, zone="Europe/Lisbon"
        )
        second = add_event(
            db,
            "m8-event-two",
            aid,
            "interview",
            "2026-10-19T09:00:00+00:00",
            "2026-10-19T10:00:00+00:00",
            zone="Europe/Lisbon",
        )
        spare = add_event(
            db,
            "m8-event-three",
            aid,
            "interview",
            "2026-10-20T11:00:00+00:00",
            "2026-10-20T11:30:00+00:00",
            zone="Europe/Lisbon",
        )
        r.run.clock.set(datetime.fromisoformat(NOW))
        rid = str(
            r.run.rows("SELECT id FROM job_requirements WHERE kind='must_have' ORDER BY position")[
                0
            ]["id"]
        )
        return cls(r, tw, aid, other, third, cid, rid, (first, second), spare)

    # --- plumbing ----------------------------------------------------------------------------

    @property
    def run(self) -> Any:
        return self.r.run

    def seen(self, label: str, response: Any) -> Any:
        """Hand a response to the recorder (when there is one) and return it."""
        if self.recorder is not None:
            self.recorder(label, response)
        return response

    def post(self, path: str, data: dict[str, str] | None = None) -> Any:
        response = self.run.client.post(path, data=data or {}, headers=self.run.run.action(**HTMX))
        return self.seen(f"POST {path}", response)

    def get(self, path: str) -> Any:
        response = self.run.client.get(path, headers=self.run.run.htmx())
        return self.seen(f"GET {path.split('?')[0]}", response)

    def task_row(self, task_type: str) -> Any:
        found = self.run.rows("SELECT id FROM tasks WHERE type=? ORDER BY rowid DESC", (task_type,))
        assert found, f"no {task_type} task"
        return found[0]["id"]

    def settled(self, task_type: str, status: str = "succeeded") -> Any:
        row = self.run.wait_terminal(self.task_row(task_type))
        assert str(row.status.value) == status, (task_type, str(row.status.value), row.error)
        return row

    def script(self, task: str, **kwargs: Any) -> None:
        self.run.spy.script(task, ScriptedResponse(**kwargs))

    def ok(self, response: Any) -> Any:
        assert response.status_code == 200, (response.status_code, response.text[:300])
        return response

    # --- paths -------------------------------------------------------------------------------

    def prep_path(self, tail: str = "") -> str:
        return f"/applications/{self.aid}/prep{tail}"

    def practice_path(self, qid: str, tail: str = "", aid: str | None = None) -> str:
        return f"/applications/{aid or self.aid}/prep/practice/{qid}{tail}"

    def cards_path(self, tail: str = "") -> str:
        return f"/applications/{self.aid}/prep/cards{tail}"

    def brief_path(self) -> str:
        return f"/companies/{self.cid}/brief"

    def debrief_path(self, event: str, tail: str = "") -> str:
        return base(self.aid, event) + tail

    # --- versions ----------------------------------------------------------------------------

    def prep_version(self) -> int:
        return int(self.run.app.state.prep.view(self.aid).version)

    def item_version(self, qid: str, aid: str | None = None) -> int:
        return int(self.run.app.state.answer_practice.view(aid or self.aid, qid).version)

    def debrief_version(self, event: str) -> int:
        found = self.run.rows("SELECT version FROM debriefs WHERE event_id=?", (event,))
        return int(found[0]["version"]) if found else 0

    def app_version(self) -> int:
        return int(
            self.run.rows("SELECT version FROM applications WHERE id=?", (self.aid,))[0]["version"]
        )

    # --- starts (the route only) -------------------------------------------------------------

    def start_questions(self, action: str = "generate") -> Any:
        return self.post(self.prep_path(f"/{action}"), {"version": str(self.prep_version())})

    def start_web(self) -> Any:
        return self.post(self.prep_path("/web"), {"version": str(self.prep_version())})

    def start_practice(self, qid: str, answer: str) -> Any:
        self.run.clock.advance(timedelta(minutes=1))  # attempts are ordered by their time
        return self.post(
            self.practice_path(qid, "/answer"),
            {"answer": answer, "version": str(self.item_version(qid))},
        )

    def start_card(self, kind: str) -> Any:
        return self.post(self.cards_path(f"/{kind}/generate"), {"version": ""})

    def start_adapt(self, qid: str, aid: str) -> Any:
        return self.post(self.practice_path(qid, "/adapt", aid), {"version": ""})

    def start_brief(self) -> Any:
        return self.post(self.brief_path(), {"version": str(company_version(self.r, self.cid))})

    def save_debrief(self, event: str, **fields: str) -> Any:
        data = {"version": str(self.debrief_version(event)), **fields}
        return self.post(self.debrief_path(event, "/save"), data)

    def start_drafts(self, event: str) -> Any:
        return self.post(
            self.debrief_path(event, "/drafts"), {"version": str(self.debrief_version(event))}
        )

    # --- whole successful flows --------------------------------------------------------------

    def generated_question(self, text: str) -> str:
        self.script(
            "interview_questions",
            data={
                "questions": [
                    {
                        "text": text,
                        "type": "behavioral",
                        "requirement_ids": [self.requirement],
                        "company_specific": False,
                    }
                ],
                "ask_them": [],
            },
        )
        self.ok(self.start_questions())
        self.settled("interview_questions")
        found = self.run.rows("SELECT id FROM bank_questions WHERE text=?", (text,))
        assert len(found) == 1, "the generated question was stored"
        return str(found[0]["id"])

    def web_question(self, text: str, url: str) -> None:
        self.script(
            "interview_web_questions",
            data={
                "questions": [
                    {"text": text, "type": "behavioral", "source_url": url, "source_date": None}
                ]
            },
            tool_calls=1,
            searched_urls=(url,),
        )
        self.ok(self.start_web())
        self.settled("interview_web_questions")

    def practised(self, qid: str, answer: str, comment: str) -> None:
        self.script("answer_feedback", data=feedback_data(comment))
        self.ok(self.start_practice(qid, answer))
        self.settled("answer_feedback")

    def card(self, kind: str, text: str) -> None:
        self.script(
            "draft_narrative",
            data={"units": [{"text": text, "fact_ids": ["F1@1"], "added_details": []}]},
        )
        self.ok(self.start_card(kind))
        self.settled("draft_narrative")

    def adapted(self, qid: str, aid: str, text: str) -> None:
        self.script(
            "adapt_answer",
            data={"units": [{"text": text, "fact_ids": ["F1@1"], "added_details": []}]},
        )
        self.ok(self.start_adapt(qid, aid))
        self.settled("adapt_answer")

    def briefed(self, text: str, url: str) -> None:
        self.script(
            "company_brief",
            data={
                "sections": [
                    {
                        "heading": "what_they_do",
                        "text": text,
                        "source_url": url,
                        "source_date": None,
                    }
                ]
            },
            tool_calls=1,
            searched_urls=(url,),
        )
        self.ok(self.start_brief())
        self.settled("company_brief")

    def marked_ready(self, qid: str) -> None:
        self.ok(
            self.post(
                self.prep_path(f"/questions/{qid}/ready"),
                {"version": str(self.item_version(qid)), "ready": "1"},
            )
        )

    def claimed(self, qid: str) -> None:
        """Practise ``qid`` with an answer that claims two things no fact states, then add the
        first claim as a draft fact."""
        self.practised(qid, TWO_CLAIMS, "You answer the question directly.")
        attempt = self.run.app.state.answer_practice.view(self.aid, qid).attempts[0]
        self.ok(
            self.post(
                self.practice_path(qid, f"/claims/{attempt.id}"),
                {"claim": "0", "version": str(attempt.version)},
            )
        )

    def card_version(self, kind: str) -> str:
        """The current version id of the ``kind`` card."""
        cards = self.run.app.state.prep_documents.view(self.aid).cards
        return str(next(c for c in cards if c.kind == kind).version.id)

    def card_ids(self, kind: str) -> tuple[str, str]:
        """The current version id of the ``kind`` card and the id of its first unit."""
        cards = self.run.app.state.prep_documents.view(self.aid).cards
        card = next(c for c in cards if c.kind == kind)
        return str(card.version.id), str(card.units[0].id)

    def adapted_ids(self, aid: str) -> tuple[str, str]:
        """The latest adapted answer's version id in ``aid`` and the id of its first unit."""
        found = self.run.rows(
            "SELECT v.id AS version, u.id AS unit FROM prep_document_versions v "
            "JOIN prep_documents d ON d.id=v.document_id "
            "JOIN prep_units u ON u.document_version_id=v.id "
            "WHERE d.kind='adapted_answer' AND d.application_id=? "
            "ORDER BY v.number DESC, u.position",
            (aid,),
        )
        assert found, "an adapted answer exists"
        return str(found[0]["version"]), str(found[0]["unit"])

    def card_edited_and_approved(self, kind: str, text: str) -> None:
        version, unit = self.card_ids(kind)
        self.ok(
            self.post(
                self.cards_path(f"/{kind}/units/{unit}/edit"), {"version": version, "text": text}
            )
        )
        version, _ = self.card_ids(kind)  # an edit makes a new version
        self.ok(self.post(self.cards_path(f"/{kind}/approve"), {"version": version}))

    def dismissed(self, event: str) -> None:
        self.ok(
            self.post(
                self.debrief_path(event, "/dismiss"),
                {"version": str(self.debrief_version(event))},
            )
        )

    def debriefed(
        self, event: str, *, questions: str, how: str, experience: str, draft: str
    ) -> None:
        self.ok(
            self.save_debrief(
                event,
                questions=questions,
                how_it_went=how,
                experiences=experience,
                company_only="1",
                next_step="next_round",
                next_date="2026-10-27",
                next_time="10:00",
                next_zone="Europe/Lisbon",
            )
        )
        self.script("structure_answer", data=payload(text=draft, skills=[]))
        self.ok(self.start_drafts(event))
        self.settled("debrief_facts")

    # --- forced failures ---------------------------------------------------------------------

    def fail_start(self, kind: str, text: str) -> tuple[Any, str]:
        """Start ``kind`` with a malformed answer scripted and return (the response, the task
        type). ``text`` is a person's typed text the start carries."""
        everything = " ".join(MARKERS)
        malformed = f"not json {everything} {{"
        if kind == "debrief_facts":
            self.script("structure_answer", raw_text=malformed)
            event = self.events[1]
            self.ok(
                self.save_debrief(
                    event,
                    questions="",
                    how_it_went=f"It felt calm {HOW_MARKER}.",
                    experiences=f"I planned delivery routes and kept a log {EXPERIENCE_MARKER}.",
                )
            )
            return self.start_drafts(event), kind
        self.script(kind, raw_text=malformed)
        if kind == "interview_questions":
            return self.start_questions("more"), kind
        if kind == "interview_web_questions":
            return self.start_web(), kind
        if kind == "answer_feedback":
            qid = self.first_question()
            return self.start_practice(qid, text), kind
        if kind == "draft_narrative":
            return self.start_card("why_this_role"), kind
        if kind == "adapt_answer":
            qid = self.first_question()
            return self.start_adapt(qid, self.third), kind
        assert kind == "company_brief", kind
        return self.start_brief(), kind

    def first_question(self) -> str:
        found = self.run.rows("SELECT id FROM bank_questions ORDER BY created_at, id")
        assert found, "the bank has a question"
        return str(found[0]["id"])

    def failed_view(self, kind: str) -> str:
        """The address of the fragment that shows the latest failure of ``kind``."""
        qid = self.first_question()
        return {
            "interview_questions": self.prep_path(),
            "interview_web_questions": self.prep_path(),
            "answer_feedback": self.practice_path(qid),
            "draft_narrative": self.cards_path(),
            "adapt_answer": self.practice_path(qid, aid=self.third),
            "company_brief": self.brief_path(),
            "debrief_facts": self.debrief_path(self.events[1]),
        }[kind]


def run_failures(
    w: World, monkeypatch: pytest.MonkeyPatch, on_page: Callable[[str, Any], None]
) -> None:
    """Every M8 task fails (a malformed answer), then is retried through the Tasks route with a
    provider that raises with every marker in its detail and in a chained cause, an unexpected
    exception and a timeout. ``on_page`` is shown each response a person would see."""
    run = w.run
    everything = " ".join(MARKERS)
    original = run.spy.run

    async def network(request: object, cancel: object) -> object:
        raise NetworkUnavailable(detail=everything) from RuntimeError(everything)

    async def unexpected(request: object, cancel: object) -> object:
        raise RuntimeError(f"unexpected {everything}")

    async def timeout(request: object, cancel: object) -> object:
        raise TimeoutError(f"timeout {everything}")

    for kind in FAILING:
        response, task_type = w.fail_start(kind, ANSWER_MARKER)
        assert response.status_code == 200, (kind, response.status_code, response.text[:200])
        on_page(f"{kind} start", response)
        row = w.settled(task_type, "failed")
        assert row.error, kind
        task_id = w.task_row(task_type)
        shown = w.get(w.failed_view(kind))
        assert shown.status_code == 200, (kind, shown.status_code)
        on_page(f"{kind} failed view", shown)
        for behaviour in (network, unexpected, timeout):
            monkeypatch.setattr(run.spy, "run", behaviour)
            retried = w.post(f"/tasks/{task_id}/retry")
            assert retried.status_code == 200, (kind, retried.status_code, retried.text[:200])
            on_page(f"{kind} retry", retried)
            assert str(run.wait_terminal(task_id).status.value) == "failed", kind
            on_page(f"{kind} failed view", w.get(w.failed_view(kind)))
        monkeypatch.setattr(run.spy, "run", original)
    for path in ("/tasks/panel", "/tasks/indicator"):
        on_page(path, w.get(path))


# --- AC-M1.4 for interview-preparation text ----------------------------------------------------


def test_prep_text_never_in_logs_task_rows_usage_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("app.main.UuidIdFactory", SequentialIdFactory),
        watched(tmp_path, monkeypatch, capsys) as watch,
    ):
        run = watch.run
        r = Refining(run)
        w = World.build(r)
        surfaces = watch.surfaces

        def shown(where: str, response: Any) -> None:
            add_response(surfaces, where, response)

        w.recorder = shown  # every response of every flow below is scanned, not only some
        portal, opened = open_events(watch)
        with opened as stream:
            frames = [portal.call(stream.next_chunk)]

            # 1. Questions: generated, searched for online, filtered and listed.
            question = f"Tell me about a time you handled {BANK_MARKER}."
            qid = w.generated_question(question)
            w.web_question("Describe a fictional online-reported challenge.", WEB_URL)
            # A marker in a query string is not used here: the in-process client logs its own
            # requests. The real CLI access log is checked by the query-string test below.
            for path in (
                w.prep_path(),
                w.prep_path("?q=challenge&type=behavioral"),
                w.prep_path("?focus=1"),
            ):
                w.ok(w.get(path))
            assert BANK_MARKER in w.get(w.prep_path()).text

            # 2. Practice: an answer typed by the person and the feedback written for it.
            answer = f"I planned 60 delivery routes with a team of 3, a log {ANSWER_MARKER}."
            w.practised(qid, answer, f"You answer the question directly {FEEDBACK_MARKER}.")
            view = w.ok(w.get(w.practice_path(qid)))
            assert ANSWER_MARKER in view.text and FEEDBACK_MARKER in view.text

            # The small actions: mark the question ready, add a claim as a draft fact.
            w.marked_ready(qid)
            w.run.clock.advance(timedelta(minutes=1))
            claims_qid = str(
                w.run.app.state.prep.add_bank_question(
                    "Describe a time you managed several sites.", "behavioral"
                ).id
            )
            w.claimed(claims_qid)

            # 3. A narrative card and an adapted answer in another application.
            w.card(
                "tell_me_about_yourself",
                f"I planned 60 delivery routes a week with a team of 3, {NARRATIVE_MARKER}.",
            )
            w.ok(w.get(w.cards_path()))
            # Edit a card's paragraph and approve it (another card, so the narrative stays).
            w.card("strengths", FACT_1)
            w.card_edited_and_approved("strengths", KEPT_TEXT)
            w.adapted(
                qid,
                w.other,
                f"I planned 60 delivery routes a week with a team of 3, {ADAPTED_MARKER}.",
            )
            w.ok(w.get(w.practice_path(qid, aid=w.other)))

            # 4. The company brief, with a source link.
            w.briefed(f"Plans delivery routes for small depots ({BRIEF_MARKER}).", BRIEF_URL)
            brief_view = w.ok(w.get(w.brief_path()))
            assert BRIEF_MARKER in brief_view.text and BRIEFURL_MARKER in brief_view.text
            shown("company page", w.ok(run.client.get(f"/companies/{w.cid}")))

            # 5. The debrief: Today offers it, the questions asked, how it went and an experience
            #    are kept, draft facts are made from the experience only, the status is moved.
            w.ok(w.get("/today/content"))
            first = w.events[0]
            w.debriefed(
                first,
                questions=f"Walk me through {ASKED_MARKER} work.\nHow do you handle a delay?",
                how=f"It felt calm {HOW_MARKER}.",
                experience=f"I planned delivery routes and kept a log {EXPERIENCE_MARKER}.",
                draft=f"Planned delivery routes and kept a log {DRAFT_MARKER}.",
            )
            page = w.ok(w.get(w.debrief_path(first)))
            for marker in (ASKED_MARKER, HOW_MARKER, EXPERIENCE_MARKER, DRAFT_MARKER):
                assert marker in page.text, f"the debrief shows {marker} back to the person"
            moved = w.post(w.debrief_path(first, "/status"), {"version": str(w.app_version())})
            assert moved.status_code == 200, moved.text[:300]
            w.ok(w.get(f"/applications/{w.aid}/detail"))
            w.dismissed(w.spare)

            # 6. Failures, for each task, retried through the Tasks route.
            run_failures(w, monkeypatch, shown)

            # 7. The diagnostic screens, and an unexpected error while each M8 screen is drawn
            #    (the body the person gets is what is scanned: the in-process client does not go
            #    through uvicorn's logger).
            for path in (
                "/tasks/panel",
                "/tasks/indicator",
                "/settings/usage",
                "/health",
                "/settings/testing-tools",
            ):
                diagnostic = w.ok(w.get(path))
                surfaces.add(f"{path} body", diagnostic.text)
                surfaces.add_headers(path, diagnostic)
            everything = " ".join(MARKERS)

            def broken(*args: object, **kwargs: object) -> object:
                raise RuntimeError(f"screen {everything}")

            for service, name, path in (
                ("prep", "view", w.prep_path()),
                ("answer_practice", "view", w.practice_path(qid)),
                ("prep_documents", "view", w.cards_path()),
                ("company_brief", "view", w.brief_path()),
                ("debrief", "view", w.debrief_path(first)),
            ):
                with monkeypatch.context() as patched:
                    patched.setattr(getattr(run.app.state, service), name, broken)
                    crashed = w.get(path)
                assert crashed.status_code == 500, (path, crashed.status_code)
                surfaces.add(f"unexpected error body {service}", crashed.text)
                surfaces.add_headers(f"unexpected error {service}", crashed)

            # 8. The stream, through the last task.
            states = final_states(run)
            assert list(states.values()).count("failed") >= len(FAILING), states
            frames.append(read_until_final(portal, stream, states))
            surfaces.add("event stream headers", repr(stream.headers))
            surfaces.add("event stream frames", b"".join(frames).decode("utf-8", "replace"))

        # Control: the flows stored every marker, and only where it belongs.
        for marker in MARKERS:
            assert_stored_only_in(run, marker, ALLOWED[marker])
        assert "practice_attempts" in tables_containing(run, ANSWER_MARKER)
        assert "practice_attempts" in tables_containing(run, FEEDBACK_MARKER)
        assert "debriefs" in tables_containing(run, HOW_MARKER)
        assert "company_briefs" in tables_containing(run, BRIEF_MARKER)
        kinds = {r["type"] for r in run.rows("SELECT type FROM tasks WHERE status='failed'")}
        assert kinds == set(FAILING), kinds
        # How it went is kept locally and never sent to the AI.
        assert run.spy.requests, "the flows made requests"
        for request in run.spy.requests:
            text = "\n".join((request.system, request.user))
            assert HOW_MARKER not in text, request.task
    surfaces.assert_clean(MARKERS)


# --- the error text a person sees --------------------------------------------------------------


def test_prep_errors_carry_no_answer_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = ANSWER_MARKER
    with refining(tmp_path) as r:
        w = World.build(r)
        qid = str(r.run.app.state.prep.add_bank_question(FAKE_QUESTION_TEXT, "behavioral").id)
        # What the probes need to exist, made from answers without a marker: a practice attempt
        # that flagged nothing, a drafted card and an adapted answer in another application.
        w.practised(qid, GROUNDED, "You answer the question directly.")
        attempt = r.run.app.state.answer_practice.view(w.aid, qid).attempts[0]
        w.card("strengths", FACT_1)
        card_version, card_unit = w.card_ids("strengths")
        w.adapted(qid, w.other, FACT_1)
        adapted_version, adapted_unit = w.adapted_ids(w.other)
        shown: list[tuple[str, Any]] = []

        def send(label: str, path: str, data: dict[str, str], expect: int) -> Any:
            response = w.post(path, data)
            assert response.status_code == expect, (
                label,
                response.status_code,
                expect,
                response.text[:200],
            )
            shown.append((label, response))
            return response

        version = str(w.prep_version())
        item = str(w.item_version(qid))
        first = w.events[0]
        save = w.debrief_path(first, "/save")
        answers = w.practice_path(qid, "/answer")
        stale_id = f"stale-{marker}"  # a safe id that is no card's or answer's version

        # Prep: an action that does not exist, a stale version, an unknown question.
        send("prep unknown action", w.prep_path(f"/{marker}"), {"version": version}, 404)
        send("prep stale version", w.prep_path("/more"), {"version": "99"}, 409)
        ready_path = w.prep_path(f"/questions/{qid}/ready")
        send("ready stale version", ready_path, {"version": "99", "ready": "1"}, 409)
        send(
            "ready unknown question",
            w.prep_path(f"/questions/missing-{marker}/ready"),
            {"version": "0", "ready": "1"},
            404,
        )
        # Practice: blank, too long, stale, an unknown question, claims that were never flagged,
        # an unknown attempt, a stale attempt.
        send("blank", answers, {"answer": "   ", "version": item}, 422)
        send("too long", answers, {"answer": marker + "x" * 4001, "version": item}, 422)
        send("stale answer", answers, {"answer": marker, "version": "99"}, 409)
        send(
            "practice unknown question",
            w.practice_path(f"missing-{marker}", "/answer"),
            {"answer": marker, "version": "0"},
            404,
        )
        claims = w.practice_path(qid, f"/claims/{attempt.id}")
        send("claims never flagged", claims, {"claim": "0", "version": str(attempt.version)}, 422)
        send("claims stale version", claims, {"claim": "0", "version": "99"}, 409)
        send(
            "claims unknown attempt",
            w.practice_path(qid, f"/claims/missing-{marker}"),
            {"claim": "0", "version": "1"},
            404,
        )
        # Narrative cards: a kind that does not exist, a stale version of a card, a unit that
        # is not in the card, an edit that is blank or too long, a card that cannot be copied.
        send("card unknown kind", w.cards_path(f"/{marker}/generate"), {"version": ""}, 404)
        send("card generate stale", w.cards_path("/strengths/generate"), {"version": stale_id}, 409)
        unit_path = w.cards_path(f"/strengths/units/{card_unit}/edit")
        send(
            "card edit unknown unit",
            w.cards_path(f"/strengths/units/missing-{marker}/edit"),
            {"version": card_version, "text": marker},
            404,
        )
        send("card edit stale version", unit_path, {"version": stale_id, "text": marker}, 409)
        send("card edit blank", unit_path, {"version": card_version, "text": "  "}, 422)
        send(
            "card edit too long",
            unit_path,
            {"version": card_version, "text": marker + "x" * 1300},
            422,
        )
        send("card approve stale", w.cards_path("/strengths/approve"), {"version": stale_id}, 409)
        send(
            "card use refused",
            w.cards_path("/why_this_role/use"),
            {"version": "", "source": marker},
            422,
        )
        # A card whose only paragraph was removed has nothing to approve.
        w.ok(
            w.post(w.cards_path(f"/strengths/units/{card_unit}/remove"), {"version": card_version})
        )
        emptied = w.card_version("strengths")
        send("card approve nothing", w.cards_path("/strengths/approve"), {"version": emptied}, 409)
        # Adapted answers: the answer changed since the page loaded, a version that is not the
        # current one, a unit that is not in it, an edit that is too long.
        send(
            "adapt version not current",
            w.practice_path(qid, "/adapt", w.other),
            {"version": stale_id},
            409,
        )
        send(
            "adapted approve unknown version",
            w.practice_path(qid, f"/adapted/missing-{marker}/approve", w.other),
            {},
            409,
        )
        send(
            "adapted edit unknown version",
            w.practice_path(qid, f"/adapted/missing-{marker}/units/{adapted_unit}/edit", w.other),
            {"text": marker},
            409,
        )
        adapted = f"/adapted/{adapted_version}/units"
        send(
            "adapted edit unknown unit",
            w.practice_path(qid, f"{adapted}/missing-{marker}/edit", w.other),
            {"text": marker},
            404,
        )
        send(
            "adapted edit too long",
            w.practice_path(qid, f"{adapted}/{adapted_unit}/edit", w.other),
            {"text": marker + "x" * 1300},
            422,
        )
        # The debrief: too long, an unknown zone, date and next step, a stale version, steps
        # before a saved debrief, an unknown event.
        send("debrief too long", save, {"version": "0", "questions": marker * 400}, 422)
        send(
            "debrief zone",
            save,
            {
                "version": "0",
                "next_step": "next_round",
                "next_date": "2026-10-27",
                "next_time": "10:00",
                "next_zone": marker,
            },
            422,
        )
        send(
            "debrief date",
            save,
            {
                "version": "0",
                "next_step": "next_round",
                "next_date": marker,
                "next_time": "10:00",
                "next_zone": "UTC",
            },
            422,
        )
        send("debrief step", save, {"version": "0", "next_step": marker}, 422)
        send("debrief stale", save, {"version": "99", "questions": marker}, 409)
        send("debrief drafts early", w.debrief_path(first, "/drafts"), {"version": "0"}, 409)
        send("debrief status early", w.debrief_path(first, "/status"), {"version": "1"}, 409)
        send("debrief dismiss stale", w.debrief_path(first, "/dismiss"), {"version": "99"}, 409)
        send(
            "debrief unknown event",
            w.debrief_path(f"missing-{marker}", "/save"),
            {"version": "0", "questions": marker},
            404,
        )
        # The company brief.
        send("brief stale", w.brief_path(), {"version": "99"}, 409)
        send("brief unknown company", f"/companies/missing-{marker}/brief", {"version": "1"}, 404)

        # The refusals of a malformed body (a field that is not part of the form, a version that
        # is no number, a value outside its choices) are all the same 400, before any service
        # is asked: a separate group, named as such.
        for label, path, data in (
            ("prep bad version", w.prep_path("/more"), {"version": marker}),
            ("prep unknown field", w.prep_path("/more"), {"version": version, marker: marker}),
            ("ready bad value", ready_path, {"version": "0", "ready": marker}),
            ("practice bad version", answers, {"answer": marker, "version": marker}),
            ("practice unknown field", answers, {"answer": marker, "version": "0", marker: marker}),
            ("claims not a number", claims, {"claim": marker, "version": "1"}),
            ("card approve no version", w.cards_path("/strengths/approve"), {"version": ""}),
            (
                "adapted edit no text",
                w.practice_path(qid, f"{adapted}/{adapted_unit}/edit", w.other),
                {},
            ),
            ("debrief unknown field", save, {"version": "0", marker: marker}),
            ("brief bad version", w.brief_path(), {"version": marker}),
            ("brief unknown field", w.brief_path(), {"version": "1", marker: marker}),
        ):
            send(f"malformed: {label}", path, data, 400)

        # An unexpected error from a service, with the text in its message and cause.
        def explode(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"service {marker}") from ValueError(marker)

        state = r.run.app.state
        for service, names, path, data in (
            ("prep", ("plan_start", "start_questions"), w.prep_path("/more"), {"version": version}),
            (
                "answer_practice",
                ("plan_start", "submit"),
                answers,
                {"answer": marker, "version": item},
            ),
            (
                "prep_documents",
                ("plan_start", "start_card"),
                w.cards_path("/tell_me_about_yourself/generate"),
                {"version": ""},
            ),
            ("debrief", ("save", "dismiss"), save, {"version": "0", "questions": marker}),
            ("company_brief", ("plan_start", "start"), w.brief_path(), {"version": "1"}),
        ):
            with monkeypatch.context() as patched:
                for name in names:
                    patched.setattr(getattr(state, service), name, explode)
                crashed = w.post(path, data)
            assert crashed.status_code == 500, (service, crashed.status_code)
            shown.append((f"unexpected {service}", crashed))

        leaked = [
            f"{label}: {line[:80]!r}"
            for label, response in shown
            for line in messages_of(response)
            if marker in line
        ]
        assert not leaked, "answer text in an error message:\n" + "\n".join(leaked)

        # Control: forms do redisplay what was typed in their own field, so a scan of the whole
        # body would have found it; only the messages are free of it.
        redisplayed = [r for label, r in shown if label in {"blank", "too long", "stale answer"}]
        assert any(marker in r.text for r in redisplayed), "the answer is shown back in its field"
        assert all(status_lines(r.text) for r in redisplayed if r.status_code in (409, 422))
        typed = [r for label, r in shown if label in {"debrief too long", "debrief stale"}]
        assert any(marker in r.text for r in typed), "the debrief form keeps what was typed"

        # Failed tasks: the message a person sees for each, whatever the provider said.
        failures: list[tuple[str, Any]] = []
        run_failures(w, monkeypatch, lambda label, response: failures.append((label, response)))
        assert len(failures) > len(FAILING) * 4
        leaked = [
            f"{label}: {line[:80]!r}"
            for label, response in failures
            for line in messages_of(response)
            if any(m in line for m in MARKERS)
        ]
        assert not leaked, "text in a failure message:\n" + "\n".join(leaked)
        alerts = [
            line for label, r in failures if "failed view" in label for line in status_lines(r.text)
        ]
        assert alerts, "a failed task shows a message"

    # The same for the errors the services raise themselves: no answer text in str, repr or args.
    with refining(tmp_path / "service") as r:
        w = World.build(r)
        qid = str(r.run.app.state.prep.add_bank_question(FAKE_QUESTION_TEXT, "behavioral").id)
        raised: list[BaseException] = []

        def attempt(call: Callable[[], object]) -> None:
            try:
                call()
            except Exception as error:
                raised.append(error)

        practice = r.run.app.state.answer_practice
        for text, version in (
            (" ", 0),
            (marker * 400 + "x" * 4001, 0),
            (marker, 99),
        ):
            attempt(
                lambda text=text, version=version: practice.submit(
                    w.aid, qid, text, version=version
                )
            )
        attempt(lambda: r.run.app.state.prep.add_bank_question(marker, f"not-a-type-{marker}"))
        attempt(
            lambda: r.run.app.state.debrief.save(
                w.aid,
                w.events[0],
                DebriefEntry(
                    questions=marker * 400, how_it_went="", experiences="", company_only=False
                ),
                version=0,
            )
        )
        assert len(raised) == 5, [type(e).__name__ for e in raised]
        for error in raised:
            texts = [str(error), repr(error), *map(str, error.args), str(error.__cause__)]
            texts += [getattr(error, "user_message", ""), str(getattr(error, "detail", ""))]
            texts += [str(getattr(error, "errors", ""))]
            assert not [t for t in texts if marker in t], type(error).__name__


# --- the access log ----------------------------------------------------------------------------


def test_prep_query_strings_not_in_access_log(tmp_path: Path) -> None:
    marker = "CanaryPrepQueryHeron"  # letters only
    app = start_app(tmp_path)
    try:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        with Database.open(app.data_dir / "app.db") as db:
            seed = Seeder.make(
                db,
                _service_applications(db, clock),
                _service_statuses(db, clock),
                ticker(clock),
                stamp_after(clock),
            )
            application = seed.application("query-job", "Route Planner", COMPANY, "applied")
            aid = str(application.id)
            event = add_event(
                db, "query-event", aid, "interview", EVENT_START, EVENT_END, zone="Europe/Lisbon"
            )
            with db.transaction() as conn:
                cid = str(
                    conn.execute("SELECT id FROM companies WHERE name=?", (COMPANY,)).fetchone()[0]
                )
                conn.execute(
                    "INSERT INTO bank_questions (id,text,type,origin,created_at,updated_at) "
                    "VALUES ('query-question','Tell me about a fictional challenge.',"
                    "'behavioral','user','2026-01-05T09:00:00+00:00','2026-01-05T09:00:00+00:00')"
                )
        with app.client() as client:
            token = page_token(client.get("/").text)
            headers = {TOKEN_HEADER_NAME: token, "HX-Request": "true"}
            pages = [
                f"/applications/{aid}?tab=prep&q={marker}&type={marker}&focus={marker}",
                f"/applications/{aid}?tab=prep&{marker}={marker}",
                f"/applications/{aid}/debriefs/{event}?active={marker}&{marker}={marker}",
                f"/companies/{cid}?q={marker}&{marker}={marker}",
            ]
            for path in pages:
                response = client.get(path)
                assert response.status_code == 200, (path.split("?")[0], response.status_code)
            fragments = [
                f"/applications/{aid}/prep?q={marker}&type={marker}&focus={marker}&push={marker}",
                f"/applications/{aid}/prep?q={marker}",
                f"/applications/{aid}/prep/practice/query-question?refresh={marker}&q={marker}",
                f"/applications/{aid}/prep/cards?refresh={marker}&{marker}={marker}",
                f"/applications/{aid}/debriefs/{event}?active={marker}",
                f"/companies/{cid}/brief?focus={marker}&q={marker}",
                f"/applications/{aid}/detail?q={marker}",
                f"/today/content?q={marker}&{marker}={marker}",
            ]
            for path in fragments:
                response = client.get(path, headers=headers)
                assert response.status_code in (200, 204), (
                    path.split("?")[0],
                    response.status_code,
                )
            # Refused posts carry a query string too: the log must not.
            posts = [
                f"/applications/{aid}/debriefs/{event}/save?return={marker}&{marker}={marker}",
                f"/companies/{cid}/brief?focus={marker}",
                f"/applications/{aid}/prep/more?type={marker}",
            ]
            for path in posts:
                response = client.post(
                    path, data={"version": marker}, headers={**headers, "Origin": app.origin}
                )
                assert response.status_code == 400, (path.split("?")[0], response.status_code)
    finally:
        app.stop()
    output = app.stdout_path.read_text() + app.stderr_path.read_text()
    output += "".join(p.read_text() for p in app.log_dir.glob("*.log*"))
    assert f"GET /applications/{aid} HTTP/1.1" in output
    assert f"GET /applications/{aid}/prep HTTP/1.1" in output
    assert f"GET /applications/{aid}/debriefs/{event} HTTP/1.1" in output
    assert f"GET /applications/{aid}/prep/practice/query-question HTTP/1.1" in output
    assert f"GET /companies/{cid}/brief HTTP/1.1" in output
    assert f"POST /applications/{aid}/debriefs/{event}/save HTTP/1.1" in output
    assert marker not in output
    for fragment in (
        "?q=",
        "&q=",
        "?type=",
        "&type=",
        "?focus=",
        "&focus=",
        "?active=",
        "?refresh=",
        "&push=",
        "?return=",
        "?tab=",
        "&tab=",
    ):
        assert fragment not in output, fragment


def _service_applications(db: Database, clock: FrozenClock) -> Any:
    from app.services.applications import ApplicationService

    return ApplicationService(lambda: db, clock, SequentialIdFactory("seed"))


def _service_statuses(db: Database, clock: FrozenClock) -> Any:
    from app.services.applications import ApplicationStatuses

    return ApplicationStatuses(lambda: db, clock, SequentialIdFactory("seedstatus"))


# --- never-use facts ---------------------------------------------------------------------------

# Letters only. The never-use fact F4 also carries a skill and a role in this test, so a request
# that offered the skill vocabulary or the role of every fact would name it.
HIDDEN_SKILL = "hiddenskillquokka"
HIDDEN_EMPLOYER = "Hiddenemployerquokka"
HIDDEN_TITLE = "Hiddentitlequokka"
HIDDEN_WORDS = (
    "Brightwater",
    "Confidential fictional",
    HIDDEN_SKILL,
    HIDDEN_EMPLOYER,
    HIDDEN_TITLE,
)
ORDINARY = (FACT_1, FACT_2, FACT_3, DRAFT_TEXT)
M8_REQUESTS = (
    "interview_questions",
    "interview_web_questions",
    "answer_feedback",
    "draft_narrative",
    "adapt_answer",
    "company_brief",
    "debrief_facts",
)
FACT_FREE = {"interview_web_questions", "company_brief", "debrief_facts"}
USES_FACTS = {"answer_feedback", "draft_narrative", "adapt_answer"}


def give_hidden_fact_a_skill_and_role(r: Refining) -> None:
    """F4 (never use) gets a skill and a role of its own, through the app's own service."""
    rows = r.run.rows("SELECT id FROM facts WHERE number=4")
    fact = r.facts.get(str(rows[0]["id"]))
    r.facts.edit(
        fact.id,
        FactInput(
            text=HIDDEN,
            privacy="never_use",
            skills=(HIDDEN_SKILL,),
            new_role=NewRole(employer=HIDDEN_EMPLOYER, title=HIDDEN_TITLE),
        ),
        expected_version=fact.version,
    )
    stored = r.run.rows(
        "SELECT s.name FROM fact_skills fs JOIN skills s ON s.id=fs.skill_id WHERE fs.fact_id=?",
        (fact.id,),
    )
    assert [row["name"] for row in stored] == [HIDDEN_SKILL], "the never-use fact has the skill"
    assert r.run.rows("SELECT id FROM roles WHERE employer=?", (HIDDEN_EMPLOYER,))


def drive_for(w: World, kind: str) -> str:
    """Run one M8 flow, scripted to repeat nothing of the never-use fact, and return the AI task
    whose requests are checked (the debrief makes ``structure_answer`` requests)."""
    qid = str(w.run.app.state.prep.add_bank_question(FAKE_QUESTION_TEXT, "behavioral").id)
    if kind == "interview_questions":
        w.generated_question("Tell me about a time you led a fictional team.")
    elif kind == "interview_web_questions":
        w.web_question("Describe a fictional online-reported challenge.", WEB_URL)
    elif kind == "answer_feedback":
        w.practised(qid, FACT_1, "You answer the question directly.")
    elif kind == "draft_narrative":
        w.card("tell_me_about_yourself", FACT_1)
    elif kind == "adapt_answer":
        w.practised(qid, FACT_1, "You answer the question directly.")
        w.adapted(qid, w.other, FACT_1)
    elif kind == "company_brief":
        w.briefed("Plans delivery routes for small depots.", BRIEF_URL)
    else:
        w.debriefed(
            w.events[0],
            questions="Walk me through a route you planned.",
            how="It felt calm.",
            experience="I planned delivery routes and kept a log.",
            draft="Planned delivery routes and kept a log.",
        )
        return "structure_answer"
    return kind


@pytest.mark.parametrize("kind", M8_REQUESTS)
def test_never_use_facts_absent_from_every_m8_request(tmp_path: Path, kind: str) -> None:
    with refining(tmp_path) as r:
        w = World.build(r)
        give_hidden_fact_a_skill_and_role(r)
        hidden = r.run.rows("SELECT id, number FROM facts WHERE number=4")[0]
        assert HIDDEN in {row["text"] for row in r.run.rows("SELECT text FROM fact_versions")}
        task = drive_for(w, kind)
        requests = [q for q in r.run.spy.requests if q.task == task]
        assert requests, f"{kind} made no {task} request"
        ids = [str(row["id"]) for row in r.run.rows("SELECT id FROM facts")]
        for request in requests:
            found = never_use_problems(request, HIDDEN, HIDDEN_WORDS, str(hidden["id"]))
            assert not found, f"{task} request holds a never-use fact: {found}"
            text = "\n".join((request.system, request.user, json.dumps(request.output_schema)))
            if kind in FACT_FREE:
                # No fact text, no reference to one and no fact id at all.
                assert not [f for f in ORDINARY if f in text], f"{task} holds fact text"
                assert not re.search(r"\bF\d+@", text), f"{task} holds a fact reference"
                assert not [i for i in ids if i in text], f"{task} holds a fact id"
        if kind in USES_FACTS:
            assert any("F1@" in q.user for q in requests), "ordinary facts are used (the control)"


# --- performance -------------------------------------------------------------------------------

QUESTION_SUBJECTS = (
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
)
QUESTION_STYLES = (
    "Tell me about a time you improved {}.",
    "How would you approach {} in a new depot?",
    "What does good {} look like to you?",
    "Describe a mistake you made with {} and what changed.",
    "Walk me through how you would measure {}.",
)
STAMP = "2026-10-01T09:00:00+00:00"
REPORT_ONLY = "never"


def seed_bank(db: Database, application: str, company: str, requirement_texts: list[str]) -> None:
    """1,000 bank questions with requirement tags and a sighting each, by SQL in one transaction.
    A tenth are scoped to the first company; every fifth has several tags."""
    with db.transaction() as conn:
        for n in range(1000):
            subject = QUESTION_SUBJECTS[n % len(QUESTION_SUBJECTS)]
            text = QUESTION_STYLES[n % len(QUESTION_STYLES)].format(subject) + f" (case {n})"
            qid = f"scale-q-{n:04d}"
            conn.execute(
                "INSERT INTO bank_questions (id,text,type,origin,company_id,asked_count,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    qid,
                    text,
                    ("behavioral", "technical", "cv")[n % 3],
                    ("generated", "asked", "user")[n % 3],
                    company if n % 10 == 0 else None,
                    n % 4,
                    STAMP,
                    STAMP,
                ),
            )
            tags = [requirement_texts[n % len(requirement_texts)]]
            if n % 5 == 0:
                tags.append(f"Five years of {subject}")
            for tag in dict.fromkeys(tags):  # the same tag twice would be a duplicate row
                conn.execute(
                    "INSERT INTO bank_question_requirements "
                    "(question_id,text,created_at,updated_at) VALUES (?,?,?,?)",
                    (qid, tag, STAMP, STAMP),
                )
            conn.execute(
                "INSERT INTO bank_question_sightings (id,question_id,application_id,origin,"
                "found_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (f"scale-s-{n:04d}", qid, application, "generated", STAMP, STAMP, STAMP),
            )
        for n in range(0, 600, 30):  # twenty questions the first application is preparing
            conn.execute(
                "INSERT INTO prep_items (id,application_id,question_id,ready,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (f"scale-i-{n:04d}", application, f"scale-q-{n:04d}", n % 60 == 0, STAMP, STAMP),
            )


@pytest.mark.slow
def test_ac_m8_fragments_under_150ms(tmp_path: Path, record_property: Any) -> None:
    with refining(tmp_path) as r:
        tw = Tailoring(r)
        first = tw.ready("scale-measured-job")
        aid = str(first.id)
        statuses = ("preparing", "applied", "screening", "interviewing", "rejected")
        for n in range(499):  # 500 applications in all, at 40 companies
            tw.seed_application(
                f"scale-job-{n}",
                f"Fictional Role {n}",
                f"Fictional Company {n % 40}",
                statuses[n % len(statuses)],
            )
        seed_scale(r, jobs=0, facts=300)
        db = r.run.app.state.workspace.db
        assert r.run.rows("SELECT count(*) AS n FROM applications")[0]["n"] == 500
        assert r.run.rows("SELECT count(*) AS n FROM facts")[0]["n"] >= 300
        cid = company_id(r)
        requirements = [
            row["text"] for row in r.run.rows("SELECT text FROM job_requirements ORDER BY position")
        ]
        seed_bank(db, aid, cid, requirements)
        assert r.run.rows("SELECT count(*) AS n FROM bank_questions")[0]["n"] == 1000
        assert r.run.rows("SELECT count(*) AS n FROM bank_question_sightings")[0]["n"] == 1000
        event = add_event(db, "scale-event", aid, "interview", EVENT_START, EVENT_END, zone="UTC")
        r.run.clock.set(datetime.fromisoformat(NOW))
        r.run.execute(
            "UPDATE companies SET website=?, industry=? WHERE id=?", (WEBSITE, INDUSTRY, cid)
        )
        client, htmx = r.client, r.run.run.htmx()
        results: dict[str, float] = {}
        paths: dict[str, Callable[[], Any]] = {
            "GET /applications/{id}/prep": lambda: client.get(
                f"/applications/{aid}/prep", headers=htmx
            ),
            "GET /applications/{id}/prep?q=": lambda: client.get(
                f"/applications/{aid}/prep?q=route&type=behavioral", headers=htmx
            ),
            "GET /board/columns": lambda: client.get("/board/columns", headers=htmx),
            "GET /today/content": lambda: client.get("/today/content", headers=htmx),
            "GET /applications/{id}/detail": lambda: client.get(
                f"/applications/{aid}/detail", headers=htmx
            ),
            "GET /applications/{id}/debriefs/{event}": lambda: client.get(
                base(aid, event), headers=htmx
            ),
            "GET /companies/{id}": lambda: client.get(f"/companies/{cid}"),
        }
        probe = paths["GET /today/content"]()
        assert probe.status_code == 200 and f'data-debrief-event-id="{event}"' in probe.text, (
            "Today offers the debrief"
        )
        # What each answer must hold, so a fast error page or an empty shell cannot pass.
        holds = {
            "GET /applications/{id}/prep": "prep-question",
            "GET /applications/{id}/prep?q=": "prep-question",
            "GET /board/columns": f'data-application-id="{aid}"',
            "GET /today/content": f'data-debrief-event-id="{event}"',
            "GET /applications/{id}/detail": "Route Planner",
            "GET /applications/{id}/debriefs/{event}": "debrief-form",
            "GET /companies/{id}": COMPANY,
        }
        for label, request in paths.items():
            needle = holds[label]
            measure(
                results,
                label,
                request,
                lambda response, needle=needle: (
                    response.status_code == 200 and needle in response.text
                ),
            )
        for label, seconds in results.items():
            record_property(label, round(seconds * 1000, 2))
        report = ", ".join(f"{label}: {round(s * 1000, 1)} ms" for label, s in results.items())
        print(f"M8 150 ms medians (median of 5): {report}")
        slow = {label: round(s * 1000, 2) for label, s in results.items() if s >= BUDGET_SECONDS}
        assert not slow, f"over the {BUDGET_SECONDS * 1000:.0f} ms budget (median of 5): {slow}"
