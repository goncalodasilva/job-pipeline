"""The running app, assumed interface, markup hooks and readers for the M6.9 cover letter tests
(`draft_cover_letter`, step 4 of the Tailoring tab; AC-M6.1 to AC-M6.4 for letters; spec 3.1, 3.2,
3.6; ADR-106 items 5, 8, 9, 11, 12; ADR-113; ADR-114). Every value is fictional. No application
module of M6.9 is imported at module level, so a missing service, task or route fails the test that
needs it, not the collection.

The letter works as the CV does (``tests/document_pages.py`` and ``tests/cv_step_pages.py``): the
same stored tables (``documents`` with kind ``cover_letter``; ``document_versions``,
``document_units``, ``document_unit_facts``, ``document_sent_facts``), the same checks, merge, edit
rules and version rules. What differs is written below. Anything not written here is as for the CV.

ASSUMED INTERFACE (the implementer follows it)

Task and wiring
- Prompt ``prompts/draft_cover_letter`` (Thorough, kind ``generation`` in ``TASK_SPECS``, no tools).
  Inputs ``job_context``, ``facts``, ``angle``, ``tone``, ``length``, ``note``, ``keep_units`` and
  ``job_id``. The job context holds the posting, the requirements and the job's title and company
  (the model may name them). ``tone`` is ``warm|formal|direct`` and ``length`` is
  ``short|medium|long``; both reach the request. Output (strict schema): ``{"paragraphs": [{"text",
  "fact_ids", "added_details"}]}``, one to eight paragraphs, ``text`` 1 to 1200 characters on the
  same plain-text pattern as the CV schema, ``fact_ids`` as in the CV schema, no other property
  anywhere (a ``status`` or ``approved`` property is invalid).
- Task type ``draft_cover_letter`` (label "Draft cover letter"), handler
  ``app/tasks/documents.py::draft_cover_letter_handler(service)``, dedupe key
  ``draft_cover_letter:<application id>``; the whole result is written by the writer that
  ``ctx.complete`` runs (never before) or not at all, like ``tailor_cv``.
- ``app/services/letters.py``: ``LetterService`` on ``app.state.letters``. It shares the stored
  documents of ``DocumentService`` (same repository, same ``merge_regenerated``, ``check_units`` and
  ``approval_blockers``), and raises the exception classes of ``app.services.documents``
  (``GenerateNotReady``, ``ReplaceNotConfirmed``, ``GenerateInProgress``, ``NothingToApprove``,
  ``ApprovalBlocked``, ``UnitInvalid``, ``UnitNotFound``; ``VersionConflict`` of
  ``app.domain.errors``). Module constants: ``BAD_TONE`` = "Choose a warm, formal or direct tone.",
  ``BAD_LENGTH`` = "Choose a short, medium or long letter.".
- Fake provider: ``draft_cover_letter_scripts()`` in ``app/modules/ai/providers/job_scripts.py``,
  registered in ``app/wiring.py`` (``provider.script_keywords("draft_cover_letter", ...)``). Its
  well-behaved default restates, word for word, the first two usable facts it was sent, one
  paragraph each, each cited by its id and starting "Practice data. ". Note keywords:
  ``fake-letter-malformed`` (raw text), ``fake-letter-status`` (a ``status`` property, invalid),
  ``fake-letter-invented`` (one paragraph "Led a team of 50 people at Imaginary Aerospace Corp."
  citing ``F1@1``, labelled like the rest).

``LetterService`` (blocking; ``application_id`` first)
- ``plan_start(application_id, *, tier=None) -> SummaryPlan`` as ``DocumentService.plan_start``;
  ``GenerateNotReady`` with ``reason`` in ``summary|requirements|facts`` only (the messages of
  ``app.services.tailoring``: ``NO_SUMMARY``, ``NO_REQUIREMENTS``, ``NO_FACTS``). A letter needs
  neither a CV country nor an approved template, and no CV.
- ``start_generate(application_id, *, tone=None, length=None, note=None, replace_all=False,
  confirm_replace=False, tier=None) -> TaskRef``. ``tone`` None is the tone of the current version,
  else ``warm``; ``length`` None is the length of the current version, else ``medium``. A tone or
  length outside the sets is a ``ValueError`` whose ``str`` is ``BAD_TONE`` / ``BAD_LENGTH``
  (nothing queued, no request). The note is trimmed, at most 500 characters, without control or
  bidi characters (as the CV's). ``ReplaceNotConfirmed`` first when ``replace_all`` and not
  ``confirm_replace``. A task already queued or running for the application is returned when it was
  asked the same, else ``GenerateInProgress`` ("A letter is already being drafted. Wait for it to
  finish."). The service does not ask the budget gate; the route does.
- The request holds the sent facts (``facts_for_ai`` only: no never-use, draft or archived fact),
  the chosen angle, the job's title and company, the note, the kept paragraphs (user-written or
  removed ones of the current version) and ``tone`` and ``length`` as words. It holds no personal
  detail (name, email, phone, location, link, photo) and none of the code-owned frame (below).
- The written version: kind ``cover_letter`` document, one per application;
  ``origin`` ``ai``, ``template_snapshot`` NULL, ``tone`` and ``length`` columns set,
  ``angle_text``, ``note``, provider, model, ``prompt_id`` ``draft_cover_letter``,
  ``prompt_version``, ``task_id``,
  its ``document_sent_facts`` and one unit per paragraph: ``kind`` ``content``, ``section``
  ``body``, no role, ``origin`` ``ai``, citations for every cited ref that is a stored fact
  version, ``flags`` of ``check_units`` and the model's ``added_details``.
- Checking: as ``check_units`` with ONE addition: the job's company and the job's title (as stored
  on the job) are allowed name sources for a letter paragraph, and for nothing else (quantities,
  skills and every other name stay checked against the cited facts; a CV is never given them). A
  unit that cites an unsent id is ``unsupported``; an uncited paragraph is ``unsupported``.
- ``view(application_id, version_id=None) -> LetterView``: reads only. Fields of ``DocumentView``
  except ``diff``, ``template``, ``sections`` and ``contact``: ``application_id``, ``document_id``,
  ``version``, ``versions``, ``units`` (``UnitView``, section ``body``), ``blockers``,
  ``practice_notice``, ``task`` (state of ``draft_cover_letter:<id>``); plus ``tone`` and ``length``
  (of the shown version, else None), ``job_title``, ``company`` and ``frame``.
  ``LetterFrame``: ``greeting`` (non-empty; a greeting line), ``opening`` (one sentence that names
  the job's title and company), ``closing`` (non-empty sign-off phrase) and ``name`` (the saved
  ``personal_details`` name, "" when none). All four are built by code at view time, never stored
  in a unit and never sent to a request, and no AI output can change them.
- ``edit_unit``, ``add_unit(application_id, version_id, text, *, fact_ids=(), added_details=())``
  (no section and no role: always section ``body``, placed last), ``remove_unit``,
  ``resolve_flag_confirm``, ``approve``: as the CV's. ``ApprovalBlocked.message`` counts
  paragraphs: "1 paragraph needs your check." / "N paragraphs need your check.". A confirmation
  creates a confirmed fact (source ``document``, no role, no skills) that the paragraph then cites.

Routes (``{id}`` application id, ``{uid}`` unit id, ``{vid}`` document version id; the rules for
ids, errors, tokens, origin, ``version``, 400/404/409/422 are those of step 3 in
``tests/cv_step_pages.py``, with the ids and texts below)
- ``GET /applications/{id}?tab=tailoring&step=4[&version={vid}]`` (token-free page). Rule 13: the
  page holds no personal detail; its ``#letter-frame`` is a lazy placeholder
  ``<div id="letter-frame" hx-get="/applications/{id}/tailoring/4/frame" hx-trigger="load">``.
- ``GET /applications/{id}/tailoring/4`` (fragment ``#tailoring`` with the frame filled),
  ``.../4/version/{vid}`` (read-only), ``.../4/frame`` (``#letter-frame`` alone). Token needed.
- ``POST .../4/generate`` (``tone``, ``length``, ``note``, ``replace_all``, ``confirm_replace`` and
  the budget dialog's fields), ``.../4/unit/{uid}/edit`` (``version``, ``text``),
  ``.../4/unit/{uid}/remove``, ``.../4/unit/{uid}/confirm``, ``.../4/unit/add`` (``version``,
  ``text``), ``.../4/approve`` (``version``). Token and origin needed. A citation is never posted.
- Announcements: generate "Letter drafting started."; edit "Paragraph saved."; with a new claim
  "Paragraph saved. Save this as a fact?"; add "Paragraph added."; remove "Paragraph removed.";
  confirm "Saved as a fact. The paragraph now cites it."; approve "Letter approved."; on an approved
  version "Saved as a new draft. The approved version is unchanged."; a self-refresh after the task
  succeeded "Letter ready to review.".
- ``tone``, ``length`` and ``note`` may each be absent from a generate post (the service
  defaults apply); the budget dialog fields are accepted too.
- A bad tone or length is a 422 (the select ``aria-invalid="true"``, ``aria-describedby`` naming a
  visible element with ``BAD_TONE`` / ``BAD_LENGTH``).

Markup of step 4 inside ``#tailoring[data-step="4"]`` (``data-keep-edits``, own ``hx-get``, as step
3). The step is a ``section#letter-step`` with ``data-state`` (``none|running|failed|draft|
approved``), ``data-version-id``, ``data-version-number`` (absent without a version) and
``data-readonly``; ``h5#letter-heading[tabindex=-1]`` "Cover letter". Stepper state words for step
4: "Not started", "Running", "Needs attention", "Needs your check", "Ready to approve", "Done"
(never "Coming later" and no "later step" text any more).
- ``p#letter-optional``: says the letter is optional. ``a#letter-next``: while no version of the
  letter is approved the text "Continue without a letter", afterwards "Continue to Submit"; its
  href is the step-5 page address. Generating a letter is never required for anything.
- ``form#letter-generate-form`` (when ready, not running, not an old version):
  ``select#letter-tone[name=tone]`` labelled "Tone" with options (warm, Warm), (formal, Formal),
  (direct, Direct); ``select#letter-length[name=length]`` labelled "Length" with (short, Short),
  (medium, Medium), (long, Long); the selected options are the shown version's tone and length,
  else warm and medium; ``textarea#letter-note`` labelled "Note for the letter (optional)"; when a
  version exists ``input#letter-replace-all[name=replace_all][value=1]`` "Replace everything,
  including my edits" and ``input#letter-confirm-replace[name=confirm_replace][value=1]`` "Yes,
  replace my edits too"; ``button#letter-generate-start`` "Draft the letter" or "Regenerate".
- Not ready: ``p#letter-not-ready`` with the exception message verbatim and
  ``a#letter-not-ready-link`` ("Open job" ``/jobs/{job id}`` for summary and requirements,
  "Open facts" ``/facts``).
- Running ``p#letter-running[role=status]`` "Drafting your letter…". Failed ``p#letter-error[role=
  alert]`` with ``message_for_kind``. ``p#letter-practice.badge`` holds the practice notice.
  ``p#letter-status``: "Draft, version N. Not approved yet." / "Approved, version N." / "Version N
  (read-only).". ``ol#letter-versions`` as ``#cv-versions``. ``p#letter-stale-note[data-stale-note]
  [hidden]``.
- ``div#letter-frame`` (filled by the fragment and by ``.../4/frame``): ``p[data-letter-greeting]``,
  ``p[data-letter-opening]``, ``p[data-letter-closing]``, ``p[data-letter-name]``, in that DOM
  order, text = the ``LetterFrame`` fields; no form, input, textarea or button inside it
  (code-built, never editable); ``a[href="/settings/your-details"]`` "Change in Settings";
  ``p#letter-name-missing`` instead of ``[data-letter-name]`` when there is no name. The
  paragraphs sit between the opening and the closing in the DOM (greeting, opening,
  ``ol#letter-paragraphs``, closing, name).
- ``ol#letter-paragraphs > li[data-line][data-unit-id][data-origin]`` (``id="line-<uid>"``,
  ``tabindex="-1"``, ``data-flagged="true"`` when it has a flag or a blocker); no diff mark and no
  master letter. Inside: ``p[data-line-text]#line-text-<uid>`` (escaped); ``[data-origin-label]``
  "Edited by you" for user paragraphs; ``ul[data-flags] > li[data-flag]`` with
  ``[data-flag-label]`` / ``[data-flag-claim]`` as in the CV step; the facts used by THIS paragraph:
  ``details[data-line-facts] > summary`` "Facts used (N)" with ``ul[data-fact-list] > li > a``
  (``href="/facts/<fact id>/edit"``, text = the user's wording of the cited version), or
  ``[data-no-facts]`` "No facts cited."; actions as in the CV step (``details[data-line-edit]``,
  ``form[data-line-form=edit|remove|confirm]`` (the edit ``textarea[name=text]`` is labelled
  "Edit this paragraph"), buttons "Save paragraph", "Remove",
  "This is true — save it as a fact"); none while ``data-readonly="true"``.
- ``details#letter-add`` "Add a paragraph" with ``form#letter-add-form`` (hidden ``version``,
  ``textarea[name=text]`` labelled "New paragraph", submit "Add paragraph"; no section or role).
- ``button#letter-approve`` "Approve letter" (draft, not read-only); disabled with
  ``p#letter-approve-reason`` ("1 paragraph needs your check." / "N paragraphs need your check." /
  "There is nothing to approve yet.") as the CV's; ``p#letter-approved`` "Approved. Changing a
  paragraph starts a new draft." when approved.
- ``dialog#letter-save-fact`` (``role="dialog" aria-modal="true"``, title "Save this as a fact?",
  ``ul#letter-save-fact-claims``, a form posting to the confirm route of the NEW version's
  paragraph, "Yes, save it as a fact", "Not now" with ``autofocus``).
"""

