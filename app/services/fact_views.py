"""What the Fact bank page shows (M3.5): plain data built from the fact services, so routes
and templates hold no rules. The Library's filters, options, pages and counts; the drafts
grouped by the import that made them, each with the CV line it came from; the kept uploads
with the latest task of each; the tab labels with the number of drafts.

Everything here reads only (spec 2.9 rule 12). Fact wording, quotes, employers and skills are
the user's own or AI-made text: they are handed on as plain strings for the templates to
escape, and nothing of them is logged. Only an import id of the safe shape (``is_task_id``)
becomes an element id or part of a URL.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from typing import Final
from urllib.parse import urlencode

from app.domain.errors import FactInputInvalid, format_day_time
from app.domain.facts import FactStatus, FactType, Privacy
from app.domain.tasks import is_task_id
from app.modules.storage import Database, facts_repo
from app.services.cv_import import STATE_DONE, ImportSummary
from app.services.fact_library import FactLibrary, LibraryPage, LibraryQuery, checked_status
from app.services.facts import FactService, FactView, fact_view
from app.services.roles import RoleService, RoleView
from app.services.skills import SkillService
from app.services.task_views import TaskView

LIBRARY: Final = "library"
DRAFTS: Final = "drafts"
CAPTURE: Final = "capture"
_TAB_LABELS: Final = (
    (LIBRARY, "Library"),
    (DRAFTS, "Drafts to review"),
    (CAPTURE, "Capture inbox"),
)

# ADR-089 item 8: what the fake provider made says so, by provider name (ADR-092 item 10).
PRACTICE_PROVIDER: Final = "fake"

MSG_NO_FACTS: Final = "No facts yet. Import your CV?"
MSG_IMPORT_FAILED: Final = "CV import failed. The reason is under Uploaded CVs."
# A file and a pasted text sent together: the page's own words for that refusal.
MSG_CHOOSE_ONE: Final = "Choose one: upload a file or paste the text, not both."
MSG_NO_MATCH: Final = "No facts match your search."
MSG_CONFIRMED: Final = "Fact confirmed."
MSG_EDITED: Final = "Fact saved and confirmed."
MSG_DISCARDED: Final = "Draft discarded."
MSG_RESTORED: Final = "Draft restored."
PRACTICE_BADGE: Final = "Practice data (fake provider)"

_MAX_QUERY_CHARS: Final = 200
_MAX_ID_CHARS: Final = 64
_MAX_PAGE_DIGITS: Final = 6
_STATUS_LABELS: Final[Mapping[FactStatus, str]] = {
    FactStatus.CONFIRMED: "Confirmed",
    FactStatus.DRAFT: "Draft",
    FactStatus.ARCHIVED: "Archived",
    FactStatus.DISCARDED: "Discarded",
}
_PRIVACY_BADGES: Final[Mapping[Privacy, str]] = {
    Privacy.NEVER_USE: "Never use in applications",
    Privacy.GENERALIZE: "Generalize names",
}
_PART_LABELS: Final = (
    ("text", "Wording"),
    ("situation", "Situation"),
    ("action", "Action"),
    ("result", "Result"),
)
_STORY_PARTS: Final = ("situation", "action", "result")
_KIND_LABELS: Final[Mapping[str, str]] = {
    "text": "Pasted text",
    "pdf": "PDF file",
    "docx": "Word file",
}
_OTHER_GROUP: Final = "Other drafts"
_NOTE_DONE: Final = "Imported. Its drafts are listed below until you confirm or discard them."
_NOTE_WAITING: Final = "Not imported yet."
_NOTE_CANCEL: Final = "To cancel, open Tasks in the top bar."
_START: Final = "Start"
_TRY_AGAIN: Final = "Try again"


@dataclass(frozen=True)
class FactBank:
    """The open workspace's fact services and the database they share."""

    db: Database
    facts: FactService
    library: FactLibrary
    roles: RoleService
    skills: SkillService


@dataclass(frozen=True)
class TabView:
    id: str
    label: str
    current: bool
    count: int = 0
    """How many drafts wait (the Drafts tab only)."""


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class EditField:
    """One part of a draft's wording in its inline form."""

    name: str
    label: str
    value: str
    hint: str = ""
    """What to write there, in a line (the full editor's story parts)."""


