"""The running app, routes, markup readers and fictional data for the M6.6 country CV template tests
(ADR-106 items 3, 6 and 12; spec 3.6; TD-280). Every value is fictional. The application modules
are not imported here (only the repo's own test helpers and the pure domain), so a missing service
or route fails the test that needs it.

Assumed interface (the implementer matches it; the integration and e2e tests share these names).
It builds on ``tests/tailoring_pages.py`` (the workspace harness) and on the country select and
``POST /applications/{id}/tailoring/country`` of ``tests/integration/test_application_country.py``.

Service, task, storage
- ``app/services/cv_templates.py``: ``CvTemplateService`` on ``app.state.cv_templates`` (built like
  ``TailoringService``: factory, clock, ids, workspace, settings, tasks, usage). Methods the tests
  call: ``start_draft(country_code, style: TemplateStyle, note: str | None, *, tier=None) ->
  TaskRef | None`` (enqueues the task type ``draft_cv_template`` with the dedupe key
  ``template_dedupe_key(code)`` = ``draft_cv_template:<CC>``, so a second start for the country
  returns the running task; ``None`` for a code that is not in the country list) and
  ``approved(country_code) -> TemplateSpec | None`` (only an APPROVED row; M6.7 reads it).
- ``app/tasks/cv_templates.py``: ``draft_cv_template_handler(service)`` registered in
  ``create_app`` for the task type ``draft_cv_template``; it calls ``run_draft_cv_template``
  (``app/services/template_ai.py``) with the code, the country NAME, the style and the note the
  start stored in the task, and writes with ``ctx.complete`` (the row is written in the
  transaction that marks the task succeeded, and nowhere else). A failed or cancelled task
  writes nothing, so an existing row (approved or not) is untouched.
- ``cv_templates`` row (migration 0006, no new migration): ``country`` the upper-case code;
  ``spec`` the JSON text of ``TemplateDraft.to_data()``, i.e. ``{"template": {...closed spec...},
  "notes": {six notes}}``; provenance is in the discrete columns (provider ``fake`` marks practice
  data, model, prompt_id, prompt_version, task_id). Consumers (document snapshots, the renderer)
  never unwrap it themselves: ``tailoring_repo.template_spec(spec_json: str) -> TemplateSpec``
  (``app/modules/storage/tailoring_repo.py``) returns the validated closed spec from the column's
  text, ignoring the notes, and raises ``ValueError`` for text that is not a valid spec.
  A new draft is ``status='draft'``, ``approved_at`` NULL, ``version`` 1, provenance (provider,
  model, prompt_id ``draft_cv_template``, prompt_version, task_id). A REDRAFT of an existing
  country UPDATEs the same row (``version`` + 1): new spec and notes, ``status='draft'``,
  ``approved_at`` NULL, new provenance. An approval sets ``status='approved'`` and ``approved_at``
  to the clock's now (ISO 8601); any edit of the spec UPDATEs back to ``draft`` with
  ``approved_at`` NULL. Every update checks ``version`` (``VersionConflict``).

Routes (token for every route, origin too for POST; none of them is a page). ``<CC>`` is a code in
the country list of the CV country select (at least PT Portugal, DE Germany, US, GB, FR France);
any other code (``XX``, ``pt``, ``PRT``) is a plain-text 404 that changes nothing.
- ``GET /settings/cv-templates``: the ``#templates-settings`` section (below).
- ``GET /settings/cv-templates/<CC>``: the ``#cv-template`` unit (below). Read only; the header
  ``HX-Trigger: cv-template`` (the unit's own id, as htmx sends it when the unit polls itself)
  makes it announce "Template draft ready." once, when its latest task has succeeded.
- ``POST /settings/cv-templates/<CC>/draft``: URL-encoded ``pages`` ("1" to "3"; default "2"),
  ``photo`` (``none|optional|expected``; default "optional"), ``note`` (optional, at most 300
  characters) plus the budget dialog's fields (``tier`` and the confirmation fields, as the gap
  check). Through the budget gate: 200 with the unit and "Template draft started." when started
  or when the country's task is already running (one task per country); 200 with the budget dialog
  (text "Continue") and nothing started when a warning is due; 409 with the reason when blocked
  (the button ``#template-draft-start`` is then ``aria-disabled="true"``). A style or note
  refused by ``TemplateStyle`` / ``MAX_NOTE_CHARS`` is a 422 tied to ``pages``, ``photo`` or
  ``note`` that starts nothing, keeps the typed note in the textarea and announces "Draft not
  started. 1 field needs a change."; an unknown or repeated field is a plain-text 400
  "This request isn't valid.".
- ``POST /settings/cv-templates/<CC>``: the review form (below): ``version``, ``action`` (``save``
  or ``approve``) and the spec fields. 404 when the country has no template row. 400 for an unknown
  or repeated field (a ``date_of_birth``, a ``detail_nationality``), an ``action`` that is neither,
  and a missing or non-numeric ``version``. 409 for a stale ``version``: the unit with the reload
  prompt (``VersionConflict.message`` and a "Reload" link to ``/settings/your-details``), nothing
  changes. 422
  for a value the closed spec refuses: nothing changes, the unit shows the SAVED spec's markers
  but keeps what was typed in every control that can hold it, and the refusal copy is fixed per
  field (never an entered value or pydantic text). 200: ``save`` stores the spec (status ``draft``,
  approved_at NULL) and announces "Template saved. Approve it to use it."; ``approve`` stores the
  posted spec AND approves it, announcing "Template approved.". Every 200/409/422 answer is the
  unit, ``HX-Trigger`` ``{"announce": "<text>", "templates-changed": "<CC>"}`` on a change (the
  Settings list refreshes itself on that event). A 422 announces "Template not saved. N field(s)
  need(s) a change." (``approve``: "Template not approved. ...", "1 field needs a change." for
  one).

Markup. ``#cv-template`` is the unit (the swap target of every answer, ``hx-swap="outerHTML"``):
``data-country="<CC>"``, ``data-state`` one of ``none`` (no row, no task), ``running`` (a task
is queued or running), ``failed`` (no row and the latest task failed), ``draft``, ``approved``
(the row's status; ``running`` and an alert win over them while a redraft is in flight or has just
failed). It holds:
- ``h4``/``h5`` ``#cv-template-heading`` (``tabindex="-1"``, ``data-autofocus`` after an answer
  with a message), text "CV template: <Country name>".
- while running: ``role="status"`` ("Drafting ..."); the unit polls itself with ``hx-get`` to its
  GET route, ``hx-trigger`` containing ``every``. A failed latest task: ``role="alert"`` with
  ``message_for_kind(task.error)``. A fake provider's row: the practice notice
  (``PRACTICE_NOTICE`` of ``app.services.job_fit``) in the unit.
- the draft form ``form#template-draft-form`` (action ``/settings/cv-templates/<CC>/draft``):
  selects ``pages`` ("Length": 1, 2, 3 pages) and ``photo`` ("Photo": none, optional,
  expected), textarea ``note`` ("Note (optional)") and the submit ``button#template-draft-start``,
  text "Draft a template for <Country name>" (states ``none`` and ``failed``). With a row (or a
  task running) the
  form sits in ``details > summary`` "Redraft this template" and the button reads "Draft again".
- with a row: the review form ``form#template-form`` (``method="post"``, action
  ``/settings/cv-templates/<CC>``): hidden ``version`` (the row's); twelve section slots in order,
  ``select[name=section_<i>]`` (label "Section <i>", the empty option first, then the 11 ids of
  ``SectionId``) and ``input[name=heading_<i>]`` (label "Heading for section <i>", the optional
  heading); ``select[name=max_pages]`` ("Maximum pages", 1 to 3); ``input[type=checkbox]
  [name=photo_slot][value=1]`` ("Photo slot"); checkboxes ``detail_email``, ``detail_phone``,
  ``detail_location``, ``detail_links`` (value 1; "Show email"...; the name is always shown);
  ``select[name=date_format]`` ("Date format": ``month_year|numeric|iso|year``) and
  ``select[name=page_size]`` ("Page size": ``A4|Letter``). Prefilled from the SAVED row. Each
  field's note is a visible ``p#template-note-<field>`` (field in ``NOTE_FIELDS``) named by the
  control's ``aria-describedby``; notes are plain escaped text. Two submit buttons named
  ``action``: "Save changes" (``save``) and "Approve template" (``approve``).
  Invalid controls: ``aria-invalid="true"`` + ``aria-describedby`` naming a visible error element
  (ids unique), exactly one ``data-autofocus``, on the first invalid control. Error tie: a
  section id outside the set, a repeated section or a heading with no section is tied to
  ``section_<i>`` (the slot at fault, the later one for a repeat); a heading over 40 characters or
  with control or bidi characters to ``heading_<i>``; no section at all to ``section_1``;
  ``max_pages`` (not 1 to 3), ``date_format``, ``page_size`` to their own names.
- the unit also shows the saved status in words ("Draft: not approved yet." or "Approved.") and
  the approved time.

Workspace panel: step 3 (CV) of ``#tailoring`` (page ``?tab=tailoring&step=3`` and fragment
``/applications/<id>/tailoring/3``) holds one ``#cv-template`` unit for the application's CV
country (the workspace's, else the preference). It keeps the "later step" text about the CV itself.
The other steps hold no unit. Reading a step writes nothing and starts nothing.

Settings > CV templates: the token-free ``GET /settings/your-details`` page (M9.2) and ``GET
/settings/cv-templates`` hold ``<section id="templates-settings" class="settings-section"
aria-labelledby="templates-heading">`` with ``<h2 id="templates-heading" tabindex="-1">CV
templates</h2>``; ``ul#template-list`` with ``li#template-<CC>[data-country][data-status]`` per row
in country-code order (text: the country name, the code, "Approved" or "Draft"), each with a
``button`` "Review <Country name> template" (``hx-get`` ``/settings/cv-templates/<CC>``, target
``#template-editor``); an empty list shows ``p#template-empty`` ("No templates yet...") and no
``ul``; ``div#template-editor`` receives the unit.
"""