import html
import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from httpx import Response

from tests.board_pages import elements
from tests.cv_step_pages import CvStep
from tests.integration.test_app_shell import Element
from tests.refine_harness import refining
from tests.tailoring_ai_inputs import FACT_1, FACT_2, FACT_3, FACT_7
from tests.tailoring_pages import by_id, forms_in, hidden_value, inside

DRAFT_COVER_LETTER = "draft_cover_letter"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai" / DRAFT_COVER_LETTER
COMPANY = "Harbor Kite Fictional"
TITLE = "Route Planner"
BAD_TONE = "Choose a warm, formal or direct tone."
BAD_LENGTH = "Choose a short, medium or long letter."

# The paragraphs of ``ok.json`` and the facts each cites.
P1 = "I planned 60 delivery routes a week with a team of 3."
P2 = "I also cut invoice processing time by 35% across 4 warehouses."
P3 = "I speak English at work every day, and I scheduled 12 vans for the night shift."
OK_PARAGRAPHS = [P1, P2, P3]
OK_CITED = [["F1@1"], ["F2@1"], ["F3@1", "F7@1"]]
OK_FACT_TEXTS = [[FACT_1], [FACT_2], [FACT_3, FACT_7]]
KEPT_PARAGRAPH = "I planned 60 delivery routes a week with a team of 3, mostly at night."
GLOBEX_P = "Led route planning at Globex Corporation."
INFLATED_P = "Cut invoice processing time by 85% across 14 warehouses."
UNKNOWN_ID_P = "Managed a regional planning office."
UNCITED_P = "A seasoned leader known for turning around struggling teams."
COMPANY_P = (
    "Planned 60 delivery routes a week with a team of 3, "
    "which is what the Route Planner role at Harbor Kite Fictional asks for."
)
COMPANY_AND_GLOBEX_P = (
    "Planned 60 delivery routes a week for Harbor Kite Fictional and Globex Corporation."
)
NEW_CLAIM_P = "I planned 60 delivery routes a week with a team of 9 and a budget of $2m."
LETTER_ANNOUNCE = {
    "started": "Letter drafting started.",
    "edit": "Paragraph saved.",
    "offer": "Paragraph saved. Save this as a fact?",
    "add": "Paragraph added.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Letter approved.",
    "new_draft": "Saved as a new draft. The approved version is unchanged.",
    "ready": "Letter ready to review.",
}


