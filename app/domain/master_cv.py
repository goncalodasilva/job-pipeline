"""The master CV and the diff of a tailored CV against it (ADR-106 item 5; detailed design
"Diff conventions"). Pure: no I/O, clock or ids.

The master CV is every usable fact (``facts_for_ai``: confirmed, current, not "never use"),
grouped by role, most recent role first. ``cv_diff`` compares a tailored CV's lines with it by
fact number, never by wording. Texts are data: never escaped or changed.
"""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.domain.diff import DiffSegment, word_diff
from app.domain.documents import DocumentUnit, fact_key
from app.domain.fact_selection import AIFact, FactRecord, facts_for_ai
from app.domain.facts import FactRef
from app.domain.partial_dates import earliest_day, latest_day


@dataclass(frozen=True)
class RolePeriod:
    """The partial ISO dates of a role; ``end=None`` with a ``start`` is a current role."""

    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class MasterLine:
    ref: FactRef
    role: str | None
    text: str


@dataclass(frozen=True)
class MasterRole:
    role: str | None
    start: str | None
    end: str | None
    lines: tuple[MasterLine, ...]


@dataclass(frozen=True)
class MasterCv:
    roles: tuple[MasterRole, ...]

    @property
    def lines(self) -> tuple[MasterLine, ...]:
        return tuple(line for role in self.roles for line in role.lines)


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _line_text(fact: AIFact) -> str:
    content = fact.content
    parts = (content.text, content.situation, content.action, content.result)
    return _collapsed(" ".join(part for part in parts if part and part.strip()))


# --- Role order ---------------------------------------------------------------------------------


def _role_sort_key(role: str | None, period: RolePeriod) -> tuple[int, int, int, str, str]:
    """Current roles, then by latest end (later first), later start, label; undated roles
    follow in label order; the role-less group is last."""
    if role is None:
        return (3, 0, 0, "", "")
    label = (role.casefold(), role)
    start = period.start
    if start is not None and period.end is None:
        return (0, -earliest_day(start).toordinal(), 0, *label)
    if period.end is not None:
        started = earliest_day(start).toordinal() if start is not None else date.min.toordinal()
        return (1, -latest_day(period.end).toordinal(), -started, *label)
    return (2, 0, 0, *label)


def _group_by_role(facts: Iterable[AIFact]) -> dict[str | None, list[AIFact]]:
    groups: dict[str | None, list[AIFact]] = defaultdict(list)
    for fact in facts:
        groups[fact.role].append(fact)
    return groups


def build_master_cv(
    records: Iterable[FactRecord], role_dates: Mapping[str, RolePeriod] | None = None
) -> MasterCv:
    """The master CV of the usable facts; roles with no usable fact aren't listed."""
    dates = role_dates or {}
    groups = _group_by_role(facts_for_ai(records))
    periods = {role: dates.get(role, RolePeriod()) if role else RolePeriod() for role in groups}
    roles: list[MasterRole] = []
    for role in sorted(groups, key=lambda r: _role_sort_key(r, periods[r])):
        facts = sorted(groups[role], key=lambda f: f.ref.number)
        lines = tuple(MasterLine(f.ref, role, _line_text(f)) for f in facts)
        roles.append(MasterRole(role, periods[role].start, periods[role].end, lines))
    return MasterCv(tuple(roles))


# --- The diff -----------------------------------------------------------------------------------


class LineMark(StrEnum):
    UNCHANGED = "unchanged"
    ADDED = "added"
    REMOVED = "removed"
    REWORDED = "reworded"
    MOVED = "moved"


@dataclass(frozen=True)
class DiffLine:
    """One line of the diff. ``master_line`` indexes ``MasterCv.lines`` (None for ADDED);
    ``unit_id`` is the tailored unit's (None for REMOVED); ``segments`` hold the word diff of
    a REWORDED line; ``moved`` is also set on a reworded line that moved."""

    mark: LineMark
    text: str
    fact_ids: tuple[str, ...]
    master_line: int | None
    unit_id: str | None
    segments: tuple[DiffSegment, ...] = ()
    moved: bool = False


