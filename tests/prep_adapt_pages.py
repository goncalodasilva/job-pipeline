"""The running app, assumed interface, markup hooks and readers for the M8.2 part C tests of
adapting an earlier answer (``adapt_answer``). Every value is fictional. No application module of
part C is imported at module level, so a missing service, task or route fails the test that needs
it, not the collection.

THE FEATURE. A practice answer the person wrote for a bank question in ANOTHER application can be
adapted to this one. The practice screen of a question (``GET /applications/{aid}/prep/practice/
{qid}``) shows the best earlier answer under "From your earlier answer" and a button "Adapt to
this role". The AI task ``adapt_answer`` rewrites the old answer from the person's confirmed
facts for this job; the result is a document of kind ``adapted_answer`` that is reviewed, edited
and approved exactly as a narrative card is (``tests/prep_card_pages.py``): the same tables
(``prep_documents`` with ``question_id`` set, ``prep_document_versions``, ``prep_units``,
``prep_unit_facts``, ``prep_version_facts``), the same version rules, the same honesty checks
(``check_units`` against the facts SENT for the version plus the facts the person saved from
documents, with the TARGET application's job title and company as the only name sources) and the
same approval rule (``approval_blockers`` with the facts as they are now). The old answer is DATA:
it may name another employer, a number or a skill no confirmed fact states, and what the model
copies from it is flagged and blocks approval.

ASSUMED INTERFACE (the implementer follows it)

Which earlier answer. ``practice_repo.best_answer(conn, question_id, exclude_application)`` (any
equivalent; the tests read it through ``adapted_view().earlier``): the LATEST attempt (by
``practice_attempts.created_at``, then id) among the attempts of the question's prep items in
OTHER applications whose item is marked ready; when no such item has an attempt, the latest
attempt among all other applications' items. Never an attempt of the application being viewed,
never an attempt of another question. It returns the answer text and its application-independent
provenance (``practiced_at``: the attempt's ``created_at``; ``ready``: whether its item is marked
ready) and nothing else: no application id, job title, company or attempt id.

Task and wiring
- Prompt ``prompts/adapt_answer`` (see ``tests/unit/modules/ai/test_adapt_answer_prompt.py``) and
  ``TaskSpec("adapt_answer", kind="generation")``. Task type ``adapt_answer``, target
  ``<application id>/<question id>``, so the dedupe key is ``adapt_answer:<aid>/<qid>``, label
  "Adapt answer" (the Tasks indicator says "Adapt answer finished."). The whole result is written
  by the writer ``ctx.complete`` runs, in the transaction that marks the task succeeded, or not at
  all. The request holds: ``old_answer``, ``question``, the sent facts (``facts_for_ai`` only: no
  never-use, draft or archived fact), the chosen angle (empty when none) and ``job_context``
  (the target job's title, company, posting and requirements), each ONE untrusted block, and
  nothing else (no personal detail, nothing about pay, no CV text).
- ``app.state.prep_documents`` (the existing ``PrepDocumentService``) gains the methods below.
  They raise the exception classes of ``app.services.documents`` (``GenerateNotReady``,
  ``NothingToApprove``, ``ApprovalBlocked`` with the noun "paragraph", ``UnitInvalid``,
  ``UnitNotFound``), ``VersionConflict`` and ``ApplicationNotFound`` of ``app.domain.errors``.
  ``ApplicationNotFound`` is raised for an unknown or unsafe application id, an unknown or unsafe
  question id and a question outside the application's prep scope (a question of another
  company); every method takes ``application_id`` then ``question_id``.

``PrepDocumentService`` (blocking)
- ``adapted_view(aid, qid) -> AdaptedView`` reads only (nothing is written, started or created):
  ``application_id``; ``question_id``; ``earlier`` (None, or an object with ``text``,
  ``practiced_at`` and ``ready``); ``can_adapt`` (True only when ``earlier`` exists AND a usable
  confirmed fact exists AND the adapted answer is not approved-without-blocker; False
  otherwise); ``answer``, a ``CardView`` of ``app.services.prep_documents`` with ``kind``
  "adapted_answer", ``label`` "Adapted answer" and every field of a card (``state`` one of
  ``none|running|failed|draft|approved|recheck``, ``version`` (None or ``id``, ``number``,
  ``status``, ``origin``, ``provider``, ``reused_version_id``), ``units`` (visible units, each
  with ``id``, ``text``, ``origin``, ``flags``, ``fact_ids``, ``added_details``, ``cited``),
  ``blockers`` (``unit_id``, ``kind``, ``claim``: stored flags plus the approval check re-run with
  the facts as they are NOW, for approved versions too), ``approved``, ``needs_recheck``,
  ``task``, ``practice``, ``empty``).
- ``plan_adapt(aid, qid, tier=None) -> SummaryPlan`` (what the route asks the budget gate;
  service ``fake`` and tier ``thorough`` by default; ``ValueError`` for a tier that is not fast
  or thorough; ``ProviderNotConfigured`` without a provider; ``GenerateNotReady`` as below).
- ``start_adapt(aid, qid, *, version=None, tier=None) -> TaskRef``. ``version`` is the id of the
  adapted answer's current version as last seen (None while it has none): ``VersionConflict``
  otherwise. ``GenerateNotReady`` with reason ``answer`` (a non-empty message) when there is no
  earlier answer, with reason ``facts`` (message ``app.services.tailoring.NO_FACTS``) when no
  usable confirmed fact exists: no task, no request. A task already queued or running for the
  same application and question is returned. The service does not ask the budget gate.
- The written version: ``origin`` ``generated``, ``status`` ``draft``, ``number`` next, parent the
  previous current version (None for the first), provider, model, ``prompt_id`` ``adapt_answer``,
  ``prompt_version`` ``2``, the chosen angle's ``angle_id`` (NULL when none) and ``angle_text``;
  units with section ``answer``, origin ``ai``, no role, the flags of ``check_units``, the
  model's ``added_details`` and one ``prep_unit_facts`` row per cited fact version that was sent;
  one ``prep_version_facts`` row per fact version SENT. The document row has ``kind``
  ``adapted_answer``, this application and ``question_id`` = the question; there is one document
  per application and question.
- ``edit_adapted(aid, qid, version_id, unit_id, text, *, fact_ids=None, added_details=None) ->
  CardEdit``, ``remove_adapted(aid, qid, version_id, unit_id) -> CardEdit``,
  ``confirm_adapted(aid, qid, version_id, unit_id) -> CardEdit``, ``approve_adapted(aid, qid,
  version_id) -> None``: exactly as ``edit_unit``, ``remove_unit_outcome``,
  ``resolve_flag_confirm`` and ``approve`` of a card (every change is a NEW draft version, the
  parent's sent facts are copied, a line is re-checked against the facts SENT for that version
  plus the facts the person saved from documents, with this application's title and company as
  name sources; a removed unit stays as a tombstone; a confirmation creates a CONFIRMED fact with
  source ``document`` in the same transaction as the new version; approval re-runs
  ``approval_blockers`` with the facts as they are now). ``CardEdit.offer.question`` is
  ``SAVE_AS_FACT``.

Routes (inside the ``#prep`` swap: every one answers the practice fragment, the whole
``section#prep``). GET needs the token; POSTs need token and origin. ``{p}`` is
``/applications/{aid}/prep/practice/{qid}``.
- ``POST {p}/adapt`` (``version``, ``tier`` and the budget dialog's fields): announces "Adapting
  your earlier answer."; refused first by the budget gate (409 with the reason ``p#adapt-reason``,
  or 200 with ``dialog#adapt-budget-dialog``); ``GenerateNotReady`` is 409 with its message.
- ``POST {p}/adapted/{version_id}/units/{uid}/edit`` (``text``): "Paragraph saved."; with a new
  claim "Paragraph saved. Save this as a fact?"; from an approved version "Saved as a new draft.
  The approved version is unchanged."; ``UnitInvalid`` is 422 with its message.
- ``POST {p}/adapted/{version_id}/units/{uid}/remove``: "Paragraph removed.".
- ``POST {p}/adapted/{version_id}/units/{uid}/confirm``: "Saved as a fact. The paragraph now cites
  it.".
- ``POST {p}/adapted/{version_id}/approve``: "Answer approved."; ``ApprovalBlocked`` and
  ``NothingToApprove`` are 409 with the exception message.
  For these, the version is in the path: a ``version_id`` that is not a safe id, an unknown
  application, question outside the scope, or an unsafe/unknown unit id is the fixed 404; a safe
  id that is not the current version is 409 with the reload prompt (``Reload``); a missing field
  (no ``text`` for an edit), or any extra or repeated field is 400.

Markup of ``GET {p}`` (inside the practice fragment)
- Nothing below exists when there is no earlier answer and no adapted version.
- ``section#adapt[data-adapt][data-state=<state>]`` (the state of ``answer``) with
  ``h4#adapt-heading[tabindex=-1]`` "From your earlier answer"; after an adapt action it carries
  ``data-autofocus`` and ``#practice-heading`` does not.
- ``[data-earlier-answer]#adapt-earlier``: the old answer, escaped, whole; ``p#adapt-earlier-meta``
  "Practiced on <date>." (a ``<time>``) plus "Marked ready when you practiced it." when ready.
  No other application's name or id.
- ``form#adapt-form`` (hidden ``version``, the adapted answer's current version id, "" while it
  has none) with ``button#adapt-submit`` "Adapt to this role" ("Adapt again" once a version
  exists), only when ``can_adapt``; when the budget blocks it the button has
  ``aria-disabled="true"`` and ``aria-describedby`` naming ``p#adapt-reason``. The note
  ``p#adapt-hint`` says it uses AI and that the whole earlier answer is sent as written,
  including any employer name in it (with a draft: that adapting again writes a new draft and
  does not carry the edits).
- Without a usable confirmed fact (the earlier answer is still shown, there is no form and no
  ``#adapt-submit``): ``p#adapt-empty`` "No confirmed facts to adapt it with yet.", a plain
  ``button#adapt-unavailable`` (``type=button``, ``aria-disabled="true"``,
  ``aria-describedby="adapt-empty"``) and ``a#adapt-sessions`` ("Answer questions in a session",
  ``href="/sessions/new"``).
- Review fixes (``tests/integration/test_prep_adapt_review.py``): the section follows the
  practice form and precedes "Your tries"; every form inside ``#adapt`` has ``hx-target="#adapt"``
  and ``hx-select="#adapt"`` (a half-typed practice answer and an open Edit elsewhere survive; the
  open Edit is put back by ``app/static/js/prep.js``, matching paragraphs by text); the message,
  ``p#adapt-alert[role=alert]``, the reload prompt ``#adapt-conflict`` ("Nothing was started."
  for a stale Adapt, "Your change was not saved." for an edit, "Nothing was changed." for the
  rest), the budget reason and ``dialog#adapt-budget-dialog`` are all inside the section (only
  with no section does the alert stand on the page); labels ``h5#adapt-earlier-label`` "Your
  earlier answer" and ``h5#adapt-units-heading`` "Adapted for this role" (the ``ol`` is
  ``aria-labelledby`` it); the date is a ``<time>``; while a task runs the section polls itself
  and ``p#adapt-running`` says editing is paused, with Approve ``aria-disabled`` naming it.
- ``p#adapt-status``: "Draft, version N. Not approved yet." / "Approved, version N." / "Needs
  re-check, version N.". ``p#adapt-running[role=status]`` "Adapting your earlier answer…" while a
  task runs (the ``section#prep`` then polls: its ``hx-trigger`` has ``every 2s``);
  ``p#adapt-error[role=alert]`` after a failure. ``p#adapt-practice.badge`` holds the practice
  notice when the provider is "fake".
- ``ol#adapt-units > li[data-unit][data-unit-id][data-origin][data-flagged]`` with
  ``p[data-unit-text]`` (escaped), ``ul[data-flags] > li[data-flag]`` with ``[data-flag-claim]``,
  ``details[data-unit-facts]`` / ``[data-no-facts]``, ``details[data-unit-edit]`` containing
  ``form[data-unit-form=edit]`` (``textarea[name=text]`` labelled "Edit this paragraph", button
  "Save paragraph"), ``form[data-unit-form=remove]`` and, on a flagged unit,
  ``form[data-unit-form=confirm]`` (button "This is true — save it as a fact"). Each form's
  ``action`` is the route above for the current version.
- ``form#adapt-approve-form`` with ``button#adapt-approve`` "Approve answer" on a draft with a
  unit; ``aria-disabled="true"`` and ``aria-describedby`` naming ``p#adapt-approve-reason`` ("1
  paragraph needs your check." / "N paragraphs need your check.") while a blocker exists; none on
  an approved answer, where ``p#adapt-approved`` says "Approved. Changing a paragraph starts a new
  draft." (``recheck``: ``p#adapt-recheck`` "A fact this answer cites changed. Check the
  paragraphs again.").
- Everything is announced in the live region with the fixed texts above; texts never go through
  ``|safe``.
"""

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from httpx import Response