def injection_letter_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def letter_url(aid: str, action: str) -> str:
    return f"/applications/{aid}/tailoring/4/{action}"


def letter_unit_url(aid: str, uid: str, action: str) -> str:
    return letter_url(aid, f"unit/{uid}/{action}")


def letter_page_url(aid: str, version: str | None = None) -> str:
    return f"/applications/{aid}?tab=tailoring&step=4" + (f"&version={version}" if version else "")


# --- readers --------------------------------------------------------------------------------------


def letter_step(page: str) -> Element:
    return by_id(page, "letter-step")


def paragraphs(page: str) -> list[Element]:
    """Every ``li[data-line]`` of the step, in DOM order."""
    box = letter_step(page)
    return [e for e in elements(page) if e.within(box) and e.tag == "li" and "data-line" in e.attrs]


def paragraph_by_unit(page: str, unit_id: str) -> Element:
    found = [p for p in paragraphs(page) if p.attrs.get("data-unit-id") == unit_id]
    assert len(found) == 1, f"expected one paragraph for unit {unit_id}, found {len(found)}"
    return found[0]


def letter_state(page: str) -> str:
    return str(letter_step(page).attrs["data-state"])


def letter_version_id(page: str) -> str:
    return str(letter_step(page).attrs["data-version-id"])


