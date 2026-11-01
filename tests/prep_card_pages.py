"""The running app, assumed interface, markup hooks and readers for the M8.2 part B tests of
narrative cards (``draft_narrative``, the salary card built in code, card reuse). Every value is
fictional. No application module of part B is imported at module level, so a missing service,
task or route fails the test that needs it, not the collection.

A card is a short answer the person gives in an interview: "Tell me about yourself", "Why this
role", "Why this company", "Why are you leaving", "Strengths" and "Weaknesses" (written by
``draft_narrative``), and "Salary" (built by code). Cards are stored in the M8.1 tables:
``prep_documents`` (one per application and kind), ``prep_document_versions``, ``prep_units`` and
``prep_unit_facts``, plus ``prep_version_facts`` (migration 0009: the facts SENT for a version).
They work as the CV and the cover letter do (``tests/letter_pages.py``): the same checks, the
same version rules, the same approval rules. What is written below is the contract of this part.

ASSUMED INTERFACE (the implementer follows it)

Task and wiring
- Prompt ``prompts/draft_narrative`` (see ``tests/unit/modules/ai/test_draft_narrative_prompt.py``)
  and ``TaskSpec("draft_narrative", kind="generation")``. Task type ``draft_narrative``, dedupe key
  ``draft_narrative:<application id>/<kind>``, label "Draft narrative card"; the whole result is
  written by the writer that ``ctx.complete`` runs, in the transaction that marks the task
  succeeded, or not at all. A card's request holds: ``card_kind`` (trusted); the job context (the
  posting, the requirements, the job's title and company); the sent facts (``facts_for_ai`` only:
  no never-use, draft or archived fact); the chosen angle; the company's saved notes, only for
  ``why_this_company`` (the block is empty for every other kind); and the company brief, empty
  until M8.3. It holds no personal detail and nothing about the salary.
- ``app.state.prep_documents`` is a ``PrepDocumentService`` (``app/services/prep_documents.py``),
  built like ``LetterService``. It raises the exception classes of ``app.services.documents``
  (``GenerateNotReady``, ``GenerateInProgress``, ``NothingToApprove``, ``ApprovalBlocked`` with the
  noun "paragraph", ``UnitInvalid``, ``UnitNotFound``) and ``VersionConflict`` of
  ``app.domain.errors``; plus ``ReuseRefused`` (a ``ValueError`` with a ``message``) of its own
  module. ``kind`` is one of ``CARD_KINDS`` of ``app.domain.narrative`` or "salary". Every method
  takes ``application_id`` first and ``kind`` second.

``PrepDocumentService`` (blocking)
- ``view(application_id) -> CardsView`` reads only (nothing is written, started or created):
  ``application_id``; ``cards``, one ``CardView`` for each of ``CARD_KINDS`` and then "salary",
  in that order; ``has_facts`` (a usable confirmed fact exists); ``approved_kinds`` (the kinds
  whose current version is approved AND has no blocker now); ``angle_id`` / ``angle_name`` (the
  application's chosen angle, else None).
  ``CardView``: ``kind``; ``label`` ("Tell me about yourself", "Why this role", "Why this
  company", "Why you are leaving", "Strengths", "Weaknesses", "Salary"); ``state`` (one of
  ``none|running|failed|empty|draft|approved|recheck``); ``version`` (None, or an object with
  ``id``, ``number``, ``status``, ``origin``, ``provider``, ``reused_version_id``);
  ``units`` (visible units, each with ``id``, ``text``, ``origin``, ``flags`` as
  ``(kind, claim)``-pairs-like objects with ``.kind`` and ``.claim``, ``fact_ids``,
  ``added_details`` and ``cited`` with the user's wording); ``blockers`` (objects with
  ``unit_id``, ``kind`` and ``claim``: the stored flags plus the approval check re-run with the
  facts as they are NOW, for approved versions too); ``empty`` (True when the card has no visible
  unit because no confirmed fact supports it); ``approved`` (status approved AND no blocker);
  ``needs_recheck`` (status approved AND a blocker); ``task`` (``active``, ``status``, ``error``);
  ``reusable`` (the approved cards of OTHER applications this one may copy, newest first, each
  with ``version_id`` and ``label``; always empty for the three kinds that are not reusable).
  States: ``none`` no version yet, ``running`` the task is active, ``failed`` the last task
  failed and no version came of it, ``empty`` the current version has no visible unit,
  ``recheck`` the current version is approved but a blocker exists, ``approved``, ``draft``.
- ``plan_start(application_id, kind, *, tier=None) -> SummaryPlan`` (what the route asks the
  budget gate); ``start_card(application_id, kind, *, version=None, tier=None) -> TaskRef``.
  ``version`` is the id of the card's current version as last seen, None while it has none
  (``VersionConflict`` otherwise). ``ValueError`` for "salary" and for any word that is not a
  card kind (nothing queued). ``GenerateNotReady`` with reason ``facts`` (message
  ``app.services.tailoring.NO_FACTS``) when no usable confirmed fact exists: no request. A
  task already queued or running for the same application and kind is returned. The service
  does not ask the budget gate; the route does.
- The written version: ``origin`` ``generated``, ``status`` ``draft``, ``number`` next, parent
  the previous current version (None for the first), provider, model, ``prompt_id``
  ``draft_narrative``, ``prompt_version`` ``1``, the chosen angle's ``angle_id`` (NULL when
  none) and ``angle_text`` ("<name>: <description>", "" when none); units with section
  ``answer``, origin ``ai``, no role, the flags of ``check_units`` (job title and company are
  name sources), the model's ``added_details``, one ``prep_unit_facts`` row per cited fact version
  that was sent; and one ``prep_version_facts`` row per fact version SENT in the request. A
  model answer with no unit is stored as a version with no unit (state ``empty``).
- ``edit_unit(application_id, kind, version_id, unit_id, text, *, fact_ids=None,
  added_details=None) -> EditOutcome``, ``remove_unit(application_id, kind, version_id,
  unit_id) -> str`` (the new version id), ``resolve_flag_confirm(application_id, kind,
  version_id, unit_id) -> EditOutcome``, ``approve(application_id, kind, version_id)``: exactly as
  ``DocumentBase`` (every change is a NEW draft version of the card, the parent's sent facts are
  copied, a line is re-checked against the facts SENT for that version plus the facts the person
  saved from documents, a removed unit stays as a tombstone, a confirmation creates a CONFIRMED
  fact with source ``document`` in the same transaction as the new version, approval re-runs
  ``approval_blockers`` with the facts as they are now). ``EditOutcome`` and ``FactOffer`` are
  those of ``app.services.documents``; the offer's question is ``SAVE_AS_FACT``.
- ``build_salary(application_id, *, version=None) -> str``: builds the salary card in code from
  the saved job preferences' minimum salary and the job's stated pay
  (``app.domain.salary_card.build_salary_card``) and returns the id of the approved version. No
  AI request, no task, no usage record, no budget check. One transaction: document (first time),
  version (origin ``code``, no provider/model/prompt, no angle), units (origin ``code``, no
  citation) and the approval. With the same inputs as the current version (``version`` is its id)
  it changes nothing and returns that id; with changed inputs it writes a new approved version
  whose parent is the old one. ``VersionConflict`` for a stale ``version``;
  ``GenerateNotReady`` with reason ``salary`` when neither a minimum nor a stated pay exists.
- ``use_card(application_id, kind, source_version_id, *, version=None) -> str``: copies another
  application's APPROVED card into a new DRAFT version here (origin ``reused``,
  ``reused_version_id`` = the source, the source's ``angle_id`` and ``angle_text``, units and
  their citations copied under new ids, the source's sent facts copied) and returns its id. It
  is the card's current version here (parent: the previous current one, if any). Refused with
  ``ReuseRefused`` (nothing written) unless: ``kind`` is one of the four reusable kinds, the
  source is an approved version of the same kind of ANOTHER application, the source has no
  blocker now, and both applications have the SAME non-null chosen angle id (the source version's
  ``angle_id`` and this application's workspace angle). ``VersionConflict`` for a stale
  ``version``. Approval re-runs the checks here with the facts as they are then.

Routes (inside the ``#prep`` swap; ``{aid}`` application, ``{kind}`` a card kind, ``{uid}`` a unit
id). GET needs the token. POSTs need token and origin and a ``version`` field (the card's current
version id as last seen, "" while it has none); an unknown application, kind (also "salary" where
it is not a card to draft), unit or version id is the fixed 404; a missing field, an extra field
or a repeated field is 400; a stale ``version`` is 409 with the reload prompt (the text
``VersionConflict.message``).
- ``GET /applications/{aid}/prep/cards``: all cards. Writes nothing.
- ``POST .../cards/{kind}/generate`` (``version``, ``tier`` and the budget dialog's fields):
  announces "Card drafting started."; refused first by the budget gate (409 with the reason, or
  200 with the dialog ``dialog#cards-budget-dialog``); ``GenerateNotReady`` is 409 with its
  message.
- ``POST .../cards/{kind}/units/{uid}/edit`` (``version``, ``text``): "Paragraph saved."; with a
  new claim "Paragraph saved. Save this as a fact?"; from an approved version "Saved as a new
  draft. The approved version is unchanged."; ``UnitInvalid`` is 422 with its message.
- ``POST .../cards/{kind}/units/{uid}/remove`` (``version``): "Paragraph removed.".
- ``POST .../cards/{kind}/units/{uid}/confirm`` (``version``): "Saved as a fact. The paragraph now
  cites it.".
- ``POST .../cards/{kind}/approve`` (``version``): "Card approved."; ``ApprovalBlocked`` and
  ``NothingToApprove`` are 409 with the exception message.
- ``POST .../cards/{kind}/use`` (``version``, ``source`` the source version id): "Card copied as
  a new draft. Review it before approving."; ``ReuseRefused`` is 422 with its message.
- ``POST .../cards/salary`` (``version``): "Salary card built and approved."; not ready is 409.

Markup of ``GET .../cards`` (a ``section#prep`` like every Prep fragment)
- ``h3#cards-heading[tabindex=-1][data-autofocus]`` "Narrative cards"; ``a#cards-back`` "Back to
  questions" (``hx-get`` the Prep fragment). The Prep tab holds ``a#prep-cards-link`` "Narrative
  cards" (``href`` the page address ``/applications/{aid}?tab=prep``, ``hx-get`` the cards
  address).
- One ``section[data-card="<kind>"]#card-<kind>`` per card, in order, with ``data-state`` (the
  state above) and ``h4#card-<kind>-heading[tabindex=-1]`` with the label. After an action on a
  card its heading carries ``data-autofocus`` (instead of ``#cards-heading``).
- ``p#card-<kind>-status``: "Draft, version N. Not approved yet." / "Approved, version N." /
  "Needs re-check, version N." (the last when ``needs_recheck``). ``p#card-<kind>-empty`` "No
  confirmed facts support this card yet." with ``a#card-<kind>-sessions`` ("Answer questions in a
  session", ``href="/sessions/new"``) when the card is ``empty`` or the person has no usable
  fact. ``p#card-<kind>-running[role=status]`` "Drafting this card…" while a task runs;
  ``p#card-<kind>-error[role=alert]`` (the task's ``message_for_kind``) after a failure.
  ``p#card-<kind>-practice.badge`` holds the practice notice when the provider is "fake".
- ``form#card-<kind>-generate-form`` (hidden ``version``) with ``button#card-<kind>-generate``
  "Draft this card" ("Redraft" once a version exists), and the reason element ``p#cards-reason``
  when the budget blocks it (the button then has ``aria-disabled="true"``). The salary card has
  ``form#card-salary-form`` and ``button#card-salary-build`` "Build salary card" ("Rebuild" once
  it exists) instead.
- ``ol#card-<kind>-units > li[data-unit][data-unit-id][data-origin][data-flagged]``
  (``id="unit-<uid>"``, ``tabindex="-1"``): ``p[data-unit-text]`` (escaped); ``ul[data-flags] >
  li[data-flag]`` with ``[data-flag-claim]``; ``details[data-unit-facts]`` "Facts used (N)" or
  ``[data-no-facts]`` "No facts cited."; ``details[data-unit-edit] > summary`` "Edit" with
  ``form[data-unit-form=edit]`` (``textarea[name=text]`` labelled "Edit this paragraph", button
  "Save paragraph"), ``form[data-unit-form=remove]`` (button "Remove") and, on a flagged unit,
  ``form[data-unit-form=confirm]`` (button "This is true — save it as a fact"). No dialog opens
  after an edit: the offer is the confirm button on the flagged unit. None of these forms on a
  salary card's code units except "Remove".
- ``form#card-<kind>-approve-form`` (hidden ``version``) with ``button#card-<kind>-approve``
  "Approve card" on a draft with a unit; with ``aria-disabled=
  "true"`` and ``aria-describedby`` naming ``p#card-<kind>-approve-reason`` ("1 paragraph needs
  your check." / "N paragraphs need your check.") while a blocker exists; no approve button on an
  approved card, where ``p#card-<kind>-approved`` says "Approved. Changing a paragraph starts a
  new draft." (``recheck``: ``p#card-<kind>-recheck`` "A fact this card cites changed. Check the
  paragraphs again.").
- ``form#card-<kind>-use-form`` (hidden ``version`` and ``source``) with ``button#card-<kind>-use``
  "Use this card from another application" only when ``reusable`` is not empty.
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

TASK = "draft_narrative"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai" / TASK
KINDS = (
    "tell_me_about_yourself",
    "why_this_role",
    "why_this_company",
    "why_leaving",
    "strengths",
    "weaknesses",
)
REUSABLE = ("tell_me_about_yourself", "why_leaving", "strengths", "weaknesses")
COMPANY = "Harbor Kite Fictional"
TITLE = "Route Planner"
KIND = "tell_me_about_yourself"
P1 = "I planned 60 delivery routes a week with a team of 3."
P2 = "I also cut invoice processing time by 35% across 4 warehouses."
ANGLE_ID = "angle-planner"
ANGLE_NAME = "Planner"
ANGLE_TEXT = "Lead with routes."
STAMP = "2026-10-20T09:00:00+00:00"
POSTING_INJECTION = FIXTURES / "injection_posting.txt"
ANNOUNCE = {
    "generate": "Card drafting started.",
    "edit": "Paragraph saved.",
    "edit_claim": "Paragraph saved. Save this as a fact?",
    "edit_approved": "Saved as a new draft. The approved version is unchanged.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Card approved.",
    "use": "Card copied as a new draft. Review it before approving.",
    "salary": "Salary card built and approved.",
}
EMPTY_TEXT = "No confirmed facts support this card yet."


def service_module() -> Any:
    return importlib.import_module("app.services.prep_documents")


def fixture_data(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())["data"]
    return loaded


class CardDesk:
    """An application with a current summary and facts F1 to F5 (F1 to F3 usable), the card
    service, and readers for what it stores."""

    def __init__(
        self,
        r: Refining,
        job: str = "card-job",
        *,
        posting: str | None = None,
        facts: bool = True,
    ) -> None:
        self.r = r
        self.tw = Tailoring(r)
        self.aid: str = self.tw.ready(job, posting=posting, facts=facts).id
        self.service = r.run.app.state.prep_documents

    # --- reading the service ------------------------------------------------------------------

    def view(self, aid: str | None = None) -> Any:
        return self.service.view(aid or self.aid)

    def card(self, kind: str = KIND, aid: str | None = None) -> Any:
        found = [c for c in self.view(aid).cards if c.kind == kind]
        assert len(found) == 1, kind
        return found[0]

    def version_id(self, kind: str = KIND, aid: str | None = None) -> str | None:
        version = self.card(kind, aid).version
        return None if version is None else str(version.id)

    def posted_version(self, kind: str = KIND, aid: str | None = None) -> str:
        """What a form posts as ``version``: the current version id, "" while there is none."""
        return self.version_id(kind, aid) or ""

    def unit_id(self, kind: str, text: str, aid: str | None = None) -> str:
        found = [u.id for u in self.card(kind, aid).units if u.text == text]
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

    # --- drafting -----------------------------------------------------------------------------

    def draft(
        self,
        kind: str = KIND,
        fixture: str | None = "ok",
        *,
        data: dict[str, Any] | None = None,
        aid: str | None = None,
        status: str = "succeeded",
        **options: Any,
    ) -> Any:
        """Script the answer (``fixture`` None: the app's own fake), start the card through the
        service and wait for the task to end as ``status``."""
        self.script(fixture, data)
        self.r.run.clock.advance(timedelta(minutes=1))
        ref = self.service.start_card(
            aid or self.aid, kind, version=self.version_id(kind, aid), **options
        )
        row = self.r.run.wait_terminal(ref.id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return ref

    def approve(self, kind: str = KIND, aid: str | None = None) -> str:
        version = self.version_id(kind, aid)
        assert version is not None
        self.service.approve(aid or self.aid, kind, version)
        return version

    def approved(self, kind: str = KIND, fixture: str = "ok", aid: str | None = None) -> str:
        self.draft(kind, fixture, aid=aid)
        return self.approve(kind, aid)

    # --- reading storage ----------------------------------------------------------------------

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return self.r.run.rows(sql, params)

    def versions(self, kind: str = KIND, aid: str | None = None) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT v.* FROM prep_document_versions v JOIN prep_documents d ON d.id=v.document_id "
            "WHERE d.application_id=? AND d.kind=? ORDER BY v.number",
            (aid or self.aid, kind),
        )

    def version_row(self, version_id: str) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM prep_document_versions WHERE id=?", (version_id,))
        return row

    def units(self, version_id: str, *, removed: bool = True) -> list[dict[str, Any]]:
        """The stored units of a version in position order, each with ``flag_list`` (the JSON
        as a list of dicts) and ``cited`` (the refs ``F<n>@<v>`` of its citation rows)."""
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
        """(kind, claim, unit text) of every stored flag, in unit order."""
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

    def card_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608 - fixed names
            for table in (
                "prep_documents",
                "prep_document_versions",
                "prep_units",
                "prep_unit_facts",
                "prep_version_facts",
            )
        }

    def counts(self) -> dict[str, int]:
        return {
            table: self.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]  # noqa: S608
            for table in (
                "prep_documents",
                "prep_document_versions",
                "prep_units",
                "prep_unit_facts",
                "prep_version_facts",
                "facts",
                "fact_versions",
                "tasks",
                "usage_records",
            )
        }

    def fact(self, number: int) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM facts WHERE number=?", (number,))
        return row

    # --- changing the world -------------------------------------------------------------------

    def set_angle(
        self,
        aid: str | None = None,
        angle_id: str = ANGLE_ID,
        name: str = ANGLE_NAME,
        description: str = ANGLE_TEXT,
    ) -> None:
        """Choose an angle for the application's workspace (the angle row is created once)."""
        aid = aid or self.aid
        if not self.rows("SELECT id FROM angles WHERE id=?", (angle_id,)):
            self.r.run.execute(
                "INSERT INTO angles (id,name,description,origin,created_at,updated_at) "
                "VALUES (?,?,?,'user',?,?)",
                (angle_id, name, description, STAMP, STAMP),
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

    def another(self, job: str = "other-card-job", *, angle: str | None = ANGLE_ID) -> str:
        """A second application (own job), with ``angle`` chosen when given."""
        aid: str = self.tw.another_application(job).id
        if angle is not None:
            self.set_angle(aid, angle)
        return aid

    def withdraw(self, how: str, number: int = 1) -> None:
        """Make a fact unusable for a card that cites it: ``never_use``, ``archived``, or
        ``reworded`` (a new version of the fact, so an earlier citation is out of date)."""
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

    def set_minimum(self, amount: int | None, currency: str = "EUR") -> None:
        from app.domain.settings import JOB_PREFERENCES, JobPreferences, Salary
        from app.services.settings import SettingsService

        settings = SettingsService(self.r.run.app.state.workspace.db, self.r.run.clock)
        before = settings.load(JOB_PREFERENCES)
        minimum = None if amount is None else Salary(amount=amount, currency=currency)
        saved = (
            before.value.model_copy(update={"minimum_salary": minimum})
            if before.value is not None
            else JobPreferences(
                target_roles=("Route Planner",), cv_country="NL", minimum_salary=minimum
            )
        )
        settings.save(JOB_PREFERENCES, saved, expected_version=before.version)

    def set_posting_pay(
        self,
        low: float | None,
        high: float | None,
        currency: str | None = "EUR",
        period: str | None = "year",
        aid: str | None = None,
    ) -> None:
        job = self.rows("SELECT job_id FROM applications WHERE id=?", (aid or self.aid,))[0][
            "job_id"
        ]
        self.r.run.execute(
            "UPDATE jobs SET salary_min=?, salary_max=?, salary_currency=?, salary_period=? "
            "WHERE id=?",
            (low, high, currency, period, job),
        )

    def set_company_notes(self, notes: str | None) -> None:
        self.r.run.execute("UPDATE companies SET notes=? WHERE name=?", (notes, COMPANY))

    # --- requests -----------------------------------------------------------------------------

    def path(self, tail: str = "", aid: str | None = None) -> str:
        return f"/applications/{aid or self.aid}/prep/cards{tail}"

    def unit_path(self, kind: str, unit_id: str, action: str, aid: str | None = None) -> str:
        return self.path(f"/{kind}/units/{unit_id}/{action}", aid)

    def get(self, aid: str | None = None) -> Response:
        return self.r.client.get(self.path("", aid), headers=self.r.run.run.htmx())

    def post(self, path: str, values: dict[str, Any] | None = None) -> Response:
        return self.r.client.post(
            path, data=values or {}, headers=self.r.run.run.action(**{"HX-Request": "true"})
        )


@contextmanager
def card_app(tmp_path: Any, job: str = "card-job", **options: Any) -> Iterator[CardDesk]:
    with refining(tmp_path) as r:
        yield CardDesk(r, job, **options)