from app.domain.facts import Privacy
from app.modules.ai.providers.fake import ScriptedResponse
from tests.fakes.ai import load_ai_fixture
from tests.refine_harness import Refining, refining
from tests.tailoring_ai_inputs import FACT_1
from tests.tailoring_pages import Tailoring

TASK = "adapt_answer"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai" / TASK
QUESTION = "Tell me about a time you planned under pressure."
OLD = "I planned 60 delivery routes a week with a team of 3, and I kept the schedule on time."
NEWER = "A newer try: I planned 60 delivery routes a week, and I told the team the plan."
COMPANY = "Harbor Kite Fictional"  # the company of the application the answer was practiced in
TARGET_COMPANY = "Other Fictional Co"  # the company of the application it is adapted to
TARGET_TITLE = "Other Planner"
P1 = "I planned 60 delivery routes a week with a team of 3."
P2 = "I cut invoice processing time by 35% across 4 warehouses."
STAMP = "2026-10-20T09:00:00+00:00"
ANGLE_ID = "angle-planner"
ANNOUNCE = {
    "adapt": "Adapting your earlier answer.",
    "edit": "Paragraph saved.",
    "edit_claim": "Paragraph saved. Save this as a fact?",
    "edit_approved": "Saved as a new draft. The approved version is unchanged.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Answer approved.",
}
INJECTION_OLD = FIXTURES / "injection_old_answer.txt"
TABLES = (
    "prep_documents",
    "prep_document_versions",
    "prep_units",
    "prep_unit_facts",
    "prep_version_facts",
)


