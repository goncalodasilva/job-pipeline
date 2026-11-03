"""Country CV templates (M6.6; ADR-106 items 3, 6 and 12; spec 3.6): draft once per country,
review, approve, reuse.

A draft is one AI request per country (``template_ai.run_draft_cv_template``) holding the
country, the style choices and an optional note, and nothing about the person. The start
stores those choices in the task (``input_ref``), so the handler needs nothing else, and the
dedupe key ``draft_cv_template:<CC>`` allows one draft at a time per country. The result is
written in the transaction that marks the task succeeded, and nowhere else: a failed or
cancelled task leaves an existing row (approved or not) as it was. A redraft puts the same
row back to a draft.

The ``spec`` column holds ``{"template": <closed spec>, "notes": {...}}``; the review form
edits every field of the closed spec, and only a form that passes the closed model is stored.
Approval is the person's: it stores ``approved`` and the time. An edit of an approved
template puts it back to a draft. Reads never write or start work.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError

from app.domain.clock import Clock
from app.domain.cv_templates import (
    DateFormat,
    DetailId,
    PageSize,
    SectionId,
    TemplateSpec,
    TemplateStatus,
)
from app.domain.errors import ProviderNotConfigured, VersionConflict
from app.domain.ids import IdFactory
from app.domain.locales import COUNTRIES
from app.domain.settings import AIProviderSettings, Tier, has_control_characters
from app.domain.tasks import TaskRef
from app.domain.template_draft import PhotoConvention, TemplateStyle
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory
from app.modules.storage import tailoring_repo
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.tailoring import TaskState
from app.services.tasks import TaskService
from app.services.template_ai import (
    DRAFT_CV_TEMPLATE,
    MAX_NOTE_CHARS,
    run_draft_cv_template,
    template_dedupe_key,
)
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

DRAFT_LABEL: Final = "Draft CV template"
SLOTS: Final = 12
MAX_HEADING: Final = 40
COUNTRY_NAMES: Final[Mapping[str, str]] = dict(COUNTRIES)
SECTION_IDS: Final = tuple(item.value for item in SectionId)
DETAIL_IDS: Final = tuple(item.value for item in DetailId)
DATE_FORMATS: Final = tuple(item.value for item in DateFormat)
PAGE_SIZES: Final = tuple(item.value for item in PageSize)
PAGE_CHOICES: Final = ("1", "2", "3")
PHOTO_CHOICES: Final = tuple(item.value for item in PhotoConvention)
DEFAULT_PAGES: Final = "2"
DEFAULT_PHOTO: Final = "optional"
# Every field of the review form except ``version`` and ``action``.
REVIEW_FIELDS: Final = frozenset(
    {
        *(f"section_{n}" for n in range(1, SLOTS + 1)),
        *(f"heading_{n}" for n in range(1, SLOTS + 1)),
        "max_pages",
        "photo_slot",
        "date_format",
        "page_size",
        *(f"detail_{name}" for name in DETAIL_IDS),
    }
)
CHECKBOXES: Final = ("photo_slot", *(f"detail_{name}" for name in DETAIL_IDS))
STYLE_MESSAGES: Final = {
    "pages": "Choose 1, 2 or 3 pages.",
    "photo": "Choose none, optional or expected.",
    "note": f"Keep the note to {MAX_NOTE_CHARS} characters or fewer.",
}
REVIEW_MESSAGES: Final = {
    "unknown_section": "Choose a section from the list.",
    "repeated": "Each section can appear once. Choose another section or leave this one empty.",
    "no_section": "Choose a section for this heading, or clear the heading.",
    "no_sections": "Choose at least one section.",
    "heading": f"Use a heading of up to {MAX_HEADING} characters on one line, or leave it empty.",
    "max_pages": "Choose 1, 2 or 3 pages.",
    "date_format": "Choose a date format from the list.",
    "page_size": "Choose A4 or Letter.",
}
_ACTIVE: Final = ("queued", "running")


class TemplateNotFound(LookupError):
    """No such country in the list, no template for it yet, or no open workspace."""


class TemplateBusy(RuntimeError):
    """A draft is queued or running for the country, so the template can't be approved: the draft
    would replace what was approved."""

    message = "A new draft is being made. Approve the template once it is ready."


class StyleInvalid(ValueError):
    """The draft form's style or note is refused: ``fields`` maps each field name to its fixed
    message (never the entered value)."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        super().__init__("invalid template style")
        self.fields = dict(fields)


