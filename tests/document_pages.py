"""The running app, fictional data and readers for the M6.7 tailored CV tests (service half:
AC-M6.1 to AC-M6.4; spec 3.1, 3.2, 3.6; ADR-106 items 5, 8, 9, 11, 12; ADR-110 item 8). Every value
is fictional. No route is involved: the tests call the service on ``app.state.documents`` and the
real task runner executes the handler behind the spy and the fake provider. The application
modules of M6.7 are imported lazily (``Documents.docs``), so a missing service or handler fails the
test that needs it, not the collection.

ASSUMED INTERFACE (the implementer follows it; the UI of M6.8 builds on ``DocumentService.view``)

Task and wiring
- ``app/tasks/documents.py``: ``tailor_cv_handler(service) -> TaskHandler`` registers in
  ``create_app`` for the task type ``tailor_cv`` (``TASK_TIMEOUT_FLOOR + 30 s``, label "Tailor
  CV"); it is ``ctx.complete(await service.run_tailor_cv(ctx))`` like ``app/tasks/tailoring.py``.
  Dedupe key ``tailor_cv:<application id>`` (``tasks.enqueue("tailor_cv", application_id, ...)``).
- ``app/services/documents.py``: ``DocumentService`` on ``app.state.documents``, built like
  ``TailoringService`` (factory, clock, ids, workspace, settings, tasks, usage) plus whatever it
  needs to read the approved template (``CvTemplateService.approved`` / ``tailoring_repo
  .get_template`` + ``template_spec``), personal details (``PERSONAL_DETAILS`` setting, read ONLY
  by ``view`` for the contact block, never by a task) and to create a fact (``FactService`` gains
  a ``create_in(conn, data, *, source)``-style entry so a fact and a document version commit in
  one transaction, fact source ``document``, status confirmed).
- The model's units are read with ``run_tailor_cv`` (``app/services/tailoring_ai.py``) over the
  stored fact rows (``job_fit_repo.inputs``), the job's current summary requirements, the chosen
  angle (workspace angle, else ``None``), the APPROVED template of the application's CV country
  (workspace ``cv_country``, else ``JobPreferences.cv_country``), the note and the kept units of
  the document's current version (``origin == 'user'`` or ``removed``), ``known_skills`` from
  ``skills_repo``. ``new_id`` of ``resolve_cv_units`` is the id factory. Personal details and the
  photo are never read by the task.

Errors (all in ``app.services.documents``; each has ``.message``, plain copy, no class names)
- ``GenerateNotReady(ValueError)``: ``.reason`` in ``summary|requirements|facts|country|template``
  and ``.message``: ``app.services.tailoring.NO_SUMMARY`` / ``NO_REQUIREMENTS`` / ``NO_FACTS`` for
  the first three (checked in that order, the TailoringService rules), a plain sentence naming
  "country" when neither the workspace nor the preferences have one, and a plain sentence
  containing the word "template" when the country has no APPROVED template (a draft row does
  not count; another country's template does not count).
- ``ReplaceNotConfirmed(ValueError)``: ``replace_all`` without ``confirm_replace``.
- ``ApprovalBlocked(ValueError)``: ``.blockers`` (tuple of ``Blocker``) and ``.message``
  ``"1 line needs your check."`` / ``"N lines need your check."`` where N counts DISTINCT
  non-removed units that have at least one blocker.
- ``UnitInvalid(ValueError)``: blank text, text over 400 characters or with a control character,
  a section not in the version's template, an unknown role label. Nothing is written.
- ``VersionConflict`` (``app.domain.errors``): ``version_id`` is not the document's current
  version (edit, add, remove, confirm, approve). Nothing is written.
- ``ApplicationNotFound`` for an unknown or unsafe application id; ``UnitNotFound(LookupError)``
  when ``unit_id`` is not a unit of ``version_id``.

``DocumentService`` (all methods blocking; ``application_id`` first)
- ``plan_start(application_id, *, tier=None) -> SummaryPlan``: what a start would use (``service``
  and ``tier``; the tier is ``tier``, else ``settings.task_tiers.get("tailor_cv", "thorough")``),
  for ``app.state.budget_gate.check(plan.service, tier=plan.tier, confirmation=None)`` exactly as
  ``TailoringService.plan_start`` is used by the M6.5 route. ``GenerateNotReady`` when not ready,
  ``ProviderNotConfigured`` without a provider.
- ``start_generate(application_id, *, note=None, replace_all=False, confirm_replace=False,
  tier=None) -> TaskRef``: ``ReplaceNotConfirmed`` FIRST (nothing queued) when ``replace_all`` and
  not ``confirm_replace``; then the ``plan_start`` readiness; the note is trimmed, at most 500
  characters (``ValueError`` over); then ``tasks.enqueue("tailor_cv", application_id,
  input_ref=...)`` (a task already queued or running for the application is returned,
  ``created`` False). The service does not ask the budget gate itself (the M6.8 route does);
  the runner's reserve refuses a request at the blocking point (the task fails, nothing written).
- ``run_tailor_cv(ctx) -> ResultWriter`` (inside the task): the whole result is written by the
  writer that ``ctx.complete`` runs in the transaction that marks the task succeeded, never
  before: the ``documents`` row on the first version (one per application and kind ``cv``,
  ``current_version_id`` moved to the new version with a version-checked UPDATE), one new
  ``document_versions`` row (draft, ``number`` = previous + 1, ``parent_version_id`` = the previous
  CURRENT version or NULL, ``origin`` ``ai``, ``template_snapshot`` = the approved spec as JSON
  (``TemplateSpec.model_dump(mode="json")``), ``angle_text`` = angle name and description or NULL,
  ``note`` = the trimmed note or NULL, provider/model/prompt_id/prompt_version/task_id of the
  request), its ``document_sent_facts`` (one per fact version sent: ``fact_refs``), and its
  ``document_units`` (positions 0..n-1, tombstones included) with ``document_unit_facts`` for
  every cited ref that is a real fact version (a ref that names no stored version, such as
  ``F999@1``, has no row; its flag keeps the claim), ``role_id`` found from the role label
  ("<title> at <employer>"; NULL when none), ``flags`` the JSON array ``[{"kind": "unsupported" |
  "needs_check", "claim": "<text>"}]`` of ``check_units`` (the role flag of ``resolve_cv_units``
  included), ``added_details`` JSON array, ``origin`` ``ai`` for model units. The merge of a
  regeneration is ``merge_regenerated(previous_units, result.units, replace_all)`` over the current
  version's units (kept ones are re-inserted with new unit ids, their citations and flags
  carried over unchanged; ``replace_all`` keeps no earlier unit, no tombstone). Result ref
  ``"document_version:<id>"``. A failed or cancelled task writes nothing.
- ``view(application_id, version_id=None) -> DocumentView``: reads only. ``version_id`` None is
  the current version; a given id shows that version (read-only). Structure ``DocumentView``:
    ``application_id``; ``document_id`` (None until a first version); ``version`` (``VersionInfo``
    or None before the first version); ``versions`` (newest first); ``units`` (visible,
    non-removed ``UnitView`` in stored order); ``sections`` (``SectionView``); ``contact``
    (``ContactBlock``); ``blockers`` (tuple of ``Blocker``); ``diff`` (tuple of
    ``app.domain.master_cv.DiffLine`` = ``cv_diff(build_master_cv(records, role_dates), units)``
    over the CURRENT facts); ``template`` (the version's ``TemplateSpec`` snapshot, else None);
    ``practice_notice`` (non-empty text containing "Practice data" when the version's provider is
    the fake, else None); ``task`` (``app.services.tailoring.TaskState`` of ``tailor_cv:<id>``).
  ``VersionInfo``: ``id, number, status, parent_version_id, origin, provider, note, created_at``.
  ``UnitView``: ``unit_id, position, section, role`` (label or None), ``role_id, kind, text,
    origin`` (``"ai"``/``"user"``), ``flags`` (tuple of ``FlagView(kind, claim)``), ``fact_ids``
    (tuple of ``"F<number>@<version>"`` as stored), ``cited`` (tuple of
    ``app.services.tailoring.FactWording(id, text)``: the user's own wording of each cited fact
    version), ``added_details``.
  ``SectionView``: ``id`` (section id), ``heading`` (the SNAPSHOT's heading), ``blocks``
    (``RoleBlock(header: RoleHeader | None, units: tuple[UnitView, ...])``). Template order; only
    sections with visible units; inside a section role blocks in master-CV order (most recent
    role first), the role-less block last; units in stored order.
  ``RoleHeader``: ``role_id, title, employer, start_text, end_text`` (None when absent) and
    ``current`` (start and no end) built by CODE from the ``roles`` row and the SNAPSHOT's
    ``date_format`` (``month_year`` "Mar 2019", ``numeric`` "03/2019", ``iso`` "2019-03", ``year``
    "2019"; a year-only date shows the year in every format).
  ``ContactBlock``: ``name`` ("" when no details) and ``lines`` (email, phone, location, links, in
    that order, only the ``details_shown`` of the snapshot) from the saved ``personal_details``
    setting at view time; never stored in a unit, never sent to a request.
  ``Blocker(unit_id, kind, claim)``: the stored flags of every non-removed unit PLUS
    ``approval_blockers`` re-run with the CURRENT facts (never-use, not confirmed, missing ->
    ``unsupported``; cited version older than current -> ``needs_check``), without repeats.
- ``edit_unit(application_id, version_id, unit_id, text, *, fact_ids=None, added_details=()) ->
  EditOutcome``; ``add_unit(application_id, version_id, section, text, *, role=None, fact_ids=(),
  added_details=()) -> EditOutcome``; ``remove_unit(application_id, version_id, unit_id) -> str``
  (the new version id); ``resolve_flag_confirm(application_id, version_id, unit_id) ->
  EditOutcome``; ``approve(application_id, version_id) -> None``.
    Each edit/add/remove/confirm checks ``version_id`` is the document's current version, then in
    ONE transaction inserts a NEW draft version (``origin`` ``user``, ``parent_version_id`` =
    ``version_id``, ``number`` + 1, the parent's ``template_snapshot``, ``angle_text`` and
    ``document_sent_facts`` copied, no provider columns), copies every unit (new unit ids, same
    order, citations, flags, origin, ``removed``), applies the change, moves the document's
    ``current_version_id`` (version-checked). The old version is never touched, whatever its status.
    - ``edit_unit``: the unit becomes ``origin`` ``user`` with the new text; ``fact_ids`` None keeps
      its stored citations, a tuple replaces them; its flags are RECOMPUTED with ``check_units``
      over that unit against the facts the version's ``document_sent_facts`` name (so a citation
      outside the sent set is ``unsupported``, a quantity/name/skill/added detail absent from its
      cited facts is ``needs_check``). The edited unit keeps its section and role.
    - ``add_unit``: a content unit (origin ``user``) placed right after the last stored unit of the
      same section (else at the end); ``role`` is a role label or None; same re-check; with no
      ``fact_ids`` it is ``unsupported`` (uncited) until the user confirms it.
    - ``remove_unit``: the unit stays as a tombstone (``removed`` = 1, text and flags kept); the
      view hides it, blockers ignore it, a regeneration does not bring it back.
    - ``resolve_flag_confirm``: "This is true: save it as a fact" and the yes of "Save this as a
      fact?": in the same transaction creates a CONFIRMED fact (normal privacy, version 1, source
      ``document``, text = the unit's text, role = the unit's ``role_id`` or none, no skills) and
      the new version, in which that unit (origin ``user``) cites ONLY the new fact's ref
      ``F<n>@1`` and has no flags. A structure unit is ``UnitInvalid``. If the version insert
      fails, no fact exists afterwards.
    - ``EditOutcome``: ``version_id`` (the new draft), ``unit_id`` (the changed unit's id in it),
      ``offer`` (None or ``FactOffer(question, claims, unit_id)``). ``offer`` is present exactly
      when the re-check of an edit/add found ``needs_check`` CLAIMS (the texts of
      ``app.domain.honesty.new_claims`` over the unit's text against its cited facts); then
      ``question`` is "Save this as a fact?" and ``claims`` those claim texts, in order. Nothing
      is offered otherwise. ``resolve_flag_confirm`` and ``remove_unit``-style outcomes have
      ``offer`` None.
    - ``approve``: ``VersionConflict`` for a stale ``version_id``; reads the blockers as ``view``
      does, NOW (stored flags + ``approval_blockers`` with current facts); any blocker raises
      ``ApprovalBlocked`` (nothing changes); else the version's status becomes ``approved``
      (``tailoring_repo.set_version_status``, version-checked). Approving an approved version is
      ``VersionConflict``.

Text limits: a unit's text 1..400 characters, no control characters; nothing is escaped or
normalised by the service (text is data).
"""

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.fakes.ai import load_ai_fixture
from tests.refine_harness import refining
from tests.tailoring_ai_inputs import (
    FACT_1,
    FACT_2,
    FACT_3,
    FACT_7,
    FACT_8,
    HIDDEN,
    TEMPLATE,
)
from tests.tailoring_pages import (
    ARCHIVED_TEXT,
    CURRENT_STAMP,
    DRAFT_TEXT,
    Tailoring,
)

