"""What the tailored CV screen shows (M6.7; the UI of M6.8 builds on ``DocumentService.view``).

Plain frozen data and the pure steps that shape it: sections in the template's order, role
blocks with headers built from the stored role and the template's date format, the contact block
from the saved details, and the blockers. Headings, role headers and the contact block come from
code here, never from the model. Nothing here reads or writes storage.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.cv_dates import format_partial_date
from app.domain.cv_templates import DetailId, TemplateSpec
from app.domain.documents import DocumentUnit
from app.domain.fact_selection import FactRecord
from app.domain.honesty import CurrentFact, GeneratedUnit, approval_blockers
from app.domain.master_cv import DiffLine, MasterCv
from app.domain.settings import PersonalDetails
from app.modules.storage.documents_repo import StoredUnit
from app.modules.storage.roles_repo import StoredRole
from app.services.tailoring import FactWording, TaskState

PRACTICE_NOTICE: Final = (
    "Practice data (fake provider). This CV is a fictional example, not a real tailored CV."
)


@dataclass(frozen=True)
class FlagView:
    kind: str
    claim: str


@dataclass(frozen=True)
class Blocker:
    unit_id: str
    kind: str
    claim: str


@dataclass(frozen=True)
class UnitView:
    unit_id: str
    position: int
    section: str
    role: str | None
    role_id: str | None
    kind: str
    text: str
    origin: str
    flags: tuple[FlagView, ...]
    fact_ids: tuple[str, ...]
    cited: tuple[FactWording, ...]
    added_details: tuple[str, ...]


@dataclass(frozen=True)
class RoleHeader:
    role_id: str
    title: str
    employer: str
    start_text: str | None
    end_text: str | None
    current: bool


@dataclass(frozen=True)
class RoleBlock:
    header: RoleHeader | None
    units: tuple[UnitView, ...]


@dataclass(frozen=True)
class SectionView:
    id: str
    heading: str
    blocks: tuple[RoleBlock, ...]


@dataclass(frozen=True)
class ContactBlock:
    name: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class VersionInfo:
    id: str
    number: int
    status: str
    parent_version_id: str | None
    origin: str
    provider: str | None
    note: str | None
    created_at: str


@dataclass(frozen=True)
class DocumentView:
    application_id: str
    document_id: str | None
    version: VersionInfo | None
    versions: tuple[VersionInfo, ...]
    units: tuple[UnitView, ...]
    sections: tuple[SectionView, ...]
    contact: ContactBlock
    blockers: tuple[Blocker, ...]
    diff: tuple[DiffLine, ...]
    template: TemplateSpec | None
    practice_notice: str | None
    task: TaskState


def role_label(role: StoredRole) -> str:
    """The label facts and units carry for a role: "<title> at <employer>"."""
    return " at ".join(part for part in (role.title, role.employer) if part)


def contact_block(details: PersonalDetails, template: TemplateSpec | None) -> ContactBlock:
    """The name, then only the details the template shows: email, phone, location, links."""
    shown = frozenset(template.details_shown) if template is not None else frozenset()
    lines: list[str] = []
    if DetailId.EMAIL in shown and details.email:
        lines.append(details.email)
    if DetailId.PHONE in shown and details.phone:
        lines.append(details.phone)
    if DetailId.LOCATION in shown and details.location:
        lines.append(details.location)
    if DetailId.LINKS in shown:
        lines.extend(details.links)
    return ContactBlock(details.name or "", tuple(lines))


def role_header(role: StoredRole, template: TemplateSpec) -> RoleHeader:
    fmt = template.date_format
    return RoleHeader(
        role.id,
        role.title,
        role.employer,
        format_partial_date(role.start_date, fmt),
        format_partial_date(role.end_date, fmt),
        role.start_date is not None and role.end_date is None,
    )


def sections_of(
    template: TemplateSpec,
    units: Sequence[UnitView],
    roles: Mapping[str, StoredRole],
    master: MasterCv,
) -> tuple[SectionView, ...]:
    """The template's sections that have visible units, each with its role blocks: roles in
    master-CV order (most recent first), roles the master does not list after them by label, the
    role-less block last."""
    order = {role.role: index for index, role in enumerate(master.roles) if role.role}
    sections: list[SectionView] = []
    for spec, heading in zip(template.sections, template.headings, strict=True):
        in_section = [unit for unit in units if unit.section == spec.id.value]
        if not in_section:
            continue
        blocks = _blocks(in_section, roles, order, template)
        sections.append(SectionView(spec.id.value, heading, blocks))
    return tuple(sections)


def _blocks(
    units: Sequence[UnitView],
    roles: Mapping[str, StoredRole],
    order: Mapping[str, int],
    template: TemplateSpec,
) -> tuple[RoleBlock, ...]:
    grouped: dict[str | None, list[UnitView]] = {}
    for unit in units:
        key = unit.role_id if unit.role_id in roles else None
        grouped.setdefault(key, []).append(unit)

    def rank(role_id: str | None) -> tuple[int, int, str]:
        if role_id is None:
            return (2, 0, "")
        label = role_label(roles[role_id])
        if label in order:
            return (0, order[label], label)
        return (1, 0, label)

    return tuple(
        RoleBlock(None if role_id is None else role_header(roles[role_id], template), tuple(items))
        for role_id, items in sorted(grouped.items(), key=lambda pair: rank(pair[0]))
    )


def blockers_of(
    stored: Sequence[StoredUnit],
    units: Sequence[DocumentUnit],
    facts_now: Sequence[FactRecord],
) -> tuple[Blocker, ...]:
    """What stops approval now: the stored flags of every unit that is not removed, plus the
    approval check re-run with the facts as they are now (a never-use, unconfirmed or missing
    fact is ``unsupported``; a cited version older than the current one is ``needs_check``).
    Each (unit, kind, claim) once."""
    found: list[Blocker] = [
        Blocker(unit.id, flag.kind, flag.claim)
        for unit in stored
        if not unit.removed
        for flag in unit.flags
    ]
    current = [
        CurrentFact(record.number, record.version, record.status, record.privacy)
        for record in facts_now
        if record.is_current
    ]
    generated = [
        GeneratedUnit(unit.unit_id, unit.kind, unit.section, unit.role, unit.text, unit.fact_ids)
        for unit in units
        if not unit.removed
    ]
    found.extend(
        Blocker(flag.unit_id, flag.kind, flag.claim)
        for flag in approval_blockers(generated, current)
    )
    return tuple(dict.fromkeys(found))


def lines_needing_check(blockers: Sequence[Blocker]) -> int:
    """How many distinct lines have at least one blocker."""
    return len({blocker.unit_id for blocker in blockers})
