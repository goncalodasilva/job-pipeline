"""The running app, fictional data and small HTML readers for the M6.5 workspace tests (Tailoring
tab, gap check, angle). Every value is fictional. The application modules are not imported here
(only the repo's own test helpers are), so a missing service fails the test that needs it.

Assumed interface (the implementer matches it; the integration and e2e tests share these names).

Service, tasks, routes
- ``app/services/tailoring.py``: ``TailoringService`` (``app.state.tailoring``), built like
  ``JobFitService`` (factory, clock, ids, workspace, settings, tasks, usage, gate). Methods:
  ``start_gap_check(application_id, *, tier=None) -> TaskRef | None`` and
  ``start_angles(application_id, *, tier=None) -> TaskRef | None`` (None: the step is not ready,
  i.e. no current summary or no usable confirmed fact; otherwise ``tasks.enqueue`` with the
  dedupe targets below, so a second start returns the running task).
- ``app/tasks/tailoring.py``: two thin handlers, registered in ``create_app`` as the task types
  ``gap_check`` and ``propose_angles`` (the prompt names), that write with ``ctx.complete``.
  Dedupe keys: ``gap_check:<application id>`` and ``propose_angles:<application id>``.
  They run ``run_gap_check`` / ``run_propose_angles`` (``app/services/tailoring_ai.py``) over the
  current summary's requirements (``job_fit_repo.inputs``), the stored fact rows and, for angles,
  the earlier angles: angles used by the workspaces of OTHER applications, most recently
  updated workspace first, at most ten.
- Result rows, written in the transaction that marks the task succeeded and nowhere else:
  ``gap_checks`` (application_id, ``summary`` = the current summary text, ``result`` =
  ``GapCheckResult.to_data()`` as JSON, provider/model/prompt_id/prompt_version/task_id) plus one
  ``gap_check_facts`` row per fact version sent; ``angle_proposals`` (application_id, ``result`` =
  ``AnglesResult.to_data()``, provenance). A failed or cancelled task writes no row. The
  provider column is ``fake`` for the fake provider and the page then shows ``PRACTICE_NOTICE``
  (``app.services.job_fit``).
- ``app/api/tailoring.py`` registers (``/applications/{id}`` is the existing page route):
  - ``GET /applications/{id}?tab=tailoring[&step=1..5]``: the page (token-free, read-only; a
    page with ``HX-Request`` needs the token). Without ``tab`` the Timeline and contacts tab
    stays the default. Without ``step`` the resume step is derived (below).
  - ``GET /applications/{id}/tailoring/{step}`` (token): the ``#tailoring`` fragment for that
    step; a step other than 1 to 5 answers 404.
  - ``POST /applications/{id}/tailoring/1/check`` start the gap check (budget gate as the fit:
    ``tier`` and the dialog's fields are accepted); ``POST .../1/facts`` ``text`` (and optional
    ``requirement_id``) adds a CONFIRMED fact at once (blank text: 422, nothing stored);
    ``POST .../2/propose`` start the angle proposals; ``POST .../2/angle`` ``name``,
    ``description``, ``version`` and optional ``reuse_angle_id`` saves the angle on the
    workspace (``tailoring_repo.update_workspace``). All need token and origin. Starts answer
    200 (a fragment) when started or when the running task is returned, 409 with a reason when
    the step is not ready or the budget blocks, 200 with the budget dialog when a warning is due.
    ``angle``: missing ``version`` 400, stale 409 (``VersionConflict.message`` shown, nothing
    changes), blank name 422, unknown ``reuse_angle_id`` 422. The workspace row is created by the
    first angle save only (never by a GET or a task); the form's hidden ``version`` is the
    workspace version as last seen (0 when there is no workspace yet).
  - Answers carry the announcement as ``HX-Trigger`` (or ``HX-Trigger-After-Settle``)
    ``{"announce": "<text>"}``: "Gap check started.", "Fact added.", "Angle proposals started.",
    "Angle saved.". The step heading (``tabindex="-1"``, ``data-autofocus``) takes focus after an
    answer with a message, as the fit's heading does.
- ``origin`` of a saved angle: a proposal saved unchanged is ``ai``; edited or written is
  ``user``. Saving an earlier angle unchanged (``reuse_angle_id`` and the same name and
  description) points the workspace at that existing angle (no new row, the row is not changed);
  an edited reuse creates a new ``user`` angle.

Markup
- The application page has ``nav[aria-label="Application sections"]`` with two links, "Timeline
  and contacts" (``/applications/{id}``) and "Tailoring" (``/applications/{id}?tab=tailoring``),
  the current one ``aria-current="page"``. No "Interview prep" link. The timeline tab keeps
  ``.application-timeline``; the tailoring tab shows ``#tailoring`` instead of it.
- ``#tailoring`` (the swap target of every fragment) has ``data-step="N"`` (the open step), a
  stepper ``nav[aria-label="Tailoring steps"] > ol > li[data-step="N"]`` whose text is
  "<title>: <state>" with a link ``href="/applications/{id}?tab=tailoring&step=N"`` (htmx:
  ``hx-get`` of the fragment); the open step's link has ``aria-current="step"``. Titles: Gap
  check, Angle, CV, Cover letter, Submit. State words: "Not started", "Running", "Needs
  attention" (the last task failed), "Needs your choice" (angles proposed, none saved), "Done",
  and "Coming later" (steps 3 to 5, whose body says they come in a later step).
- The open step's heading: ``h4`` "Step N: <title>".
- Resume step (no ``step`` given): 1 until a gap check is stored, then 2 (also once an angle is
  saved). An explicit ``step`` always wins.
- Step 1: ``li[data-requirement-id][data-verdict="met|partial|missing"]`` per requirement, with
  the verdict word and, for met or partial, the user's own fact wording; ``#gap-check-message``
  (only when a must-have is missing) with the code-built sentence; ``li[data-framing]`` per
  framing idea, with "This isn't in your fact bank." when flagged; each partial or missing
  requirement holds ``details > summary`` "Add a fact" with a form (``action`` ends
  ``/tailoring/1/facts``; textarea ``text`` labelled "Fact"; hidden ``requirement_id``; submit
  "Save fact"); ``button#gap-check-start``: "Check gaps", or "Check again" once a result exists.
  A failed task shows ``role="alert"`` with ``message_for_kind(task.error)``. While a task is
  active the step shows ``role="status"`` and polls like the fit.
- Step 2: ``button#angle-propose``: "Propose angles" ("Propose again" once proposed);
  ``li[data-angle]`` per proposal, in order, with name, rationale, "Used in another application"
  when ``reuse_angle_id`` survived, "This isn't in your fact bank." when flagged, and a form
  (``action`` ends ``/tailoring/2/angle``; fields ``name`` and ``description``, labelled
  "Angle name" and "Description", prefilled and editable; hidden ``version`` and
  ``reuse_angle_id``; submit "Use this angle"); a
  ``details > summary`` "Write your own angle" with the same fields and submit "Save angle"
  (``form#angle-form``); the saved angle is shown in ``#chosen-angle`` as "Your angle: <name>".
- Without a current summary the step says "Summarize the job first." with a link to the job;
  without a usable confirmed fact it says "Add confirmed facts" (link "Open facts"). Start POSTs
  then answer 409 and queue nothing.
"""

