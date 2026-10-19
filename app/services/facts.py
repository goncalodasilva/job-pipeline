"""The fact bank's writes (spec 3.2, 3.4, 3.5; ADR-089 item 11, ADR-090).

Every write is one transaction that holds the change and its ``fact_events`` row, and takes
the ``expected_version`` the caller last saw (``FactView.version``): a stale one raises
``VersionConflict`` and writes nothing. Wording changes add a ``fact_versions`` row; status,
role, skills, tags, dates and privacy change the fact itself. Versions and events are never
changed or deleted, so every undo is a new event (and, for a merge, a new version).
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from app.domain.clock import Clock
from app.domain.errors import (
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    VersionConflict,
)
from app.domain.facts import (
    FactContent,
    FactStatus,
    FactType,
    Privacy,
    RoleStatus,
    can_transition,
    clean_part,
    next_version,
    still_reads_as,
)
from app.domain.ids import IdFactory
from app.domain.partial_dates import END_BEFORE_START, checked_partial_date, ends_before_start
from app.modules.storage import Database, capture_repo, facts_repo, roles_repo
from app.services.roles import checked_role, insert_role
from app.services.skills import resolve_skills

SOURCE_MANUAL = "manual"
SOURCE_MERGE = "merge"
SOURCE_MERGE_UNDO = "merge_undo"
SOURCE_REFINE = "refine"

_CONTENT_NEEDED = "Write the fact before saving."
_STORY_NEEDED = "Fill in at least one of situation, action or result."
_MERGE_WORDING_NEEDED = "Write the wording before merging."
_ROLE_UNKNOWN = "This role wasn't found. Choose another role."
_ROLE_KEY_UNKNOWN = "A draft names a role that wasn't given."
_TYPE_UNKNOWN = "Choose fact or story."
_PRIVACY_UNKNOWN = "Choose one of the privacy options."


@dataclass(frozen=True)
class NewRole:
    """A role typed into the editor: stored, confirmed, with the save that names it."""

    employer: str
    title: str


@dataclass(frozen=True)
class FactInput:
    """What the editor posts: the fact's whole editable state. A skill or tag left out is
    removed from the fact. ``new_role`` is stored in the same transaction and wins over
    ``role_id``. ``privacy`` is a ``Privacy`` value; None leaves it as it is (normal for a
    new fact)."""

    type: FactType = FactType.FACT
    text: str | None = None
    situation: str | None = None
    action: str | None = None
    result: str | None = None
    role_id: str | None = None
    skills: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None
    privacy: str | None = None
    new_role: NewRole | None = None


@dataclass(frozen=True)
class DraftOrigin:
    """What made a batch of drafts: stored on each first version (ADR-089 item 8)."""

    source: str
    provider: str | None = None
    model: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
    cv_import_id: str | None = None


@dataclass(frozen=True)
class DraftRole:
    """A role an import found; drafts name it by ``key``."""

    key: str
    employer: str
    title: str
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class DraftInput:
    type: FactType = FactType.FACT
    text: str | None = None
    situation: str | None = None
    action: str | None = None
    result: str | None = None
    role_key: str | None = None
    skills: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    original_answer: str | None = None
    needs_check: bool = False
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class FactView:
    """A fact with its current version. ``version`` is the row's optimistic counter and
    ``content_version`` the 3 in ``F12@3``. ``source``, ``provider`` and ``model`` describe
    the current version. ``needs_check`` is true only for a draft whose current version is
    flagged: the user's own confirm is the check, so no other fact reports it, whatever its
    version rows keep. ``origin_provider`` and ``origin_model`` follow the current wording:
    those of the current version when a provider made it; for the user's own wording, those
    of the latest earlier version a provider made while at least half of the wording's words
    are still that version's (ADR-089 item 8, user decision 2026-10-16); else None."""

    id: str
    number: int
    type: FactType
    status: FactStatus
    privacy: Privacy
    role_id: str | None
    tags: tuple[str, ...]
    start_date: str | None
    end_date: str | None
    skills: tuple[str, ...]
    version: int
    content_version: int
    content: FactContent
    cv_import_id: str | None
    merged_into_fact_id: str | None
    source: str
    provider: str | None
    model: str | None
    needs_check: bool
    origin_provider: str | None
    origin_model: str | None


@dataclass(frozen=True)
class VersionView:
    id: str
    version: int
    content: FactContent
    source: str
    original_answer: str | None
    provider: str | None
    model: str | None
    prompt_id: str | None
    prompt_version: str | None
    needs_check: bool
    created_at: datetime


@dataclass(frozen=True)
class EventView:
    """One thing that happened to a fact. ``detail`` holds ids and fixed words only."""

    type: str
    detail: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class _Extras:
    """What a save carries besides wording and details, as checked: the privacy choice (None
    leaves it) and a new role's fields from ``checked_role`` (None when none was typed)."""

    privacy: str | None
    new_role: tuple[str, str, str | None, str | None] | None