def _match_lines(master: Sequence[MasterLine], units: Sequence[DocumentUnit]) -> list[int | None]:
    """For each unit, the master line it stands for, each master line at most once; None for
    a unit that is new (no ids, unknown ids, several facts, or a repeated citation)."""
    by_number = {line.ref.number: index for index, line in reversed(list(enumerate(master)))}
    taken: set[int] = set()
    matches: list[int | None] = []
    for unit in units:
        key = fact_key(unit.fact_ids)
        number = next(iter(key)) if len(key) == 1 else None
        index = by_number.get(number) if isinstance(number, int) else None
        if index is None or index in taken:
            matches.append(None)
        else:
            taken.add(index)
            matches.append(index)
    return matches


def _lines_in_order(matches: Sequence[int | None]) -> set[int]:
    """The positions in ``matches`` of one longest run of master lines in master order."""
    found = [(pos, index) for pos, index in enumerate(matches) if index is not None]
    tails: list[int] = []  # smallest last master index of a run of each length
    tail_pos: list[int] = []
    previous: dict[int, int | None] = {}
    for pos, index in found:
        length = bisect_left(tails, index)
        if length == len(tails):
            tails.append(index)
            tail_pos.append(pos)
        else:
            tails[length] = index
            tail_pos[length] = pos
        previous[pos] = tail_pos[length - 1] if length else None
    run: set[int] = set()
    cursor = tail_pos[-1] if tail_pos else None
    while cursor is not None:
        run.add(cursor)
        cursor = previous[cursor]
    return run


def _tailored_line(
    unit: DocumentUnit, master: Sequence[MasterLine], index: int | None, in_order: bool
) -> DiffLine:
    if index is None:
        return DiffLine(LineMark.ADDED, unit.text, unit.fact_ids, None, unit.unit_id)
    moved = not in_order
    if _collapsed(unit.text) != _collapsed(master[index].text):
        segments = word_diff(master[index].text, unit.text)
        return DiffLine(
            LineMark.REWORDED, unit.text, unit.fact_ids, index, unit.unit_id, segments, moved
        )
    mark = LineMark.MOVED if moved else LineMark.UNCHANGED
    return DiffLine(mark, unit.text, unit.fact_ids, index, unit.unit_id, moved=moved)


def _removed_after(
    master: Sequence[MasterLine], matched: set[int]
) -> tuple[list[DiffLine], dict[int, list[DiffLine]]]:
    """The unmatched master lines as REMOVED: those before any matched master line, and for
    each matched master line the ones that follow it."""
    leading: list[DiffLine] = []
    following: dict[int, list[DiffLine]] = defaultdict(list)
    anchor: int | None = None
    for index, line in enumerate(master):
        if index in matched:
            anchor = index
            continue
        removed = DiffLine(LineMark.REMOVED, line.text, (str(line.ref),), index, None)
        (leading if anchor is None else following[anchor]).append(removed)
    return leading, following


def cv_diff(master: MasterCv, tailored: Sequence[DocumentUnit]) -> tuple[DiffLine, ...]:
    """How a tailored CV differs from the master, matching lines by fact number. Only content
    units that are not removed are lines. A line citing several facts is ADDED and its source
    master lines are REMOVED."""
    lines = master.lines
    units = [u for u in tailored if u.kind == "content" and not u.removed]
    matches = _match_lines(lines, units)
    in_order = _lines_in_order(matches)
    leading, following = _removed_after(lines, {i for i in matches if i is not None})
    result = list(leading)
    for pos, (unit, index) in enumerate(zip(units, matches, strict=True)):
        result.append(_tailored_line(unit, lines, index, pos in in_order))
        if index is not None:
            result.extend(following.get(index, ()))
    return tuple(result)