def service_module() -> Any:
    return importlib.import_module("app.services.prep_documents")


def fixture_data(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())["data"]
    return loaded


class AdaptDesk:
    """Two applications: ``src`` (a job with a current summary and facts F1 to F5, F1 to F3
    usable) where answers are practiced, and ``aid`` (the target, "Other Planner" at "Other
    Fictional Co") where the earlier answer is adapted, one global bank question ``qid``, and the
    services."""

    def __init__(
        self,
        r: Refining,
        *,
        facts: bool = True,
        src_job: str = "adapt-src-job",
        dst_job: str = "adapt-dst-job",
    ) -> None:
        self.r = r
        self.tw = Tailoring(r)
        self.src: str = self.tw.ready(src_job, facts=facts).id
        self.aid: str = self.tw.another_application(dst_job).id
        self.qid: str = r.run.app.state.prep.add_bank_question(QUESTION, "behavioral").id
        self.service = r.run.app.state.prep_documents
        self.practice_service = r.run.app.state.answer_practice

    # --- practicing (the earlier answers) -----------------------------------------------------

    def practice(
        self,
        aid: str | None = None,
        answer: str = OLD,
        *,
        qid: str | None = None,
        ready: bool = False,
    ) -> Any:
        """Submit a practice answer through the real service and wait for its feedback; with
        ``ready`` also mark the question ready in that application."""
        aid, qid = aid or self.src, qid or self.qid
        self.r.run.spy.script("answer_feedback", load_ai_fixture("answer_feedback", "ok"))
        self.r.run.clock.advance(timedelta(minutes=1))
        version = self.practice_service.view(aid, qid).version
        made = self.practice_service.submit(aid, qid, answer, version=version)
        row = self.r.run.wait_terminal(made.task.id)
        assert str(row.status.value) == "succeeded", (str(row.status.value), row.error)
        if ready:
            self.mark_ready(aid, qid, True)
        return made

    def mark_ready(self, aid: str, qid: str, ready: bool) -> None:
        version = self.practice_service.view(aid, qid).version
        self.r.run.app.state.prep.mark_ready(aid, qid, ready=ready, version=version)

    def another(self, job: str) -> str:
        """One more application (company "Other Fictional Co"), without a summary."""
        return str(self.tw.another_application(job).id)

    def question(self, text: str, **options: Any) -> str:
        return str(self.r.run.app.state.prep.add_bank_question(text, "behavioral", **options).id)

    def known_skill(self, name: str) -> None:
        self.r.drafts(f"A draft note about {name} work.", skills=(name,))

    # --- reading the service ------------------------------------------------------------------

    def view(self, aid: str | None = None, qid: str | None = None) -> Any:
        return self.service.adapted_view(aid or self.aid, qid or self.qid)

    def answer(self, aid: str | None = None) -> Any:
        return self.view(aid).answer

    def version_id(self, aid: str | None = None) -> str | None:
        version = self.answer(aid).version
        return None if version is None else str(version.id)

    def posted_version(self, aid: str | None = None) -> str:
        return self.version_id(aid) or ""

    def unit_id(self, text: str, aid: str | None = None) -> str:
        found = [u.id for u in self.answer(aid).units if u.text == text]
        assert len(found) == 1, f"expected one unit saying {text!r}, found {len(found)}"
        return str(found[0])

    # --- the provider -------------------------------------------------------------------------

    def script(self, fixture: str | None = "ok", data: dict[str, Any] | None = None) -> None:
        if data is not None:
            self.r.run.spy.script(TASK, ScriptedResponse(data=data))
        elif fixture is not None:
            self.r.run.spy.script(TASK, load_ai_fixture(TASK, fixture))

    def requests(self) -> list[Any]:
        return [q for q in self.r.run.spy.requests if q.task == TASK]

    # --- adapting -----------------------------------------------------------------------------

    def adapt(
        self,
        fixture: str | None = "ok",
        *,
        data: dict[str, Any] | None = None,
        aid: str | None = None,
        status: str = "succeeded",
        **options: Any,
    ) -> Any:
        """Script the answer (``fixture`` None: the app's own fake), start the adaptation through
        the service and wait for the task to end as ``status``. Needs an earlier answer."""
        self.script(fixture, data)
        self.r.run.clock.advance(timedelta(minutes=1))
        ref = self.service.start_adapt(
            aid or self.aid, self.qid, version=self.version_id(aid), **options
        )
        row = self.r.run.wait_terminal(ref.id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return ref

    def ready_to_adapt(self, answer: str = OLD) -> None:
        self.practice(self.src, answer)

    def approve(self, aid: str | None = None) -> str:
        version = self.version_id(aid)
        assert version is not None
        self.service.approve_adapted(aid or self.aid, self.qid, version)
        return version

    def approved(self, fixture: str = "ok") -> str:
        self.adapt(fixture)
        return self.approve()

    # --- reading storage ----------------------------------------------------------------------

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return self.r.run.rows(sql, params)

    def versions(self, aid: str | None = None) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT v.* FROM prep_document_versions v JOIN prep_documents d ON d.id=v.document_id "
            "WHERE d.application_id=? AND d.kind='adapted_answer' AND d.question_id=? "
            "ORDER BY v.number",
            (aid or self.aid, self.qid),
        )

    def version_row(self, version_id: str) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM prep_document_versions WHERE id=?", (version_id,))
        return row

    def units(self, version_id: str, *, removed: bool = True) -> list[dict[str, Any]]:
        """The stored units of a version in position order, each with ``flag_list``, ``details``
        and ``cited`` (the refs ``F<n>@<v>`` of its citation rows)."""
        found: list[dict[str, Any]] = []
        for row in self.rows(
            "SELECT * FROM prep_units WHERE document_version_id=? ORDER BY position", (version_id,)
        ):
            if row["removed"] and not removed:
                continue
            cited = self.rows(
                "SELECT f.number AS number, fv.version AS version FROM prep_unit_facts c "
                "JOIN facts f ON f.id=c.fact_id JOIN fact_versions fv ON fv.id=c.fact_version_id "
                "WHERE c.unit_id=? ORDER BY f.number",
                (row["id"],),
            )
            found.append(
                {
                    **row,
                    "flag_list": json.loads(row["flags"]),
                    "details": json.loads(row["added_details"]),
                    "cited": [f"F{c['number']}@{c['version']}" for c in cited],
                }
            )
        return found

    def texts(self, version_id: str, *, removed: bool = False) -> list[str]:
        return [row["text"] for row in self.units(version_id, removed=removed)]

    def unit_with(self, version_id: str, text: str) -> dict[str, Any]:
        found = [row for row in self.units(version_id) if row["text"] == text]
        assert len(found) == 1, f"expected one unit saying {text!r}, found {len(found)}"
        return found[0]

    def flag_triples(self, version_id: str) -> list[tuple[str, str, str]]:
        return [
            (flag["kind"], flag["claim"], row["text"])
            for row in self.units(version_id)
            for flag in row["flag_list"]
        ]

    def sent_pairs(self, version_id: str) -> set[tuple[int, int]]:
        rows = self.rows(
            "SELECT f.number AS number, fv.version AS version FROM prep_version_facts s "
            "JOIN facts f ON f.id=s.fact_id JOIN fact_versions fv ON fv.id=s.fact_version_id "
            "WHERE s.version_id=?",
            (version_id,),
        )
        return {(row["number"], row["version"]) for row in rows}

    def adapt_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {table: self.rows(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}  # noqa: S608

    def counts(self) -> dict[str, int]:
        return {
            table: self.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]  # noqa: S608
            for table in (
                *TABLES,
                "facts",
                "fact_versions",
                "tasks",
                "usage_records",
                "practice_attempts",
                "prep_items",
            )
        }

    def fact(self, number: int) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM facts WHERE number=?", (number,))
        return row

    # --- changing the world -------------------------------------------------------------------

    def withdraw(self, how: str, number: int = 1) -> None:
        row = self.fact(number)
        facts = self.r.facts
        if how == "never_use":
            facts.set_privacy(row["id"], Privacy.NEVER_USE, expected_version=row["version"])
        elif how == "archived":
            facts.archive(row["id"], expected_version=row["version"])
        elif how == "reworded":
            from app.services.facts import FactInput

            facts.edit(
                row["id"], FactInput(text=FACT_1 + " I also trained 2 colleagues."),
                expected_version=row["version"],
            )  # fmt: skip
        else:
            raise AssertionError(how)

    def set_angle(self, aid: str | None = None, angle_id: str = ANGLE_ID) -> None:
        aid = aid or self.aid
        if not self.rows("SELECT id FROM angles WHERE id=?", (angle_id,)):
            self.r.run.execute(
                "INSERT INTO angles (id,name,description,origin,created_at,updated_at) "
                "VALUES (?,?,?,'user',?,?)",
                (angle_id, "Planner", "Lead with routes.", STAMP, STAMP),
            )
        if self.rows("SELECT id FROM workspaces WHERE application_id=?", (aid,)):
            self.r.run.execute(
                "UPDATE workspaces SET angle_id=?, updated_at=? WHERE application_id=?",
                (angle_id, STAMP, aid),
            )
        else:
            self.r.run.execute(
                "INSERT INTO workspaces (id,application_id,angle_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (f"workspace-{aid}", aid, angle_id, STAMP, STAMP),
            )

    # --- requests -----------------------------------------------------------------------------

    def path(self, tail: str = "", aid: str | None = None, qid: str | None = None) -> str:
        return f"/applications/{aid or self.aid}/prep/practice/{qid or self.qid}{tail}"

    def unit_path(self, version_id: str, unit_id: str, action: str, aid: str | None = None) -> str:
        return self.path(f"/adapted/{version_id}/units/{unit_id}/{action}", aid)

    def approve_path(self, version_id: str, aid: str | None = None) -> str:
        return self.path(f"/adapted/{version_id}/approve", aid)

    def get(self, aid: str | None = None, qid: str | None = None) -> Response:
        return self.r.client.get(self.path("", aid, qid), headers=self.r.run.run.htmx())

    def post(self, path: str, values: dict[str, Any] | None = None) -> Response:
        return self.r.client.post(
            path, data=values or {}, headers=self.r.run.run.action(**{"HX-Request": "true"})
        )


@contextmanager
def adapt_app(tmp_path: Any, **options: Any) -> Iterator[AdaptDesk]:
    with refining(tmp_path) as r:
        yield AdaptDesk(r, **options)