@dataclass(frozen=True)
class _Details:
    """What lives on the fact row besides status and privacy, as stored (ADR-090 item 4)."""

    type: str
    role_id: str | None
    tags: str
    start_date: str | None
    end_date: str | None


@dataclass(frozen=True)
class _State:
    """Everything ``facts_repo.update_fact`` writes."""

    type: str
    status: str
    role_id: str | None
    privacy: str
    tags: str
    start_date: str | None
    end_date: str | None
    current_version_id: str
    merged_into_fact_id: str | None


@dataclass(frozen=True)
class _Wording:
    """A first version's content and where it came from."""

    content: FactContent
    source: str
    original_answer: str | None = None
    needs_check: bool = False
    origin: DraftOrigin | None = None


def _content_of(version: facts_repo.StoredVersion) -> FactContent:
    return FactContent(version.text, version.situation, version.action, version.result)


def _checked_content(
    text: str | None,
    situation: str | None,
    action: str | None,
    result: str | None,
    *,
    needed: str = _CONTENT_NEEDED,
) -> FactContent:
    """The wording as stored. Blank wording is refused with ``needed``, always on ``text``:
    a form without that field ties the message to its first wording field."""
    content = FactContent(
        clean_part(text), clean_part(situation), clean_part(action), clean_part(result)
    )
    if not any(content.normalized()):
        raise FactInputInvalid("text", needed)
    return content


def _needed(type: object) -> str:
    """What a blank item of this type is told: a story is asked for one of its parts."""
    return _STORY_NEEDED if type == FactType.STORY.value else _CONTENT_NEEDED


def _refuse_reversed_dates(details: _Details) -> None:
    if ends_before_start(details.start_date, details.end_date):
        raise FactInputInvalid("end_date", END_BEFORE_START)


def _tags(tags: Iterable[str]) -> str:
    cleaned = dict.fromkeys(tag.strip() for tag in tags)
    return json.dumps([tag for tag in cleaned if tag])


def _checked_details(
    type: FactType,
    role_id: str | None,
    tags: Iterable[str],
    start_date: str | None,
    end_date: str | None,
) -> _Details:
    try:
        kind = FactType(type).value
    except ValueError:
        raise FactInputInvalid("type", _TYPE_UNKNOWN) from None
    return _Details(
        type=kind,
        # A form posts "" for "no role".
        role_id=role_id if role_id and role_id.strip() else None,
        tags=_tags(tags),
        start_date=checked_partial_date("start_date", start_date),
        end_date=checked_partial_date("end_date", end_date),
    )


def _checked_privacy(privacy: str | None) -> str | None:
    if privacy is None:
        return None
    try:
        return Privacy(privacy).value
    except ValueError:
        raise FactInputInvalid("privacy", _PRIVACY_UNKNOWN) from None


def _checked_input(data: FactInput) -> tuple[FactContent, _Details, _Extras]:
    """The editor's form as stored, checked before anything is written. Whether the end is
    before the start is the caller's check: an edit asks only when it changes the dates."""
    content = _checked_content(
        data.text, data.situation, data.action, data.result, needed=_needed(data.type)
    )
    details = _checked_details(data.type, data.role_id, data.tags, data.start_date, data.end_date)
    role = data.new_role
    new_role = None if role is None else checked_role(role.employer, role.title, None, None)
    return content, details, _Extras(_checked_privacy(data.privacy), new_role)


def _state(row: facts_repo.StoredFact) -> _State:
    return _State(
        type=row.type,
        status=row.status,
        role_id=row.role_id,
        privacy=row.privacy,
        tags=row.tags,
        start_date=row.start_date,
        end_date=row.end_date,
        current_version_id=row.current.id,
        merged_into_fact_id=row.merged_into_fact_id,
    )


def _view_of(
    row: facts_repo.StoredFact,
    skills: tuple[str, ...],
    origin: tuple[str, str | None] | None,
) -> FactView:
    current = row.current
    origin_provider, origin_model = origin or (None, None)
    return FactView(
        id=row.id,
        number=row.number,
        type=FactType(row.type),
        status=FactStatus(row.status),
        privacy=Privacy(row.privacy),
        role_id=row.role_id,
        tags=tuple(json.loads(row.tags)),
        start_date=row.start_date,
        end_date=row.end_date,
        skills=skills,
        version=row.version,
        content_version=current.version,
        content=_content_of(current),
        cv_import_id=row.cv_import_id,
        merged_into_fact_id=row.merged_into_fact_id,
        source=current.source,
        provider=current.provider,
        model=current.model,
        needs_check=row.status == FactStatus.DRAFT and current.needs_check,
        origin_provider=origin_provider,
        origin_model=origin_model,
    )