class ReviewInvalid(ValueError):
    """The review form can't be stored: ``fields`` maps each refused control, in document
    order, to its fixed message (never the entered value)."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        super().__init__("invalid template")
        self.fields = dict(fields)


@dataclass(frozen=True)
class DraftRequest:
    """What a start stores in the task: the style, the note and the tier asked for."""

    style: TemplateStyle
    note: str
    tier: Tier | None


@dataclass(frozen=True)
class TemplateView:
    """One country's template unit: the stored row (a spec the closed model accepts), the notes
    beside the fields and the latest draft task."""

    code: str
    country: str
    version: int  # 0 without a row
    status: str | None
    approved_at: str | None
    practice: bool
    spec: TemplateSpec | None
    notes: Mapping[str, str]
    task: TaskState


@dataclass(frozen=True)
class TemplateListing:
    code: str
    country: str
    status: str
    draft: str = ""  # "running" while a draft is queued or running, "failed" if the last failed


def clean_note(value: str) -> str:
    """A note as it is kept: no NUL, line ends as LF, trimmed."""
    return value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


def style_from_form(pages: str, photo: str, note: str) -> tuple[TemplateStyle, str]:
    """The style and the cleaned note, or ``StyleInvalid`` with one message per refused field."""
    errors: dict[str, str] = {}
    chosen = None
    if pages in PAGE_CHOICES and photo in PHOTO_CHOICES:
        chosen = TemplateStyle(int(pages), PhotoConvention(photo))
    if pages not in PAGE_CHOICES:
        errors["pages"] = STYLE_MESSAGES["pages"]
    if photo not in PHOTO_CHOICES:
        errors["photo"] = STYLE_MESSAGES["photo"]
    text = clean_note(note)
    if len(text) > MAX_NOTE_CHARS:
        errors["note"] = STYLE_MESSAGES["note"]
    if errors or chosen is None:
        raise StyleInvalid(errors)
    return chosen, text


def shown_from_spec(spec: TemplateSpec) -> dict[str, str]:
    """The review form's values for ``spec``, named as the form posts them (a checkbox only
    when ticked)."""
    shown: dict[str, str] = {}
    for slot in range(1, SLOTS + 1):
        section = spec.sections[slot - 1] if slot <= len(spec.sections) else None
        shown[f"section_{slot}"] = section.id.value if section else ""
        shown[f"heading_{slot}"] = (section.heading or "") if section else ""
    shown["max_pages"] = str(spec.max_pages)
    if spec.photo_slot:
        shown["photo_slot"] = "1"
    for detail in spec.details_shown:
        shown[f"detail_{detail.value}"] = "1"
    shown["date_format"] = spec.date_format.value
    shown["page_size"] = spec.page_size.value
    return shown


def shown_with_typed(spec: TemplateSpec, typed: Mapping[str, str]) -> dict[str, str]:
    """What a refused form shows: what was typed in each control that can hold it (text, a
    choice that is in the list, a tick), the saved value in the rest."""
    saved = shown_from_spec(spec)
    shown = dict(saved)
    for slot in range(1, SLOTS + 1):
        heading = typed.get(f"heading_{slot}", "").replace("\x00", "")
        shown[f"heading_{slot}"] = heading
        section = typed.get(f"section_{slot}", "")
        if section == "" or section in SECTION_IDS:
            shown[f"section_{slot}"] = section
    for name, allowed in (
        ("max_pages", PAGE_CHOICES),
        ("date_format", DATE_FORMATS),
        ("page_size", PAGE_SIZES),
    ):
        if typed.get(name) in allowed:
            shown[name] = str(typed[name])
    for name in CHECKBOXES:
        shown.pop(name, None)
        if typed.get(name) == "1":
            shown[name] = "1"
    return shown


def spec_from_form(values: Mapping[str, str]) -> TemplateSpec:
    """The closed spec a review form describes, or ``ReviewInvalid`` naming every refused
    control in document order. Messages are fixed text."""
    errors: dict[str, str] = {}
    sections: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for slot in range(1, SLOTS + 1):
        section = values.get(f"section_{slot}", "")
        heading = values.get(f"heading_{slot}", "").strip()
        if not section:
            if heading:
                errors[f"section_{slot}"] = REVIEW_MESSAGES["no_section"]
        elif section not in SECTION_IDS:
            errors[f"section_{slot}"] = REVIEW_MESSAGES["unknown_section"]
        elif section in seen:
            errors[f"section_{slot}"] = REVIEW_MESSAGES["repeated"]
        elif heading and (len(heading) > MAX_HEADING or has_control_characters(heading)):
            seen.add(section)
            errors[f"heading_{slot}"] = REVIEW_MESSAGES["heading"]
        else:
            seen.add(section)
            sections.append({"id": section, "heading": heading or None})
    if not sections and not errors:
        errors["section_1"] = REVIEW_MESSAGES["no_sections"]
    if values.get("max_pages") not in PAGE_CHOICES:
        errors["max_pages"] = REVIEW_MESSAGES["max_pages"]
    if values.get("date_format") not in DATE_FORMATS:
        errors["date_format"] = REVIEW_MESSAGES["date_format"]
    if values.get("page_size") not in PAGE_SIZES:
        errors["page_size"] = REVIEW_MESSAGES["page_size"]
    if errors:
        raise ReviewInvalid(errors)
    try:
        return TemplateSpec.model_validate(
            {
                "sections": sections,
                "max_pages": int(values["max_pages"]),
                "photo_slot": values.get("photo_slot") == "1",
                "details_shown": [
                    name for name in DETAIL_IDS if values.get(f"detail_{name}") == "1"
                ],
                "date_format": values["date_format"],
                "page_size": values["page_size"],
            }
        )
    except ValidationError:  # the checks above cover each field; this is the closed model's say
        raise ReviewInvalid({"section_1": REVIEW_MESSAGES["unknown_section"]}) from None


def _template_data(spec: TemplateSpec, stored: object) -> dict[str, Any]:
    """The JSON to store for ``spec``: the stored object itself when the form changed nothing, so
    approving a reviewed draft doesn't rewrite it; else the spec without empty headings."""
    try:
        if isinstance(stored, dict) and TemplateSpec.model_validate(stored) == spec:
            return stored
    except ValidationError:
        pass
    return spec.model_dump(mode="json", exclude_none=True)