TAILOR_CV = "tailor_cv"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai" / TAILOR_CV
SAVE_AS_FACT = "Save this as a fact?"
ROLE_A = ("Operations Analyst", "Example Widgets Ltd")
ROLE_B = ("Junior Planner", "Sample Logistics SA")
NOTE = "Lead with logistics."
SUMMARY_UNIT = "Plans delivery routes and speeds up invoice processing."
LANGUAGE_UNIT = "English, used at work every day."
CV_TABLES = (
    "documents",
    "document_versions",
    "document_units",
    "document_unit_facts",
    "document_sent_facts",
)


def injection_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def template_json(**changes: Any) -> str:
    """The ``cv_templates.spec`` text of an approved row: ``{"template": ..., "notes": {}}``."""
    spec = TEMPLATE.model_dump(mode="json")
    spec.update(changes)
    return json.dumps({"template": spec, "notes": {}})


def seed_cv_facts(facts: Any, execute: Any) -> None:
    """F1 F2 (role A, current 2019-03 to 2022-06), F3 (no role), F4 never-use, F5 draft, F6
    archived, F7 (role B, 2017-01 to 2019-02), F8 (role B, generalize names). The numbers the
    fixtures of ``tests/fixtures/ai/tailor_cv`` talk about. F1 F2 F3 F7 F8 are the usable facts."""
    from app.services.facts import DraftInput, DraftOrigin, FactInput, NewRole

    first = facts.create(
        FactInput(text=FACT_1, new_role=NewRole(employer=ROLE_A[1], title=ROLE_A[0]))
    )
    facts.create(FactInput(text=FACT_2, role_id=first.role_id))
    facts.create(FactInput(text=FACT_3))
    facts.create(FactInput(text=HIDDEN, privacy="never_use"))
    origin = DraftOrigin(source="cv_import", provider="fake", model="fake-thorough")
    facts.create_drafts(
        origin, [DraftInput(text=DRAFT_TEXT, skills=(), original_answer=DRAFT_TEXT)]
    )
    old = facts.create(FactInput(text=ARCHIVED_TEXT))
    facts.archive(old.id, expected_version=old.version)
    second = facts.create(
        FactInput(text=FACT_7, new_role=NewRole(employer=ROLE_B[1], title=ROLE_B[0]))
    )
    facts.create(FactInput(text=FACT_8, role_id=second.role_id, privacy="generalize"))
    execute(
        "UPDATE roles SET start_date=?, end_date=? WHERE id=?",
        ("2019-03", "2022-06", first.role_id),
    )
    execute(
        "UPDATE roles SET start_date=?, end_date=? WHERE id=?",
        ("2017-01", "2019-02", second.role_id),
    )