def frame_parts(page: str) -> dict[str, str]:
    """The text of each code-built part of ``#letter-frame``, by name."""
    frame = by_id(page, "letter-frame")
    found: dict[str, str] = {}
    for part in ("greeting", "opening", "closing", "name"):
        nodes = inside(page, frame, lambda e, key=f"data-letter-{part}": key in e.attrs)  # type: ignore[misc]
        if nodes:
            found[part] = nodes[0].text
    return found


def options_of(page: str, element_id: str) -> list[tuple[str, str]]:
    """(value, text) of the options of the select with this id."""
    select = by_id(page, element_id)
    return [
        (str(e.attrs.get("value", "")), e.text)
        for e in inside(page, select, lambda e: e.tag == "option")
    ]


def selected_value(page: str, element_id: str) -> str | None:
    select = by_id(page, element_id)
    chosen = [
        e for e in inside(page, select, lambda e: e.tag == "option" and "selected" in e.attrs)
    ]
    return str(chosen[0].attrs.get("value", "")) if chosen else None


def letter_autofocus(page: str) -> list[Element]:
    return [e for e in elements(page) if "data-autofocus" in e.attrs]


def letter_hidden_version(page: str, container: Element) -> str:
    return hidden_value(page, container, "version")


def unescaped_in_letter(page: str, hostile: str) -> bool:
    """Whether the hostile text appears raw in the page (it must not)."""
    return hostile in page and html.escape(hostile, quote=False) not in page