def _task_state(row: tuple[str, str, str | None, int] | None) -> TaskState:
    if row is None:
        return TaskState()
    return TaskState(row[1] in _ACTIVE, row[1], row[2], f"{row[0]}:{row[3]}")


def _request_text(code: str, style: TemplateStyle, note: str, tier: Tier | None) -> str:
    return json.dumps(
        {
            "code": code,
            "pages": style.pages,
            "photo": style.photo.value,
            "note": note,
            "tier": tier,
        },
        ensure_ascii=False,
    )


def _request_from(text: str | None) -> tuple[str, DraftRequest]:
    """The country and the request a start stored; ``ValueError`` for anything else."""
    try:
        loaded: Any = json.loads(text or "")
        code = str(loaded["code"])
        tier = loaded["tier"]
        if code not in COUNTRY_NAMES or tier not in (None, "fast", "thorough"):
            raise ValueError
        style = TemplateStyle(loaded["pages"], PhotoConvention(loaded["photo"]))
        note = loaded["note"]
        if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("The stored template request is not valid.") from None
    return code, DraftRequest(style, note, tier)


class CvTemplateService:
    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        clock: Clock,
        ids: IdFactory,
        workspace: Callable[[], SummaryWorkspace | None],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise TemplateNotFound()
        return opened

    # --- starting ---------------------------------------------------------------------------

    def plan_start(self, *, tier: Tier | None = None) -> SummaryPlan:
        """What a start would use. ``ProviderNotConfigured`` without a usable provider."""
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or task_tier(DRAFT_CV_TEMPLATE, settings))

    def start_draft(
        self, country_code: str, style: TemplateStyle, note: str | None, *, tier: Tier | None = None
    ) -> TaskRef | None:
        """Queue the draft for a country (None: not a country in the list). A draft already
        queued or running for it is returned instead."""
        if country_code not in COUNTRY_NAMES:
            return None
        text = clean_note(note or "")
        if len(text) > MAX_NOTE_CHARS:
            raise ValueError("The note is too long.")
        self.plan_start(tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise TemplateNotFound()
        return tasks.enqueue(
            DRAFT_CV_TEMPLATE,
            country_code,
            input_ref=_request_text(country_code, style, text, tier),
        )

    # --- running (inside a task) ------------------------------------------------------------

    async def run_draft(self, ctx: SummaryContext) -> ResultWriter:
        code, request = _request_from(ctx.input_ref)
        await ctx.heartbeat(0)
        opened, usage = self._open(), self._usage()
        settings = await asyncio.to_thread(self._settings)
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or usage is None or service is None:
            raise ProviderNotConfigured()
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        await ctx.heartbeat(10)
        output = await run_draft_cv_template(
            runner,
            code,
            COUNTRY_NAMES[code],
            request.style,
            request.note,
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=request.tier,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        # Stored as the model gave it: a heading it left out stays left out.
        data = {
            "template": output.result.spec.model_dump(mode="json", exclude_unset=True),
            "notes": dict(output.result.notes),
        }
        new_id, name = self._ids.new(), provider.name

        def write(conn: sqlite3.Connection) -> str:
            tailoring_repo.put_drafted_template(
                conn,
                id=new_id,
                country=code,
                data=data,
                provider=name,
                model=output.model,
                prompt_id=output.prompt_id,
                prompt_version=output.prompt_version,
                task_id=ctx.task_id,
                now=self._clock.now().isoformat(),
            )
            return f"cv_template:{code}"

        return write

    # --- reading ----------------------------------------------------------------------------

    def approved(self, country_code: str) -> TemplateSpec | None:
        """The country's approved template (a draft is not usable), else None."""
        if country_code not in COUNTRY_NAMES:
            return None
        workspace = self._workspace()
        if workspace is None:
            return None
        with workspace.db.read() as conn:
            found = tailoring_repo.get_template(conn, country_code)
        if found is None or found.status != TemplateStatus.APPROVED.value:
            return None
        try:
            return tailoring_repo.template_spec(found.spec_json)
        except ValueError:
            return None

    def listing(self) -> tuple[TemplateListing, ...]:
        """The stored templates in country-code order. Empty without an open workspace."""
        workspace = self._workspace()
        if workspace is None:
            return ()
        with workspace.db.read() as conn:
            rows = tailoring_repo.list_templates(conn)
            tasks = [
                _task_state(tailoring_repo.latest_task(conn, template_dedupe_key(row.country)))
                for row in rows
            ]
        return tuple(
            TemplateListing(
                row.country,
                COUNTRY_NAMES.get(row.country, row.country),
                row.status,
                "running" if task.active else "failed" if task.attention else "",
            )
            for row, task in zip(rows, tasks, strict=True)
        )

    def view(self, country_code: str) -> TemplateView:
        """The unit's data for a country in the list (``TemplateNotFound`` otherwise)."""
        if country_code not in COUNTRY_NAMES:
            raise TemplateNotFound()
        with self._open().db.read() as conn:
            stored = tailoring_repo.get_template(conn, country_code)
            task = _task_state(tailoring_repo.latest_task(conn, template_dedupe_key(country_code)))
        spec: TemplateSpec | None = None
        if stored is not None:
            try:
                spec = TemplateSpec.model_validate(stored.data.get("template"))
            except ValidationError:
                spec = None
        notes = stored.data.get("notes") if stored is not None else None
        return TemplateView(
            code=country_code,
            country=COUNTRY_NAMES[country_code],
            version=stored.version if stored is not None and spec is not None else 0,
            status=stored.status if stored is not None and spec is not None else None,
            approved_at=stored.approved_at if stored is not None and spec is not None else None,
            practice=bool(stored and spec is not None and stored.provider == "fake"),
            spec=spec,
            notes={str(k): str(v) for k, v in notes.items()} if isinstance(notes, dict) else {},
            task=task,
        )

    # --- reviewing --------------------------------------------------------------------------

    def save(
        self, country_code: str, values: Mapping[str, str], *, version: int, approve: bool
    ) -> None:
        """Store the review form: as the draft it becomes (an edit of an approved template needs
        approving again), or approved with ``approve``. ``TemplateNotFound``, ``ReviewInvalid``
        (nothing stored), ``VersionConflict`` for a stale ``version`` and ``TemplateBusy`` for
        an approval while a draft for the country is queued or running."""
        if country_code not in COUNTRY_NAMES:
            raise TemplateNotFound()
        spec = spec_from_form(values)
        now = self._clock.now().isoformat()
        with self._open().db.transaction() as conn:
            stored = tailoring_repo.get_template(conn, country_code)
            if stored is None:
                raise TemplateNotFound()
            if stored.version != version:
                raise VersionConflict()
            if approve:
                latest = tailoring_repo.latest_task(conn, template_dedupe_key(country_code))
                if _task_state(latest).active:
                    raise TemplateBusy()
            tailoring_repo.update_template(
                conn,
                country_code,
                data={
                    "template": _template_data(spec, stored.data.get("template")),
                    "notes": stored.data.get("notes", {}),
                },
                approved_at=now if approve else None,
                now=now,
                expected_version=version,
            )


__all__ = [
    "CvTemplateService",
    "ReviewInvalid",
    "StyleInvalid",
    "TemplateBusy",
    "TemplateNotFound",
    "TemplateView",
]