class Documents(Tailoring):
    """The workspace harness plus the tailored CV service. ``tw.ready()`` and the rest are
    inherited."""

    # --- seeding ------------------------------------------------------------------------------

    def cv_ready(
        self,
        job_id: str = "cv-job",
        *,
        summary: bool = True,
        seed_facts: bool = True,
        template: bool = True,
        country: str | None = None,
        posting: str | None = None,
    ) -> Any:
        """An application with a current summary (``REQUIREMENTS``), the CV facts, an APPROVED
        template for PT (the finished setup's preferred country; ``country`` saves another one on
        the workspace) and nothing generated. ``seed_facts=False`` keeps the facts that exist."""
        application = self.ready(job_id, summary=summary, facts=False, posting=posting)
        if seed_facts and not self.rows("SELECT id FROM facts"):
            seed_cv_facts(self.r.facts, self.r.run.execute)
            numbers = [row["number"] for row in self.rows("SELECT number FROM facts ORDER BY 1")]
            assert numbers == [1, 2, 3, 4, 5, 6, 7, 8], numbers
        if template and not self.rows("SELECT id FROM cv_templates WHERE country='PT'"):
            self.put_template("PT")
        if country is not None:
            self.r.run.app.state.tailoring.save_country(
                application.id, cv_country=country, version=0
            )
        return application

    def put_template(self, code: str, *, approved: bool = True, **changes: Any) -> None:
        self.r.run.execute(
            "INSERT INTO cv_templates (id,country,status,spec,approved_at,provider,model,"
            "prompt_id,prompt_version,task_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'fake','fake-thorough','draft_cv_template','1',NULL,?,?)",
            (
                f"template-{code}",
                code,
                "approved" if approved else "draft",
                template_json(**changes),
                CURRENT_STAMP if approved else None,
                CURRENT_STAMP,
                CURRENT_STAMP,
            ),
        )

    def save_details(self) -> None:
        from app.domain.settings import PERSONAL_DETAILS
        from app.services.settings import SettingsService
        from tests.tailoring_ai_inputs import DETAILS

        settings = SettingsService(self.db, self.r.run.clock)
        settings.save(
            PERSONAL_DETAILS,
            DETAILS,
            expected_version=settings.load(PERSONAL_DETAILS).version,
        )

    def fact_row(self, number: int) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM facts WHERE number=?", (number,))
        return row

    def role_id(self, title: str) -> str:
        (row,) = self.rows("SELECT id FROM roles WHERE title=?", (title,))
        return str(row["id"])

    # --- the service --------------------------------------------------------------------------

    @property
    def docs(self) -> Any:
        """The module ``app.services.documents`` (imported when first needed)."""
        return importlib.import_module("app.services.documents")

    @property
    def service(self) -> Any:
        return self.r.run.app.state.documents

    def generate(
        self,
        aid: str,
        fixture: str | None = "ok",
        *,
        status: str = "succeeded",
        **options: Any,
    ) -> Any:
        """Script the answer (``None``: the app's own fake), start by the service, wait for the
        task to end as ``status``."""
        if fixture is not None:
            self.script(TAILOR_CV, fixture)
        ref = self.service.start_generate(aid, **options)
        row = self.r.run.wait_terminal(ref.id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return row

    # --- what is stored -----------------------------------------------------------------------

    def document(self, aid: str) -> dict[str, Any] | None:
        found = self.rows("SELECT * FROM documents WHERE application_id=? AND kind='cv'", (aid,))
        return found[0] if found else None

    def versions(self, aid: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT v.* FROM document_versions v JOIN documents d ON d.id=v.document_id "
            "WHERE d.application_id=? AND d.kind='cv' ORDER BY v.number",
            (aid,),
        )

    def current_version_id(self, aid: str) -> str:
        document = self.document(aid)
        assert document is not None, "nothing generated yet"
        return str(document["current_version_id"])

    def version_row(self, version_id: str) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM document_versions WHERE id=?", (version_id,))
        return row

    def units(self, version_id: str, *, removed: bool = True) -> list[dict[str, Any]]:
        """The stored units of a version in position order, each with ``flag_list`` (the JSON as
        a list of dicts), ``cited`` (the refs ``F<n>@<v>`` of its citation rows, by number) and
        the role's ``role_title`` (None without a role)."""
        rows = self.rows(
            "SELECT u.*, r.title AS role_title FROM document_units u "
            "LEFT JOIN roles r ON r.id=u.role_id WHERE u.document_version_id=? ORDER BY u.position",
            (version_id,),
        )
        found: list[dict[str, Any]] = []
        for row in rows:
            if row["removed"] and not removed:
                continue
            cited = self.rows(
                "SELECT f.number AS number, fv.version AS version FROM document_unit_facts c "
                "JOIN facts f ON f.id=c.fact_id JOIN fact_versions fv ON fv.id=c.fact_version_id "
                "WHERE c.document_unit_id=? ORDER BY f.number",
                (row["id"],),
            )
            found.append(
                {
                    **row,
                    "flag_list": json.loads(row["flags"]),
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
            "SELECT f.number AS number, fv.version AS version FROM document_sent_facts s "
            "JOIN facts f ON f.id=s.fact_id JOIN fact_versions fv ON fv.id=s.fact_version_id "
            "WHERE s.document_version_id=?",
            (version_id,),
        )
        return {(row["number"], row["version"]) for row in rows}

    def table_counts(self) -> dict[str, int]:
        return {
            table: self.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]  # noqa: S608
            for table in (*CV_TABLES, "facts", "fact_versions")
        }

    def cv_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608
            for table in CV_TABLES
        }


def fixture_units(name: str) -> list[dict[str, Any]]:
    return [dict(unit) for unit in load_ai_fixture(TAILOR_CV, name).data["units"]]  # type: ignore[index]


@contextmanager
def documents_app(tmp_path: Any) -> Iterator[Documents]:
    """Each test file wraps this in its own ``d`` fixture."""
    with refining(tmp_path) as r:
        yield Documents(r)