def _version_view(row: facts_repo.StoredVersion) -> VersionView:
    return VersionView(
        id=row.id,
        version=row.version,
        content=_content_of(row),
        source=row.source,
        original_answer=row.original_answer,
        provider=row.provider,
        model=row.model,
        prompt_id=row.prompt_id,
        prompt_version=row.prompt_version,
        needs_check=row.needs_check,
        created_at=datetime.fromisoformat(row.created_at),
    )


def fact_view(conn: sqlite3.Connection, fact_id: str) -> FactView:
    """The fact as it is on ``conn`` right now. Raises ``FactNotFound``."""
    row = facts_repo.get_fact(conn, fact_id)
    if row is None:
        raise FactNotFound(detail=f"fact {fact_id} not found")
    return _view_of(row, facts_repo.skill_names(conn, fact_id), _origin(conn, row))


def _origin(conn: sqlite3.Connection, row: facts_repo.StoredFact) -> tuple[str, str | None] | None:
    """Where the current wording came from: the provider that made the current version;
    for the user's own, the latest earlier version a provider made, while the wording still
    reads as it (``still_reads_as``); else None."""
    current = row.current
    if current.provider is not None:
        return current.provider, current.model
    earlier = facts_repo.latest_by_provider(conn, row.id, besides=current.id)
    if earlier is None or earlier.provider is None:
        return None
    if not still_reads_as(_content_of(current), _content_of(earlier)):
        return None
    return earlier.provider, earlier.model


def _load(conn: sqlite3.Connection, fact_id: str, expected_version: int) -> facts_repo.StoredFact:
    row = facts_repo.get_fact(conn, fact_id)
    if row is None:
        raise FactNotFound(detail=f"fact {fact_id} not found")
    if row.version != expected_version:
        raise VersionConflict(detail=f"fact {fact_id} changed elsewhere")
    return row


def _save(conn: sqlite3.Connection, row: facts_repo.StoredFact, state: _State, now: str) -> None:
    changed = facts_repo.update_fact(
        conn, row.id, expected_version=row.version, now=now, **asdict(state)
    )
    if not changed:  # pragma: no cover - _load compared the version in this transaction
        raise VersionConflict(detail=f"fact {row.id} changed elsewhere")


def _taken_from(
    settled: FactContent, target: facts_repo.StoredFact, draft: facts_repo.StoredFact
) -> facts_repo.StoredVersion | None:
    """The version whose wording a merge took as it stands, or None when the user wrote new
    wording. When both read the same, the target's: its wording is the one that was kept."""
    wanted = settled.normalized()
    for version in (target.current, draft.current):
        if _content_of(version).normalized() == wanted:
            return version
    return None


def _require_role(conn: sqlite3.Connection, role_id: str | None) -> None:
    if role_id is not None and roles_repo.get_role(conn, role_id) is None:
        raise FactInputInvalid("role_id", _ROLE_UNKNOWN)


def _merge_to_undo(
    conn: sqlite3.Connection, draft: facts_repo.StoredFact, target: facts_repo.StoredFact
) -> tuple[facts_repo.StoredMerge, dict[str, facts_repo.StoredVersion]] | None:
    """The merge of ``draft`` into ``target`` and the target's versions by id, while the
    target still reads as that merge left it; else None. Wording is compared, not version
    ids: undoing a later merge leaves a new version that reads as this merge left it."""
    merge = facts_repo.latest_merge(conn, target.id, draft.id)
    versions = {row.id: row for row in facts_repo.list_versions(conn, target.id)}
    merged = None if merge is None else versions.get(merge.fact_version_id or "")
    if merge is None or merged is None:
        return None
    if _content_of(merged).normalized() != _content_of(target.current).normalized():
        return None
    return merge, versions