import html
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from httpx import Response

from app.domain.fit import Requirement
from tests.board_pages import elements
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_app_shell import Element, find_all
from tests.integration.test_board import seeder
from tests.refine_harness import Refining, refining
from tests.tailoring_ai_inputs import FACT_1, FACT_2, FACT_3, HIDDEN, REQUIREMENTS

GAP_CHECK = "gap_check"
PROPOSE_ANGLES = "propose_angles"
DRAFT_TEXT = "An unconfirmed draft about Kubernetes."
ARCHIVED_TEXT = "An archived fictional fact about forklifts."
OLD_STAMP = "2026-10-19T09:00:00+00:00"
CURRENT_STAMP = "2026-10-20T09:00:00+00:00"
SUMMARY_TEXT = "Plan delivery routes for a fictional harbor depot."
TITLES = {1: "Gap check", 2: "Angle", 3: "CV", 4: "Cover letter", 5: "Submit"}


def page_url(aid: str, step: int | None = None) -> str:
    return f"/applications/{aid}?tab=tailoring" + (f"&step={step}" if step is not None else "")


def fragment_url(aid: str, step: int | str) -> str:
    return f"/applications/{aid}/tailoring/{step}"


def action_url(aid: str, step: int, action: str) -> str:
    return f"/applications/{aid}/tailoring/{step}/{action}"