# --- the running app ------------------------------------------------------------------------------


class LetterDesk(CvStep):
    """The CV step harness plus the letter. ``letter_ready`` seeds an application that has a current
    summary and the CV facts F1 to F8 and NO approved template (a letter needs none)."""

    # --- seeding and the service -----------------------------------------------------------------

    def letter_ready(self, job_id: str = "letter-job", **options: Any) -> Any:
        options.setdefault("template", False)
        return self.cv_ready(job_id, **options)

    @property
    def letters_mod(self) -> Any:
        return importlib.import_module("app.services.letters")

    @property
    def letters(self) -> Any:
        return self.r.run.app.state.letters

    def draft(
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
            self.script(DRAFT_COVER_LETTER, fixture)
        ref = self.letters.start_generate(aid, **options)
        row = self.r.run.wait_terminal(ref.id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return row

    def draft_by_route(self, aid: str, fixture: str | None = "ok", **values: str) -> Response:
        if fixture is not None:
            self.script(DRAFT_COVER_LETTER, fixture)
        started = self.post_letter(aid, "generate", values)
        assert started.status_code == 200, (started.status_code, started.text[:300])
        self.ended(DRAFT_COVER_LETTER, aid)
        return started

    # --- what is stored --------------------------------------------------------------------------

    def letter_document(self, aid: str) -> dict[str, Any] | None:
        found = self.rows(
            "SELECT * FROM documents WHERE application_id=? AND kind='cover_letter'", (aid,)
        )
        return found[0] if found else None

    def letter_versions(self, aid: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT v.* FROM document_versions v JOIN documents d ON d.id=v.document_id "
            "WHERE d.application_id=? AND d.kind='cover_letter' ORDER BY v.number",
            (aid,),
        )

    def current_letter_id(self, aid: str) -> str:
        document = self.letter_document(aid)
        assert document is not None, "no letter drafted yet"
        return str(document["current_version_id"])

    def paragraph_id(self, aid: str, text: str) -> str:
        return str(self.unit_with(self.current_letter_id(aid), text)["id"])

    def request_for_letter(self) -> Any:
        (request,) = self.requests(DRAFT_COVER_LETTER)
        return request

    def tasks_of(self, task_type: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM tasks WHERE type=?", (task_type,))

    def stored_units_text(self) -> str:
        return json.dumps(self.rows("SELECT text FROM document_units"), ensure_ascii=False)

    # --- reading ---------------------------------------------------------------------------------

    def step4(self, aid: str, version: str | None = None, **headers: str) -> Response:
        path = (
            letter_url(aid, f"version/{version}") if version else f"/applications/{aid}/tailoring/4"
        )
        return self.r.client.get(path, headers=self.r.run.run.htmx(**headers))

    def page4(self, aid: str, version: str | None = None) -> Response:
        return self.r.client.get(letter_page_url(aid, version))

    def frame(self, aid: str) -> Response:
        return self.r.client.get(letter_url(aid, "frame"), headers=self.r.run.run.htmx())

    # --- writing ---------------------------------------------------------------------------------

    def post_letter(self, aid: str, action: str, values: dict[str, str] | None = None) -> Response:
        return self.post_fields(letter_url(aid, action), values or {})

    def post_paragraph(
        self, aid: str, uid: str, action: str, values: dict[str, str] | None = None
    ) -> Response:
        return self.post_fields(letter_unit_url(aid, uid, action), values or {})

    def letter_version_now(self, aid: str) -> str:
        return letter_version_id(self.step4(aid).text)

    def edit_paragraph(self, aid: str, uid: str, text: str) -> Response:
        return self.post_paragraph(
            aid, uid, "edit", {"version": self.letter_version_now(aid), "text": text}
        )

    def remove_paragraph(self, aid: str, uid: str) -> Response:
        return self.post_paragraph(aid, uid, "remove", {"version": self.letter_version_now(aid)})

    def confirm_paragraph(self, aid: str, uid: str) -> Response:
        return self.post_paragraph(aid, uid, "confirm", {"version": self.letter_version_now(aid)})

    def add_paragraph(self, aid: str, text: str) -> Response:
        return self.post_letter(
            aid, "unit/add", {"version": self.letter_version_now(aid), "text": text}
        )

    def approve_letter_now(self, aid: str) -> Response:
        return self.post_letter(aid, "approve", {"version": self.letter_version_now(aid)})

    def forms(self, page: str, container: Element) -> list[Element]:
        return forms_in(page, container)


@contextmanager
def letter_app(tmp_path: Any) -> Iterator[LetterDesk]:
    """Each test file wraps this in its own fixture."""
    with refining(tmp_path) as r:
        yield LetterDesk(r)