class FactService:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids

    # --- reading ------------------------------------------------------------------

    def get(self, fact_id: str) -> FactView:
        with self._db.read() as conn:
            return fact_view(conn, fact_id)

    def versions(self, fact_id: str) -> tuple[VersionView, ...]:
        """Every version of a fact, oldest first."""
        with self._db.read() as conn:
            if facts_repo.get_fact(conn, fact_id) is None:
                raise FactNotFound(detail=f"fact {fact_id} not found")
            return tuple(_version_view(row) for row in facts_repo.list_versions(conn, fact_id))

    def events(self, fact_id: str) -> tuple[EventView, ...]:
        """Everything that happened to a fact, oldest first (ties in insertion order)."""
        with self._db.read() as conn:
            if facts_repo.get_fact(conn, fact_id) is None:
                raise FactNotFound(detail=f"fact {fact_id} not found")
            return tuple(
                EventView(
                    row.type,
                    json.loads(row.detail) if row.detail else {},
                    datetime.fromisoformat(row.created_at),
                )
                for row in facts_repo.list_events(conn, fact_id)
            )

    def can_undo_merge(self, draft_id: str) -> bool:
        """Whether ``undo_merge`` would take this draft's merge back now: it was closed by
        a merge and its target still reads as the merge left it. Reads only."""
        with self._db.read() as conn:
            draft = facts_repo.get_fact(conn, draft_id)
            if draft is None:
                raise FactNotFound(detail=f"fact {draft_id} not found")
            if draft.status != FactStatus.DISCARDED or draft.merged_into_fact_id is None:
                return False
            target = facts_repo.get_fact(conn, draft.merged_into_fact_id)
            return target is not None and _merge_to_undo(conn, draft, target) is not None

    # --- creating -----------------------------------------------------------------

    def create(self, data: FactInput, *, note_id: str | None = None) -> FactView:
        """Add a fact the user typed: confirmed at once (ADR-089 item 11), with its new
        role, skills and privacy choice in the one transaction. ``note_id`` names the capture
        note it was turned from (M3.7): while that note is open, the same transaction records
        the fact on it, so it leaves the inbox; otherwise the note is left as it is."""
        content, details, extras = _checked_input(data)
        _refuse_reversed_dates(details)
        now = self._now()
        with self._db.transaction() as conn:
            details = self._with_role(conn, details, extras, now)
            fact_id = self._insert(
                conn,
                _Wording(content, SOURCE_MANUAL),
                details,
                FactStatus.CONFIRMED,
                data.skills,
                now,
                privacy=extras.privacy or Privacy.NORMAL.value,
            )
            self._confirm_role(conn, details.role_id, now)
            if note_id is not None:
                capture_repo.resolve_note(conn, note_id, fact_id=fact_id, now=now)
            return fact_view(conn, fact_id)

    def create_drafts(
        self,
        origin: DraftOrigin,
        drafts: Sequence[DraftInput],
        *,
        roles: Sequence[DraftRole] = (),
    ) -> tuple[FactView, ...]:
        """Store what an AI task found as drafts, with its draft roles, in one transaction."""
        with self._db.transaction() as conn:
            return self.create_drafts_in(conn, origin, drafts, roles=roles)

    def create_drafts_in(
        self,
        conn: sqlite3.Connection,
        origin: DraftOrigin,
        drafts: Sequence[DraftInput],
        *,
        roles: Sequence[DraftRole] = (),
    ) -> tuple[FactView, ...]:
        """``create_drafts`` inside a transaction the caller owns, such as a task's result
        writer. Everything is checked before the first row is written."""
        role_fields = {
            role.key: checked_role(role.employer, role.title, role.start_date, role.end_date)
            for role in roles
        }
        checked = [self._checked_draft(draft, role_fields) for draft in drafts]
        now = self._now()
        role_ids = {key: self._ids.new() for key in role_fields}
        for key, fields in role_fields.items():
            insert_role(conn, role_ids[key], fields, RoleStatus.DRAFT, now)
        made = []
        for draft, content, details in checked:
            wording = _Wording(
                content, origin.source, draft.original_answer, draft.needs_check, origin
            )
            if draft.role_key is not None:
                details = replace(details, role_id=role_ids[draft.role_key])
            made.append(self._insert(conn, wording, details, FactStatus.DRAFT, draft.skills, now))
        return tuple(fact_view(conn, fact_id) for fact_id in made)

    # --- editing ------------------------------------------------------------------

    def edit(self, fact_id: str, data: FactInput, *, expected_version: int) -> FactView:
        """Save the editor's form in one transaction. Changed wording adds a version (event
        ``edited``); changed type, role, skills, tags or dates add ``details_changed``; a
        changed privacy choice adds ``privacy_changed``; an identical edit writes nothing.
        A discarded fact can't be edited. An end date before the start is refused when the
        save changes either date."""
        return self._edit(fact_id, data, expected_version, confirm=False)

    def edit_and_confirm(self, fact_id: str, data: FactInput, *, expected_version: int) -> FactView:
        """Edit a draft and confirm it in one step."""
        return self._edit(fact_id, data, expected_version, confirm=True)

    def reword_and_confirm(
        self, fact_id: str, wording: Mapping[str, str], *, expected_version: int
    ) -> FactView:
        """A draft's inline form: the parts ``wording`` names (``text``, ``situation``,
        ``action``, ``result``) replace the draft's, and it is confirmed. Its other parts,
        type, role, skills, tags and dates stay; unchanged wording adds no version. The
        version check covers the read made here: a draft that changed in between is stale."""
        current = self.get(fact_id)
        content = current.content
        data = FactInput(
            type=current.type,
            text=wording.get("text", content.text),
            situation=wording.get("situation", content.situation),
            action=wording.get("action", content.action),
            result=wording.get("result", content.result),
            role_id=current.role_id,
            skills=current.skills,
            tags=current.tags,
            start_date=current.start_date,
            end_date=current.end_date,
        )
        return self.edit_and_confirm(fact_id, data, expected_version=expected_version)

    def set_privacy(self, fact_id: str, privacy: Privacy, *, expected_version: int) -> FactView:
        wanted = Privacy(privacy).value
        now = self._now()
        with self._db.transaction() as conn:
            row = _load(conn, fact_id, expected_version)
            if row.privacy != wanted:
                _save(conn, row, replace(_state(row), privacy=wanted), now)
                detail = {"from": row.privacy, "to": wanted}
                self._event(conn, row.id, "privacy_changed", row.current.id, now, detail)
            return fact_view(conn, fact_id)

    def save_refined_in(
        self,
        conn: sqlite3.Connection,
        fact_id: str,
        content: FactContent,
        *,
        expected_version: int,
        origin: DraftOrigin,
        detail: Mapping[str, object],
    ) -> FactView:
        """Inside a transaction the caller owns (Refine with AI's Accept, M3.8): the wording
        the user accepted becomes the fact's next version, source ``refine``, keeping the
        provider, model and prompt of the suggestion it came from, with a ``refined`` event
        (``detail``: ids only). Only a draft or a confirmed fact can be refined; a draft stays
        a draft, and its flag stays until the user confirms it (ADR-091). Wording that reads
        as the current one adds nothing. Raises ``VersionConflict``,
        ``FactTransitionRefused`` or ``FactInputInvalid``."""
        settled = _checked_content(content.text, content.situation, content.action, content.result)
        row = _load(conn, fact_id, expected_version)
        if row.status not in (FactStatus.DRAFT, FactStatus.CONFIRMED):
            raise FactTransitionRefused(detail=f"fact {fact_id} is {row.status}: no refine")
        number = next_version(row.current.version, _content_of(row.current), settled)
        if number is None:
            return fact_view(conn, fact_id)
        now = self._now()
        version_id = self._ids.new()
        facts_repo.insert_version(
            conn,
            facts_repo.StoredVersion(
                id=version_id,
                fact_id=row.id,
                version=number,
                text=settled.text,
                situation=settled.situation,
                action=settled.action,
                result=settled.result,
                source=origin.source,
                original_answer=None,
                provider=origin.provider,
                model=origin.model,
                prompt_id=origin.prompt_id,
                prompt_version=origin.prompt_version,
                # The user asserted what they accepted; only a draft keeps its flag.
                needs_check=row.status == FactStatus.DRAFT and row.current.needs_check,
                created_at=now,
            ),
        )
        _save(conn, row, replace(_state(row), current_version_id=version_id), now)
        self._event(conn, row.id, "refined", version_id, now, detail)
        return fact_view(conn, fact_id)

    # --- status -------------------------------------------------------------------

    def confirm(self, fact_id: str, *, expected_version: int) -> FactView:
        """Confirm a draft; its draft role is confirmed with it. For a draft that needs a
        check this is the user's check: the view stops reporting the flag."""
        return self._move(
            fact_id, FactStatus.DRAFT, FactStatus.CONFIRMED, "confirmed", expected_version
        )

    def discard(self, fact_id: str, *, expected_version: int) -> FactView:
        return self._move(
            fact_id, FactStatus.DRAFT, FactStatus.DISCARDED, "discarded", expected_version
        )

    def undo_discard(self, fact_id: str, *, expected_version: int) -> FactView:
        """Bring a discarded draft back. A draft closed by a merge needs ``undo_merge``."""
        return self._move(
            fact_id, FactStatus.DISCARDED, FactStatus.DRAFT, "discard_undone", expected_version
        )

    def archive(self, fact_id: str, *, expected_version: int) -> FactView:
        return self._move(
            fact_id, FactStatus.CONFIRMED, FactStatus.ARCHIVED, "archived", expected_version
        )

    def restore(self, fact_id: str, *, expected_version: int) -> FactView:
        return self._move(
            fact_id, FactStatus.ARCHIVED, FactStatus.CONFIRMED, "restored", expected_version
        )

    def confirm_import(
        self, cv_import_id: str, *, expected_versions: Mapping[str, int]
    ) -> tuple[FactView, ...]:
        """Confirm the listed drafts of one import together, or none of them. A listed fact
        that changed raises ``VersionConflict``; one that isn't a draft of that import raises
        ``FactTransitionRefused``. A draft that needs a check is left a draft, for the user
        to confirm on its own: only the facts confirmed are returned."""
        now = self._now()
        with self._db.transaction() as conn:
            rows = [_load(conn, fact_id, seen) for fact_id, seen in expected_versions.items()]
            for row in rows:
                if row.cv_import_id != cv_import_id or row.status != FactStatus.DRAFT:
                    raise FactTransitionRefused(
                        detail=f"fact {row.id} is not a draft of import {cv_import_id}"
                    )
            checked = [row for row in rows if not row.current.needs_check]
            for row in checked:
                self._set_status(conn, row, FactStatus.CONFIRMED, "confirmed", now)
            return tuple(fact_view(conn, row.id) for row in checked)

    # --- merging ------------------------------------------------------------------

    def merge(
        self,
        draft_id: str,
        target_id: str,
        content: FactContent,
        *,
        expected_draft_version: int,
        expected_target_version: int,
    ) -> FactView:
        """Fold a draft into another fact. The target always gets a new version (source
        ``merge``) with the wording the user settled on, and the draft's skills; the draft
        is closed as discarded and linked to it. Wording taken as it stands from the target
        or the draft keeps that version's provider, model, prompt and ``needs_check``;
        wording the user wrote carries none. Returns the target."""
        if draft_id == target_id:
            raise FactTransitionRefused(detail=f"fact {draft_id} can't be merged into itself")
        settled = _checked_content(
            content.text,
            content.situation,
            content.action,
            content.result,
            needed=_MERGE_WORDING_NEEDED,
        )
        now = self._now()
        with self._db.transaction() as conn:
            draft = _load(conn, draft_id, expected_draft_version)
            target = _load(conn, target_id, expected_target_version)
            # A draft folds only into a confirmed fact (decided by the user, 2026-10-16).
            if draft.status != FactStatus.DRAFT or target.status != FactStatus.CONFIRMED:
                raise FactTransitionRefused(
                    detail=f"merge of a {draft.status} fact into a {target.status} fact"
                )
            version_id = self._add_version(
                conn,
                target,
                settled,
                SOURCE_MERGE,
                now,
                made_by=_taken_from(settled, target, draft),
            )
            added = sorted(
                facts_repo.skill_ids(conn, draft.id) - facts_repo.skill_ids(conn, target.id)
            )
            for skill_id in added:
                facts_repo.link_skill(conn, target.id, skill_id)
            _save(conn, target, replace(_state(target), current_version_id=version_id), now)
            # What undo needs: the version to go back to and the skills to take away again.
            detail = {
                "draft_id": draft.id,
                "previous_version_id": target.current.id,
                "added_skill_ids": added,
            }
            self._event(conn, target.id, "merged", version_id, now, detail)
            closed = replace(
                _state(draft), status=FactStatus.DISCARDED.value, merged_into_fact_id=target.id
            )
            _save(conn, draft, closed, now)
            self._event(
                conn, draft.id, "merged_into", draft.current.id, now, {"target_id": target.id}
            )
            return fact_view(conn, target.id)

    def undo_merge(
        self, draft_id: str, *, expected_draft_version: int, expected_target_version: int
    ) -> FactView:
        """Take a merge back: the target gets a new version (source ``merge_undo``) that
        repeats the one from before the merge, loses the skills the merge added, and the
        draft is a draft again. Refused while the target doesn't read as the merge left it:
        it was reworded, or a later merge still stands (undo that one first). The skills
        are left alone once the user saved the target's details after the merge. Returns
        the draft."""
        now = self._now()
        with self._db.transaction() as conn:
            draft = _load(conn, draft_id, expected_draft_version)
            if draft.status != FactStatus.DISCARDED or draft.merged_into_fact_id is None:
                raise FactTransitionRefused(detail=f"fact {draft_id} wasn't closed by a merge")
            target = _load(conn, draft.merged_into_fact_id, expected_target_version)
            found = _merge_to_undo(conn, draft, target)
            if found is None:
                raise FactTransitionRefused(detail=f"fact {target.id} was reworded after the merge")
            merge, versions = found
            detail = json.loads(merge.detail or "{}")
            before = versions[detail["previous_version_id"]]
            version_id = self._ids.new()
            facts_repo.insert_version(
                conn,
                replace(
                    before,
                    id=version_id,
                    version=target.current.version + 1,
                    source=SOURCE_MERGE_UNDO,
                    created_at=now,
                ),
            )
            # A details change since the merge means the user saved the skill list as it is.
            if not facts_repo.details_changed_after(conn, target.id, merge.event_id):
                for skill_id in detail["added_skill_ids"]:
                    facts_repo.unlink_skill(conn, target.id, skill_id)
            _save(conn, target, replace(_state(target), current_version_id=version_id), now)
            self._event(conn, target.id, "merge_undone", version_id, now, {"draft_id": draft.id})
            reopened = replace(
                _state(draft), status=FactStatus.DRAFT.value, merged_into_fact_id=None
            )
            _save(conn, draft, reopened, now)
            self._event(
                conn, draft.id, "merge_undone", draft.current.id, now, {"target_id": target.id}
            )
            return fact_view(conn, draft.id)

    # --- inside a transaction -----------------------------------------------------

    def _now(self) -> str:
        return self._clock.now().isoformat()

    def _checked_draft(
        self,
        draft: DraftInput,
        role_fields: Mapping[str, object],
    ) -> tuple[DraftInput, FactContent, _Details]:
        if draft.role_key is not None and draft.role_key not in role_fields:
            raise FactInputInvalid("role_key", _ROLE_KEY_UNKNOWN)
        content = _checked_content(
            draft.text, draft.situation, draft.action, draft.result, needed=_needed(draft.type)
        )
        details = _checked_details(draft.type, None, draft.tags, draft.start_date, draft.end_date)
        return draft, content, details

    def _with_role(
        self, conn: sqlite3.Connection, details: _Details, extras: _Extras, now: str
    ) -> _Details:
        """The details with the role the save names: a typed one is stored here, confirmed
        (the user wrote it), and wins; a picked one must exist."""
        if extras.new_role is None:
            _require_role(conn, details.role_id)
            return details
        role_id = self._ids.new()
        insert_role(conn, role_id, extras.new_role, RoleStatus.CONFIRMED, now)
        return replace(details, role_id=role_id)

    def _insert(
        self,
        conn: sqlite3.Connection,
        wording: _Wording,
        details: _Details,
        status: FactStatus,
        skills: Iterable[str],
        now: str,
        *,
        privacy: str = Privacy.NORMAL.value,
    ) -> str:
        """A fact, its first version, its skill links and its ``created`` event."""
        fact_id, version_id = self._ids.new(), self._ids.new()
        origin = wording.origin or DraftOrigin(wording.source)
        facts_repo.insert_fact(
            conn,
            fact_id=fact_id,
            # ADR-090: picked here, inside the write transaction.
            number=facts_repo.next_number(conn),
            status=status.value,
            current_version_id=version_id,
            cv_import_id=origin.cv_import_id,
            now=now,
            privacy=privacy,
            **asdict(details),
        )
        content = wording.content
        facts_repo.insert_version(
            conn,
            facts_repo.StoredVersion(
                id=version_id,
                fact_id=fact_id,
                version=1,
                text=content.text,
                situation=content.situation,
                action=content.action,
                result=content.result,
                source=wording.source,
                original_answer=wording.original_answer,
                provider=origin.provider,
                model=origin.model,
                prompt_id=origin.prompt_id,
                prompt_version=origin.prompt_version,
                needs_check=wording.needs_check,
                created_at=now,
            ),
        )
        for skill_id in resolve_skills(conn, skills, ids=self._ids, now=now):
            facts_repo.link_skill(conn, fact_id, skill_id)
        self._event(conn, fact_id, "created", version_id, now)
        return fact_id

    def _add_version(
        self,
        conn: sqlite3.Connection,
        row: facts_repo.StoredFact,
        content: FactContent,
        source: str,
        now: str,
        *,
        made_by: facts_repo.StoredVersion | None = None,
        needs_check: bool = False,
    ) -> str:
        """The fact's next version. The user's own wording carries no provider: nothing
        generated it; ``needs_check`` says whether it still waits for the user's check.
        Wording taken as it stands from the version ``made_by`` keeps what made that one,
        and is flagged when that one was. The caller points the fact at it."""
        version_id = self._ids.new()
        facts_repo.insert_version(
            conn,
            facts_repo.StoredVersion(
                id=version_id,
                fact_id=row.id,
                version=row.current.version + 1,
                text=content.text,
                situation=content.situation,
                action=content.action,
                result=content.result,
                source=source,
                original_answer=None,
                provider=None if made_by is None else made_by.provider,
                model=None if made_by is None else made_by.model,
                prompt_id=None if made_by is None else made_by.prompt_id,
                prompt_version=None if made_by is None else made_by.prompt_version,
                needs_check=needs_check or (made_by is not None and made_by.needs_check),
                created_at=now,
            ),
        )
        return version_id

    def _event(
        self,
        conn: sqlite3.Connection,
        fact_id: str,
        type: str,
        version_id: str,
        now: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Ids and fixed words only in ``detail``: never the fact's wording."""
        facts_repo.insert_event(
            conn,
            event_id=self._ids.new(),
            fact_id=fact_id,
            type=type,
            fact_version_id=version_id,
            detail=None if detail is None else json.dumps(detail),
            now=now,
        )

    def _confirm_role(self, conn: sqlite3.Connection, role_id: str | None, now: str) -> None:
        """A draft role is confirmed with its first confirmed fact; a confirmed one is left
        as it is."""
        if role_id is not None:
            roles_repo.confirm_role(conn, role_id, now=now)

    def _set_status(
        self,
        conn: sqlite3.Connection,
        row: facts_repo.StoredFact,
        target: FactStatus,
        event: str,
        now: str,
    ) -> None:
        _save(conn, row, replace(_state(row), status=target.value), now)
        self._event(conn, row.id, event, row.current.id, now)
        if target is FactStatus.CONFIRMED:
            self._confirm_role(conn, row.role_id, now)

    def _move(
        self,
        fact_id: str,
        source: FactStatus,
        target: FactStatus,
        event: str,
        expected_version: int,
    ) -> FactView:
        """One status change with its event. ``source`` is named because the lifecycle alone
        can't tell confirming a draft from restoring an archived fact."""
        now = self._now()
        with self._db.transaction() as conn:
            row = _load(conn, fact_id, expected_version)
            allowed = row.status == source and can_transition(source, target)
            if not allowed or row.merged_into_fact_id is not None:
                raise FactTransitionRefused(detail=f"fact {fact_id} is {row.status}: no {event}")
            self._set_status(conn, row, target, event, now)
            return fact_view(conn, fact_id)

    def _set_skills(
        self, conn: sqlite3.Connection, fact_id: str, names: Iterable[str], now: str
    ) -> bool:
        """Make the fact's links the skills ``names`` mean; whether anything changed."""
        wanted = frozenset(resolve_skills(conn, names, ids=self._ids, now=now))
        linked = facts_repo.skill_ids(conn, fact_id)
        for skill_id in sorted(wanted - linked):
            facts_repo.link_skill(conn, fact_id, skill_id)
        for skill_id in sorted(linked - wanted):
            facts_repo.unlink_skill(conn, fact_id, skill_id)
        return wanted != linked

    def _edit(
        self, fact_id: str, data: FactInput, expected_version: int, *, confirm: bool
    ) -> FactView:
        content, details, extras = _checked_input(data)
        now = self._now()
        with self._db.transaction() as conn:
            row = _load(conn, fact_id, expected_version)
            editable = (
                row.status == FactStatus.DRAFT if confirm else (row.status != FactStatus.DISCARDED)
            )
            if not editable:
                raise FactTransitionRefused(detail=f"fact {fact_id} is {row.status}: no edit")
            # Dates the save leaves as they are stored are not this save's to refuse: a
            # draft's inline form sends them back unseen (an import may store them reversed).
            if (details.start_date, details.end_date) != (row.start_date, row.end_date):
                _refuse_reversed_dates(details)
            details = self._with_role(conn, details, extras, now)
            self._apply_edit(
                conn,
                row,
                content,
                details,
                data.skills,
                now,
                confirm=confirm,
                privacy=extras.privacy,
            )
            return fact_view(conn, fact_id)

    def _apply_edit(
        self,
        conn: sqlite3.Connection,
        row: facts_repo.StoredFact,
        content: FactContent,
        details: _Details,
        skills: Iterable[str],
        now: str,
        *,
        confirm: bool,
        privacy: str | None = None,
    ) -> None:
        before = _state(row)
        reworded = next_version(row.current.version, _content_of(row.current), content)
        version_id = row.current.id
        if reworded is not None:
            # A draft's flag stays until the user confirms that draft themselves.
            flagged = row.status == FactStatus.DRAFT and not confirm and row.current.needs_check
            version_id = self._add_version(
                conn, row, content, SOURCE_MANUAL, now, needs_check=flagged
            )
        after = replace(before, current_version_id=version_id, **asdict(details))
        skills_changed = self._set_skills(conn, row.id, skills, now)
        details_changed = skills_changed or after != replace(before, current_version_id=version_id)
        # Privacy is its own event, never a details change.
        privacy_changed = privacy is not None and privacy != before.privacy
        if privacy is not None:
            after = replace(after, privacy=privacy)
        if confirm:
            after = replace(after, status=FactStatus.CONFIRMED.value)
        if reworded is None and not details_changed and not privacy_changed and not confirm:
            return
        _save(conn, row, after, now)
        if reworded is not None:
            self._event(conn, row.id, "edited", version_id, now)
        if details_changed:
            self._event(conn, row.id, "details_changed", version_id, now)
        if privacy_changed:
            detail = {"from": before.privacy, "to": after.privacy}
            self._event(conn, row.id, "privacy_changed", version_id, now, detail)
        if confirm:
            self._event(conn, row.id, "confirmed", version_id, now)
        if after.status == FactStatus.CONFIRMED:
            self._confirm_role(conn, after.role_id, now)