@dataclass(frozen=True)
class FactItem:
    """One fact as the Library or the Drafts tab lists it."""

    id: str
    version: int
    status: str
    status_label: str
    text: str | None
    parts: tuple[tuple[str, str], ...]
    """A story's parts that have words, as (label, words)."""
    role: str | None
    skills: tuple[str, ...]
    privacy_badge: str | None
    practice: bool
    needs_check: bool
    quote: str | None
    """The CV line the fact was first made from (Drafts tab only)."""
    import_id: str | None
    fields: tuple[EditField, ...]
    merged: bool = False
    """A draft a merge closed: its way back is "Undo merge", never "Restore draft"."""


@dataclass(frozen=True)
class LibraryParams:
    """The Library's query as it arrived: nothing in it is trusted."""

    q: str = ""
    role: str = ""
    skill: str = ""
    status: str = ""
    page: int = 1


@dataclass(frozen=True)
class LibraryView:
    q: str
    role: str
    """The chosen role's id when it is a listed one, else blank (never shown back)."""
    skill: str
    status: str
    roles: tuple[Option, ...]
    skills: tuple[Option, ...]
    statuses: tuple[Option, ...]
    status_error: str | None
    items: tuple[FactItem, ...] = ()
    total: int = 0
    count_text: str = ""
    announce: str = ""
    empty: str | None = None
    """"none" (no facts at all yet), "drafts" (none confirmed, but drafts wait), "no_match"
    or None."""
    drafts_waiting: str = ""
    """With ``empty == "drafts"``: "1 draft" or "<n> drafts"."""
    query: str = ""
    """The page's own query string (``tab=library&...``), URL-encoded."""
    previous_query: str | None = None
    next_query: str | None = None


@dataclass(frozen=True)
class DraftGroup:
    id: str | None
    """The import's id; None for drafts no kept upload made."""
    heading: str
    """The upload's label and how many drafts it still has."""
    items: tuple[FactItem, ...]
    batch_hint: str | None
    """What "Confirm all" would do, in words; None when no draft is left that needs no
    check (the button isn't offered then)."""


@dataclass(frozen=True)
class DraftsView:
    groups: tuple[DraftGroup, ...]
    stamp: str
    """Changes when an import finishes or is removed: what the shown drafts were built from."""


@dataclass(frozen=True)
class UploadView:
    """A kept upload: never its file name."""

    id: str
    label: str
    note: str | None
    """What can be said about it: its state when it has no task, how to cancel while its
    task is queued or running."""
    task: TaskView | None
    start_label: str | None
    can_remove: bool


def library_params(query: Mapping[str, str]) -> LibraryParams:
    """The Library's fields of a request's query. A page that isn't one is the first."""
    page = query.get("page", "")
    usable = page.isascii() and page.isdigit() and len(page) <= _MAX_PAGE_DIGITS
    return LibraryParams(
        q=query.get("q", "")[:_MAX_QUERY_CHARS],
        role=query.get("role", "")[:_MAX_ID_CHARS],
        skill=query.get("skill", "")[:_MAX_ID_CHARS],
        status=query.get("status", "")[:_MAX_ID_CHARS],
        page=max(1, int(page)) if usable else 1,
    )


def found_message(total: int) -> str:
    return "1 fact found." if total == 1 else f"{total} facts found."


def batch_message(confirmed: int, skipped: int) -> str:
    """What "Confirm all from this import" did, and what it left (ADR-091 item 3)."""
    if confirmed == 0:
        first = "No facts confirmed."
    else:
        first = "1 fact confirmed." if confirmed == 1 else f"{confirmed} facts confirmed."
    if skipped == 0:
        return first
    if skipped == 1:
        return f"{first} 1 draft needs a check and was left for you to confirm on its own."
    return f"{first} {skipped} drafts need a check and were left for you to confirm one by one."


def drafts_phrase(count: int) -> str:
    return "1 draft" if count == 1 else f"{count} drafts"


def batch_hint(count: int) -> str | None:
    """What "Confirm all from this import" would confirm; None when that is nothing."""
    if count == 0:
        return None
    left = "Drafts marked 'Needs a check' are left for you."
    if count == 1:
        return f"Confirms the 1 draft in this import that needs no check. {left}"
    return f"Confirms the {count} drafts in this import that need no check. {left}"