import json
import struct
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from httpx import Response

from app.domain.cv_templates import TemplateSpec
from tests.board_pages import elements
from tests.integration.test_app_shell import Element
from tests.refine_harness import refining
from tests.settings_board_pages import accessible_name, controls_in, errors_in, invalid_names
from tests.tailoring_pages import Tailoring, announced, by_id, has_id, inside, said_text

DRAFT_TEMPLATE = "draft_cv_template"
BASE = "/settings/cv-templates"
COUNTRIES = {"PT": "Portugal", "DE": "Germany", "FR": "France"}
SLOTS = 12
SECTION_IDS = (
    "summary",
    "experience",
    "education",
    "skills",
    "projects",
    "certifications",
    "languages",
    "volunteering",
    "publications",
    "awards",
    "interests",
)
DETAIL_IDS = ("email", "phone", "location", "links")
NOTE_FIELDS = ("sections", "max_pages", "photo_slot", "details_shown", "date_format", "page_size")
INVALID = "This request isn't valid."
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai" / DRAFT_TEMPLATE

STARTED = "Template draft started."
READY = "Template draft ready."
SAVED = "Template saved. Approve it to use it."
APPROVED = "Template approved."
ONE_FIELD = "1 field needs a change."

__all__ = [
    "APPROVED",
    "BASE",
    "COUNTRIES",
    "DETAIL_IDS",
    "DRAFT_TEMPLATE",
    "INVALID",
    "NOTE_FIELDS",
    "ONE_FIELD",
    "READY",
    "SAVED",
    "SECTION_IDS",
    "SLOTS",
    "STARTED",
    "Templates",
    "accessible_name",
    "announced",
    "by_id",
    "controls_in",
    "errors_in",
    "fixture_data",
    "form_fields",
    "has_id",
    "invalid_names",
    "real_png",
    "review_values",
    "said_text",
    "templates_app",
    "unit",
    "unit_state",
]