def seed_summary(
    db: Any,
    job_id: str,
    requirements: tuple[Requirement, ...] = REQUIREMENTS,
    *,
    summary_id: str = "summary-current",
    summary: str = SUMMARY_TEXT,
    created_at: str = CURRENT_STAMP,
) -> None:
    """A summary of the job with its requirements (kept ids), written straight to storage."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO job_summaries (id,job_id,summary,fields_json,provider,model,prompt_id,"
            "prompt_version,task_id,created_at) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
            (summary_id, job_id, summary, "{}", "fake", "fake-thorough", "summarize_job", "1",
             created_at),
        )  # fmt: skip
        for position, item in enumerate(requirements, 1):
            conn.execute(
                "INSERT INTO job_requirements (id,summary_id,position,kind,text) "
                "VALUES (?,?,?,?,?)",
                (item.id, summary_id, position, item.kind, item.text),
            )


def seed_facts(facts: Any) -> None:
    """F1 to F3 usable, F4 never-use, F5 an unconfirmed draft: the numbers the AI fixtures use."""
    from app.services.facts import DraftInput, DraftOrigin, FactInput

    for text in (FACT_1, FACT_2, FACT_3):
        facts.create(FactInput(text=text))
    facts.create(FactInput(text=HIDDEN, privacy="never_use"))
    origin = DraftOrigin(source="cv_import", provider="fake", model="fake-thorough")
    facts.create_drafts(
        origin, [DraftInput(text=DRAFT_TEXT, skills=(), original_answer=DRAFT_TEXT)]
    )


# --- reading answers --------------------------------------------------------------------------


def announced(response: Response) -> list[str]:
    """The ``announce`` texts of the answer's HX-Trigger headers."""
    found: list[str] = []
    for name in ("HX-Trigger", "HX-Trigger-After-Settle"):
        raw = response.headers.get(name)
        if raw:
            value = json.loads(raw)
            if isinstance(value, dict) and "announce" in value:
                found.append(str(value["announce"]))
    return found