def role_label(role: RoleView) -> str:
    return ", ".join(part for part in (role.title, role.employer) if part)


def edit_fields(view: FactView) -> tuple[EditField, ...]:
    """A fact is reworded as one text, a story by its parts; a part that has words is always
    there, so nothing the draft says is out of reach."""
    content = view.content
    values = {
        "text": content.text,
        "situation": content.situation,
        "action": content.action,
        "result": content.result,
    }
    shown = _STORY_PARTS if view.type is FactType.STORY else ("text",)
    return tuple(
        EditField(name, label, values[name] or "")
        for name, label in _PART_LABELS
        if name in shown or values[name]
    )


def fact_item(view: FactView, roles: Mapping[str, str], quote: str | None = None) -> FactItem:
    fields = edit_fields(view)
    return FactItem(
        id=view.id,
        version=view.version,
        status=view.status.value,
        status_label=_STATUS_LABELS[view.status],
        text=view.content.text,
        parts=tuple(
            (field.label, field.value) for field in fields if field.name != "text" and field.value
        ),
        role=roles.get(view.role_id or "") or None,
        skills=view.skills,
        privacy_badge=_PRIVACY_BADGES.get(view.privacy),
        practice=view.origin_provider == PRACTICE_PROVIDER,
        needs_check=view.needs_check,
        quote=quote,
        import_id=view.cv_import_id,
        fields=fields,
        merged=view.merged_into_fact_id is not None,
    )


def _upload_label(item: ImportSummary, tz: tzinfo, *, seconds: bool = False) -> str:
    kind = _KIND_LABELS.get(item.kind, "CV")
    moment = local_time(item.created_at, tz)
    time = format_day_time(moment) + (f":{moment.second:02d}" if seconds else "")
    return f"{kind} added {time}"


def _upload_labels(kept: Sequence[ImportSummary], tz: tzinfo) -> dict[str, str]:
    """Each kept upload's label; two that would read the same also give their seconds."""
    plain = {item.id: _upload_label(item, tz) for item in kept}
    shared = {label for label in plain.values() if list(plain.values()).count(label) > 1}
    return {item.id: _upload_label(item, tz, seconds=plain[item.id] in shared) for item in kept}


def _heading(label: str, count: int) -> str:
    return f"{label} \u00b7 {drafts_phrase(count)}"


def local_time(moment: datetime, tz: tzinfo) -> datetime:
    return moment.astimezone(tz) if moment.utcoffset() is not None else moment


def _stamp(imports: Sequence[ImportSummary]) -> str:
    done = sorted(item.id for item in imports if item.state == STATE_DONE)
    return hashlib.sha256("\n".join(done).encode()).hexdigest()[:16]


def _query(params: LibraryParams, page: int) -> str:
    fields = [
        ("tab", LIBRARY),
        ("q", params.q),
        ("role", params.role),
        ("skill", params.skill),
        ("status", params.status),
        ("page", str(page) if page > 1 else ""),
    ]
    return urlencode([(name, value) for name, value in fields if value])


def open_bank(bank: Callable[[], FactBank | None]) -> FactBank:
    """The open workspace's fact services."""
    found = bank()
    if found is None:  # setup sends every request to the guide until one is open
        raise RuntimeError("no workspace is open")
    return found


def role_labels(bank: FactBank) -> dict[str, str]:
    """Every role's label by id."""
    return {role.id: role_label(role) for role in bank.roles.list()}