# --- fixtures and form encoding -------------------------------------------------------------------


def fixture_data(name: str = "ok") -> dict[str, Any]:
    """The ``data`` of a ``draft_cv_template`` AI fixture: ``{"template": ..., "notes": ...}``."""
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    data: dict[str, Any] = loaded["data"]
    return data


def form_fields(template: Mapping[str, Any]) -> dict[str, str]:
    """What a browser posts for the review form showing ``template`` (a closed spec as JSON):
    the twelve slots (blank ones empty), the selects, and only the CHECKED checkboxes."""
    sections = list(template["sections"])
    assert len(sections) <= SLOTS
    fields: dict[str, str] = {}
    for slot in range(1, SLOTS + 1):
        section = sections[slot - 1] if slot <= len(sections) else {}
        fields[f"section_{slot}"] = str(section.get("id", ""))
        fields[f"heading_{slot}"] = str(section.get("heading") or "")
    fields["max_pages"] = str(template["max_pages"])
    if template["photo_slot"]:
        fields["photo_slot"] = "1"
    for detail in template["details_shown"]:
        fields[f"detail_{detail}"] = "1"
    fields["date_format"] = str(template["date_format"])
    fields["page_size"] = str(template["page_size"])
    return fields


def real_png(width: int = 100, height: int = 100) -> bytes:
    """A small PNG a browser can decode (stdlib only): a flat colour, 8-bit RGB, no metadata."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes((0x3A, 0x6E, 0xA5)) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


# --- reading answers ------------------------------------------------------------------------------


def unit(page: str) -> Element:
    found = by_id(page, "cv-template")
    assert found.attrs.get("data-country"), "the unit names its country"
    return found


def unit_state(page: str) -> str:
    return str(unit(page).attrs["data-state"])


def review_values(page: str) -> dict[str, str]:
    """What ``#template-form`` would post as it is shown (hidden ``version`` included; checkboxes
    only when checked; a select without a selected option shows its first)."""
    form = by_id(page, "template-form")
    values: dict[str, str] = {}
    for control in inside(page, form, lambda e: e.tag in ("input", "select", "textarea")):
        name = control.attrs.get("name")
        if not name:
            continue
        if control.tag == "select":
            options = [e for e in elements(page) if e.tag == "option" and e.within(control)]
            chosen = [o for o in options if "selected" in o.attrs] or options[:1]
            values[str(name)] = str(chosen[0].attrs.get("value", "")) if chosen else ""
        elif control.attrs.get("type") == "checkbox":
            if "checked" in control.attrs:
                values[str(name)] = str(control.attrs.get("value", "on"))
        elif control.tag == "textarea":
            values[str(name)] = control.text
        else:
            values[str(name)] = str(control.attrs.get("value") or "")
    return values


# --- the running app ------------------------------------------------------------------------------


class Templates(Tailoring):
    """The workspace harness plus the template routes. ``tw.ready()`` and the rest are inherited."""

    # --- requests -------------------------------------------------------------------------------

    def show(self, code: str, **headers: str) -> Response:
        return self.r.client.get(f"{BASE}/{code}", headers=self.r.run.run.htmx(**headers))

    def settings_section(self) -> Response:
        return self.r.client.get(BASE, headers=self.r.run.run.htmx())

    def post_raw(self, path: str, data: Any) -> Response:
        """A POST with the token, the origin and ``HX-Request``; ``data`` may be a list of pairs
        (a repeated field)."""
        headers = self.r.run.run.action(**{"HX-Request": "true"})
        if isinstance(data, list):  # httpx can't encode pairs as data=: encode the form ourselves
            headers["content-type"] = "application/x-www-form-urlencoded"
            return self.r.client.post(path, content=urlencode(data), headers=headers)
        return self.r.client.post(path, data=data, headers=headers)

    def start(self, code: str, **fields: str) -> Response:
        return self.post_raw(f"{BASE}/{code}/draft", fields)

    def review(
        self,
        code: str,
        *,
        action: str = "save",
        version: int | str | None = None,
        uncheck: tuple[str, ...] = (),
        **changes: str,
    ) -> Response:
        """Post the form as it is shown now, with ``changes`` laid over it; a browser leaves an
        unchecked checkbox out, which ``uncheck`` names."""
        shown = review_values(self.show(code).text)
        fields = {k: v for k, v in shown.items() if k != "version" and k not in uncheck}
        body = {
            **fields,
            **changes,
            "action": action,
            "version": str(self.template_version(code) if version is None else version),
        }
        return self.post_raw(f"{BASE}/{code}", body)

    def approve(self, code: str, **changes: str) -> Response:
        return self.review(code, action="approve", **changes)

    # --- the provider and tasks -----------------------------------------------------------------

    def drafted(
        self,
        code: str = "PT",
        fixture: str | None = "ok",
        *,
        status: str = "succeeded",
        **form: str,
    ) -> Any:
        """Script the answer (``None``: the app's own fake), start by the route, wait for the task
        to end as ``status``."""
        if fixture is not None:
            self.script(DRAFT_TEMPLATE, fixture)
        started = self.start(code, **form)
        assert started.status_code == 200, (started.status_code, started.text[:300])
        return self.ended(DRAFT_TEMPLATE, code, status)

    def approved_template(self, code: str = "PT", fixture: str | None = "ok") -> None:
        """A drafted and approved template, by the routes."""
        self.drafted(code, fixture)
        answered = self.approve(code)
        assert answered.status_code == 200, (answered.status_code, answered.text[:300])
        assert self.template_row(code)["status"] == "approved"

    def template_tasks(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM tasks WHERE type=? ORDER BY rowid", (DRAFT_TEMPLATE,))

    # --- what is stored -------------------------------------------------------------------------

    def template_rows(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM cv_templates ORDER BY country")

    def template_row(self, code: str) -> dict[str, Any]:
        found = self.rows("SELECT * FROM cv_templates WHERE country=?", (code,))
        assert len(found) == 1, f"expected one {code} template, found {len(found)}"
        return found[0]

    def template_version(self, code: str) -> int:
        return int(self.template_row(code)["version"])

    def stored_data(self, code: str) -> dict[str, Any]:
        """The ``spec`` column as JSON: exactly ``{"template": ..., "notes": ...}``."""
        data: dict[str, Any] = json.loads(self.template_row(code)["spec"])
        assert set(data) == {"template", "notes"}, sorted(data)
        return data

    def stored_template_json(self, code: str) -> dict[str, Any]:
        template: dict[str, Any] = self.stored_data(code)["template"]
        return template

    def stored_notes(self, code: str) -> dict[str, str]:
        notes: dict[str, str] = self.stored_data(code)["notes"]
        return notes

    def stored(self, code: str) -> tuple[TemplateSpec, dict[str, str]]:
        """The closed spec (validated by the domain model) and the notes of a stored row."""
        spec = TemplateSpec.model_validate(self.stored_template_json(code))
        return spec, self.stored_notes(code)

    def approved_spec(self, code: str) -> TemplateSpec | None:
        found: TemplateSpec | None = self.r.run.app.state.cv_templates.approved(code)
        return found


@contextmanager
def templates_app(tmp_path: Any) -> Iterator[Templates]:
    """Each test file wraps this in its own ``tw`` fixture."""
    with refining(tmp_path) as r:
        yield Templates(r)