def said_text(page: str) -> str:
    """Visible-ish text with entities decoded and whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def by_id(page: str, element_id: str) -> Element:
    found = find_all(elements(page), lambda e: e.attrs.get("id") == element_id)
    assert len(found) == 1, f"expected one #{element_id}, found {len(found)}"
    return found[0]


def has_id(page: str, element_id: str) -> bool:
    return any(e.attrs.get("id") == element_id for e in elements(page))


def requirement_items(page: str) -> dict[str, Element]:
    return {
        str(e.attrs["data-requirement-id"]): e
        for e in elements(page)
        if e.tag == "li" and "data-requirement-id" in e.attrs
    }


def verdicts(page: str) -> dict[str, str]:
    return {rid: str(item.attrs["data-verdict"]) for rid, item in requirement_items(page).items()}


def framing_items(page: str) -> list[Element]:
    return [e for e in elements(page) if e.tag == "li" and "data-framing" in e.attrs]


def angle_items(page: str) -> list[Element]:
    return [e for e in elements(page) if e.tag == "li" and "data-angle" in e.attrs]


def stepper(page: str) -> dict[int, Element]:
    return {
        int(str(e.attrs["data-step"])): e
        for e in elements(page)
        if e.tag == "li" and "data-step" in e.attrs
    }


def open_step(page: str) -> int:
    return int(str(by_id(page, "tailoring").attrs["data-step"]))


def inside(page: str, container: Element, predicate: Callable[[Element], bool]) -> list[Element]:
    return [e for e in elements(page) if e.within(container) and predicate(e)]


def hidden_value(page: str, container: Element, name: str) -> str:
    found = inside(page, container, lambda e: e.tag == "input" and e.attrs.get("name") == name)
    assert len(found) == 1, f"expected one hidden {name} in the form, found {len(found)}"
    return str(found[0].attrs.get("value", ""))


def form_values(page: str, form: Element) -> dict[str, str]:
    """What the browser would post for ``form`` as the page fills it in (inputs and textareas)."""
    values: dict[str, str] = {}
    for e in inside(page, form, lambda e: e.tag in ("input", "textarea")):
        name = e.attrs.get("name")
        if name:
            values[str(name)] = str(e.attrs.get("value") or "") if e.tag == "input" else e.text
    return values


def forms_in(page: str, container: Element) -> list[Element]:
    return inside(page, container, lambda e: e.tag == "form")


# --- the running app ----------------------------------------------------------------------------


class Tailoring:
    """Wraps ``Refining`` (finished setup, fake provider, spy, real task runner)."""

    def __init__(self, r: Refining) -> None:
        self.r = r
        self._seeder = seeder(r)  # one: it numbers the companies it writes

    @property
    def spy(self) -> Any:
        return self.r.run.spy

    @property
    def db(self) -> Any:
        return self.r.run.app.state.workspace.db

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return self.r.run.rows(sql, params)

    # --- seeding ------------------------------------------------------------------------------

    def ready(
        self,
        job_id: str = "tailoring-job",
        *,
        summary: bool = True,
        facts: bool = True,
        posting: str | None = None,
    ) -> Any:
        """An application for a job with a current summary (``REQUIREMENTS``) and facts F1 to F5."""
        application = self._seeder.application(
            job_id, "Route Planner", "Harbor Kite Fictional", "preparing"
        )
        if posting is not None:
            self.r.run.execute("UPDATE jobs SET snapshot_text=? WHERE id=?", (posting, job_id))
        if summary:
            if self.rows("SELECT id FROM job_requirements"):  # ids are unique: another job
                other = tuple(
                    Requirement(f"{job_id}-{item.id}", item.kind, item.text)
                    for item in REQUIREMENTS
                )
                seed_summary(self.db, job_id, other, summary_id=f"summary-{job_id}")
            else:
                seed_summary(self.db, job_id)
        if facts and not self.rows("SELECT id FROM facts"):
            seed_facts(self.r.facts)
            numbers = [row["number"] for row in self.rows("SELECT number FROM facts ORDER BY 1")]
            assert numbers == [1, 2, 3, 4, 5], numbers
        return application

    def another_application(self, job_id: str) -> Any:
        return self._seeder.application(job_id, "Other Planner", "Other Fictional Co", "applied")

    def earlier_angle(
        self,
        angle_id: str,
        job_id: str,
        *,
        name: str,
        updated_at: str,
        description: str | None = None,
    ) -> None:
        """An angle chosen for ANOTHER application: its job, application, angle and workspace."""
        other = self.another_application(job_id)
        self.r.run.execute(
            "INSERT INTO angles (id,name,description,origin,created_at,updated_at) "
            "VALUES (?,?,?,'user',?,?)",
            (angle_id, name, description or f"Lead with {name.lower()}.", OLD_STAMP, OLD_STAMP),
        )
        self.r.run.execute(
            "INSERT INTO workspaces (id,application_id,angle_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (f"workspace-{angle_id}", other.id, angle_id, OLD_STAMP, updated_at),
        )

    def add_fact(self, text: str, **fields: Any) -> Any:
        return self.r.typed(text, **fields)

    # --- requests -----------------------------------------------------------------------------

    def page(self, aid: str, step: int | None = None, **headers: str) -> Response:
        """The token-free page (no token header)."""
        return self.r.client.get(page_url(aid, step), headers=headers)

    def fragment(self, aid: str, step: int | str) -> Response:
        return self.r.client.get(fragment_url(aid, step), headers=self.r.run.run.htmx())

    def post(self, aid: str, step: int, action: str, **values: str) -> Response:
        headers = self.r.run.run.action(**{"HX-Request": "true"})
        return self.r.client.post(action_url(aid, step, action), data=values, headers=headers)

    def post_fields(self, path: str, values: dict[str, str]) -> Response:
        return self.r.client.post(
            path, data=values, headers=self.r.run.run.action(**{"HX-Request": "true"})
        )

    # --- the provider -------------------------------------------------------------------------

    def script(self, task: str, *names: str) -> None:
        self.spy.script(task, *(load_ai_fixture(task, name) for name in names))

    def requests(self, task: str) -> list[Any]:
        return [request for request in self.spy.requests if request.task == task]

    # --- tasks --------------------------------------------------------------------------------

    def latest_task(self, task_type: str, aid: str) -> dict[str, Any]:
        found = self.rows(
            "SELECT * FROM tasks WHERE dedupe_key=? ORDER BY rowid DESC", (f"{task_type}:{aid}",)
        )
        assert found, f"no {task_type} task for {aid}"
        return found[0]

    def ended(self, task_type: str, aid: str, status: str = "succeeded") -> Any:
        row = self.r.run.wait_terminal(self.latest_task(task_type, aid)["id"])
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return row

    def check(
        self, aid: str, fixture: str | None = "ok", *, status: str = "succeeded", **values: str
    ) -> Any:
        """Script the gap check (``None``: the app's own fake answer), start it by the route and
        wait for the task to end as ``status``."""
        if fixture is not None:
            self.script(GAP_CHECK, fixture)
        started = self.post(aid, 1, "check", **values)
        assert started.status_code == 200, (started.status_code, started.text[:300])
        return self.ended(GAP_CHECK, aid, status)

    def propose(
        self, aid: str, fixture: str | None = "ok", *, status: str = "succeeded", **values: str
    ) -> Any:
        if fixture is not None:
            self.script(PROPOSE_ANGLES, fixture)
        started = self.post(aid, 2, "propose", **values)
        assert started.status_code == 200, (started.status_code, started.text[:300])
        return self.ended(PROPOSE_ANGLES, aid, status)

    def angle_version(self, aid: str) -> str:
        """The workspace version the step-2 form of the page carries."""
        shown = self.page(aid, 2).text
        return hidden_value(shown, by_id(shown, "angle-form"), "version")


@contextmanager
def tailoring_app(tmp_path: Any) -> Iterator[Tailoring]:
    """Each test file wraps this in its own ``tw`` fixture."""
    with refining(tmp_path) as r:
        yield Tailoring(r)