class FactViews:
    """``bank`` answers the open workspace's fact services (None while none is open),
    ``imports`` the kept uploads and ``latest_task`` the last task of an import, if any."""

    def __init__(
        self,
        *,
        bank: Callable[[], FactBank | None],
        imports: Callable[[], Sequence[ImportSummary]],
        latest_task: Callable[[str], TaskView | None],
        provider_kind: Callable[[], str | None],
        import_failed_said: str,
        tz: tzinfo,
    ) -> None:
        self._bank = bank
        self._imports = imports
        self._latest_task = latest_task
        self._provider_kind = provider_kind
        self.import_failed_said: Final = import_failed_said
        """What the task indicator says when an import fails; this page says
        ``MSG_IMPORT_FAILED`` instead (the reason is in the upload's own row)."""
        self._tz = tz

    def _open(self) -> FactBank:
        return open_bank(self._bank)

    def _kept(self) -> list[ImportSummary]:
        return [item for item in self._imports() if is_task_id(item.id)]

    def practice_provider(self) -> bool:
        """Whether the saved provider is the practice one, which reads no CV: an import
        then makes fictional example drafts (ADR-089 item 8)."""
        return self._provider_kind() == PRACTICE_PROVIDER

    # --- tabs -----------------------------------------------------------------------------------

    def draft_count(self) -> int:
        bank = self._open()
        with bank.db.read() as conn:
            return facts_repo.count_by_status(conn).get(FactStatus.DRAFT.value, 0)

    def tabs(self, current: str) -> tuple[TabView, ...]:
        """The three tabs; "Drafts to review" carries how many drafts wait."""
        count = self.draft_count()
        return tuple(
            TabView(tab, f"{label} ({count})", tab == current, count)
            if tab == DRAFTS
            else TabView(tab, label, tab == current)
            for tab, label in _TAB_LABELS
        )

    def drafts_tab(self) -> TabView:
        """The Drafts tab's link as it reads now, for an answer that changed the count."""
        return next(tab for tab in self.tabs(DRAFTS) if tab.id == DRAFTS)

    # --- the Library ----------------------------------------------------------------------------

    def library(self, params: LibraryParams) -> LibraryView:
        """One page of the Library for ``params``. A status that isn't one comes back as
        ``status_error`` with no results; a page past the last one is the first."""
        bank = self._open()
        roles = bank.roles.list()
        labels = {role.id: role_label(role) for role in roles}
        skills = bank.skills.list()
        form = LibraryView(
            q=params.q,
            role=params.role if params.role in labels else "",
            skill=params.skill if any(skill.id == params.skill for skill in skills) else "",
            status="",
            roles=tuple(Option(role.id, labels[role.id]) for role in roles),
            skills=tuple(Option(skill.id, skill.name) for skill in skills),
            statuses=tuple(Option(status.value, label) for status, label in _STATUS_LABELS.items()),
            status_error=None,
        )
        try:
            status = checked_status(params.status) if params.status else None
        except FactInputInvalid as error:
            return replace(form, status_error=error.user_message)
        found = self._search(bank, params, status)
        with bank.db.read() as conn:
            counts = facts_repo.count_by_status(conn)
        return self._results(replace(form, status=params.status), params, found, labels, counts)

    def _search(
        self, bank: FactBank, params: LibraryParams, status: FactStatus | None
    ) -> LibraryPage:
        query = LibraryQuery(
            text=params.q,
            role_id=params.role or None,
            skill_id=params.skill or None,
            status=status,
            page=params.page,
        )
        found = bank.library.search(query)
        if not found.items and found.total and query.page > 1:
            found = bank.library.search(replace(query, page=1))
        return found

    def _results(
        self,
        form: LibraryView,
        params: LibraryParams,
        found: LibraryPage,
        roles: Mapping[str, str],
        counts: Mapping[str, int],
    ) -> LibraryView:
        pages = max(1, -(-found.total // found.page_size))
        # No status chosen and "confirmed" list the same facts: neither is a filter.
        status = params.status if params.status != FactStatus.CONFIRMED.value else ""
        filtered = bool(params.q.strip() or params.role or params.skill or status)
        usable = counts.get(FactStatus.CONFIRMED.value, 0) + counts.get(
            FactStatus.ARCHIVED.value, 0
        )
        drafts = counts.get(FactStatus.DRAFT.value, 0)
        empty = None
        waiting = ""
        announce = count_text = found_message(found.total)
        if pages > 1:
            announce = count_text = f"{count_text} Page {found.page} of {pages}."
        if found.total == 0 and (filtered or usable):
            empty, announce = "no_match", MSG_NO_MATCH
        elif found.total == 0 and drafts:
            empty, waiting = "drafts", drafts_phrase(drafts)
            announce = f"No confirmed facts yet. You have {waiting} to review."
        elif found.total == 0:
            empty, announce = "none", MSG_NO_FACTS
        return replace(
            form,
            items=tuple(fact_item(view, roles) for view in found.items),
            total=found.total,
            count_text=count_text,
            announce=announce,
            empty=empty,
            drafts_waiting=waiting,
            query=_query(params, found.page),
            previous_query=_query(params, found.page - 1) if found.page > 1 else None,
            next_query=_query(params, found.page + 1) if found.page < pages else None,
        )

    # --- drafts ---------------------------------------------------------------------------------

    def draft(self, fact_id: str) -> FactItem:
        """One fact as the Drafts tab shows it, whatever its status now. Raises
        ``FactNotFound``."""
        bank = self._open()
        roles = role_labels(bank)
        with bank.db.read() as conn:
            return fact_item(
                fact_view(conn, fact_id), roles, facts_repo.source_quote(conn, fact_id)
            )

    def drafts(self) -> DraftsView:
        """Every draft, grouped by the import that made it (newest import first, then the
        drafts no kept upload made), in the order they were made."""
        groups, stamp = self._groups()
        return DraftsView(tuple(group for group in groups if group.items), stamp)

    def _groups(self) -> tuple[list[DraftGroup], str]:
        """A group per kept upload, then the one for the drafts no kept upload made, each
        with the drafts it has now (maybe none), and the stamp they were built from. The
        kept uploads are read before the drafts: an import that finishes in between is then
        missing from the stamp, so the page asks again, rather than counted as shown while
        its drafts are not."""
        bank = self._open()
        kept = self._kept()
        roles = role_labels(bank)
        with bank.db.read() as conn:
            ids = facts_repo.ids_with_status(conn, FactStatus.DRAFT.value)
            items = [
                fact_item(fact_view(conn, fact_id), roles, facts_repo.source_quote(conn, fact_id))
                for fact_id in ids
            ]
        labels = _upload_labels(kept, self._tz)
        groups = [self._group(item, labels[item.id], items) for item in kept]
        known = {item.id for item in kept}
        others = tuple(item for item in items if item.import_id not in known)
        groups.append(DraftGroup(None, _heading(_OTHER_GROUP, len(others)), others, None))
        return groups, _stamp(kept)

    def drafts_stamp(self) -> str:
        """``DraftsView.stamp`` as it would be now, without reading the drafts."""
        return _stamp(self._kept())

    def group(self, import_id: str) -> DraftGroup | None:
        """The drafts one kept upload still has (none, once all are confirmed); None when
        there is no such upload."""
        if not import_id:
            return None
        return next((group for group in self._groups()[0] if group.id == import_id), None)

    def group_of(self, item: FactItem) -> DraftGroup:
        """The group a fact's block sits in on the Drafts tab: its import's while that
        upload is kept, else the one for the other drafts."""
        groups = self._groups()[0]
        own = next((group for group in groups if group.id and group.id == item.import_id), None)
        return own if own is not None else groups[-1]

    def _group(self, kept: ImportSummary, label: str, items: Sequence[FactItem]) -> DraftGroup:
        own = tuple(item for item in items if item.import_id == kept.id)
        unflagged = sum(1 for item in own if not item.needs_check)
        return DraftGroup(kept.id, _heading(label, len(own)), own, batch_hint(unflagged))

    # --- kept uploads ---------------------------------------------------------------------------

    def uploads(self) -> tuple[UploadView, ...]:
        """Every kept upload, newest first, with what can be done with it: one still to be
        imported can be started (again, after an attempt that ended) unless its task is
        queued or running, and removed unless it is."""
        kept = self._kept()
        labels = _upload_labels(kept, self._tz)
        return tuple(self._upload(item, labels[item.id]) for item in kept)

    def _upload(self, item: ImportSummary, label: str) -> UploadView:
        if item.state == STATE_DONE:
            return UploadView(item.id, label, _NOTE_DONE, None, None, can_remove=True)
        task = self._latest_task(item.id)
        if task is None:
            return UploadView(item.id, label, _NOTE_WAITING, None, _START, can_remove=True)
        if task.progress is not None:  # queued or running: nothing to start or remove
            return UploadView(item.id, label, _NOTE_CANCEL, task, None, can_remove=False)
        return UploadView(item.id, label, None, task, _TRY_AGAIN, can_remove=True)
