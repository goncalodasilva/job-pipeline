"""The master CV and the diff of a tailored CV against it (M6.4; AC-M6.3 groundwork;
ADR-106 item 5; detailed design "Diff conventions"): ``app/domain/master_cv.py``. Pure; every
value is fictional.

Assumed interface (chosen here where the sources left it open; the implementer follows it):

- ``RolePeriod``: frozen dataclass ``(start: str | None = None, end: str | None = None)``, the
  partial ISO dates (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``) of a role. ``end=None`` with a
  ``start`` is a current role.
- ``MasterLine``: frozen dataclass ``(ref: FactRef, role: str | None, text: str)``; its fact id
  is ``str(ref)``. ``text`` is the fact's wording as one line: the non-blank parts of
  ``FactContent`` in the order text, situation, action, result, line endings and runs of
  whitespace collapsed to single spaces.
- ``MasterRole``: frozen dataclass ``(role: str | None, start: str | None, end: str | None,
  lines: tuple[MasterLine, ...])``.
- ``MasterCv``: frozen dataclass ``(roles: tuple[MasterRole, ...])`` with the property ``lines``
  (all lines, role by role, in order).
- ``build_master_cv(records: Iterable[FactRecord], role_dates: Mapping[str, RolePeriod] | None
  = None) -> MasterCv``: built from ``facts_for_ai(records)`` only (confirmed, current,
  not never-use; a "generalize" fact is included, since the master CV never leaves the
  machine). Facts are grouped by their role label (the same string as ``AIFact.role``; facts
  without a role form one last group with ``role=None``). Roles are ordered most recent first:
  current roles (a start and no end) first; then by the latest day the end date can mean
  (``partial_dates.latest_day``), later first; ties by the later start (``earliest_day``), then
  by the role label (case-folded). Roles with no dates, or not in ``role_dates``, follow the
  dated ones in label order; the role-less group is last. Lines in a role are in fact-number
  order. A role with no usable fact is not listed.
- ``LineMark`` (``StrEnum``): ``UNCHANGED``, ``ADDED``, ``REMOVED``, ``REWORDED``, ``MOVED`` with
  values ``"unchanged"``, ``"added"``, ``"removed"``, ``"reworded"``, ``"moved"``.
- ``DiffLine``: frozen dataclass ``(mark: LineMark, text: str, fact_ids: tuple[str, ...],
  master_line: int | None, unit_id: str | None, segments: tuple[DiffSegment, ...] = (),
  moved: bool = False)``. ``master_line`` is the index in ``MasterCv.lines`` of the master line
  it stands for (None for ADDED); ``unit_id`` is the tailored unit's (None for REMOVED);
  ``text`` is the tailored text, or the master text for REMOVED; ``segments`` is
  ``app.domain.diff.word_diff(master text, tailored text)`` for REWORDED and empty otherwise;
  ``moved`` is True for MOVED and for a REWORDED line that also moved.
- ``cv_diff(master: MasterCv, tailored: Sequence[DocumentUnit]) -> tuple[DiffLine, ...]``:
    * only tailored ``content`` units that are not ``removed`` are lines; structure units and
      tombstones are ignored (a tombstoned line leaves its master line REMOVED);
    * a tailored line matches the master line whose fact key equals its own (the set of fact
      numbers; versions are ignored), each master line at most once, the first tailored line
      first. A line with no ids, with ids unknown to the master, a repeated citation of a master
      line already matched, or one citing several facts (no single master line has that key) is
      ADDED; the master lines it draws on that nothing else matches are REMOVED;
    * a matched line whose text differs from the master text, after collapsing whitespace, is
      REWORDED (case and punctuation count); otherwise it is MOVED when it is off the longest
      run of matched lines that keep the master's order (ties: exactly the lines needed, none
      more), else UNCHANGED;
    * the result lists the tailored lines in their order; each REMOVED master line follows the
      result line of the nearest earlier master line that has one (not REMOVED), or comes first
      when none does;
    * texts are data: never escaped or changed.
"""

import importlib
from collections.abc import Sequence
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.fact_selection import FactRecord
from app.domain.facts import FactContent, FactRef, FactStatus, Privacy

OPS = "Operations Analyst at Example Widgets Ltd"
ROUTES = "Route Planner at Sample Freight Co"
CLERK = "Junior Clerk at Old Paper Co"
VOLUNTEER = "Volunteer at Lantern Society"
OPS_TEXT = "Cut invoice processing time by 35% across 4 warehouses."
ROUTE_TEXT = "Planned weekly freight routes for 12 drivers."
GUIDE_TEXT = "Wrote onboarding guides for new analysts."


def master_cv() -> ModuleType:
    return importlib.import_module("app.domain.master_cv")


def documents() -> ModuleType:
    return importlib.import_module("app.domain.documents")


def record(
    number: int,
    text: str | None = None,
    *,
    role: str | None = OPS,
    version: int = 1,
    is_current: bool = True,
    status: FactStatus = FactStatus.CONFIRMED,
    privacy: Privacy = Privacy.NORMAL,
    content: FactContent | None = None,
) -> FactRecord:
    return FactRecord(
        number=number,
        version=version,
        is_current=is_current,
        status=status,
        privacy=privacy,
        content=content if content is not None else FactContent(text=text),
        role=role,
    )


def unit(
    unit_id: str,
    text: str,
    fact_ids: tuple[str, ...] = (),
    *,
    kind: str = "content",
    removed: bool = False,
) -> Any:
    module = documents()
    return module.DocumentUnit(
        unit_id=unit_id,
        kind=kind,
        section="experience",
        role=OPS,
        text=text,
        origin=module.UnitOrigin.AI,
        fact_ids=fact_ids,
        removed=removed,
    )


def three_line_master() -> Any:
    return master_cv().build_master_cv(
        [record(1, OPS_TEXT), record(2, ROUTE_TEXT), record(3, GUIDE_TEXT)]
    )


def shape(lines: Sequence[Any]) -> list[tuple[str, str, int | None]]:
    return [(line.mark.value, line.text, line.master_line) for line in lines]


# --- the master CV -----------------------------------------------------------------------------


def test_master_cv_groups_by_role_and_orders_by_date() -> None:
    module = master_cv()
    dates = {
        OPS: module.RolePeriod("2021-03", "2024-05"),
        ROUTES: module.RolePeriod("2024-06", None),
        CLERK: module.RolePeriod("2018", "2021-02"),
    }
    records = [
        record(5, "Filed the archive.", role=CLERK),
        record(2, "Second ops line.", role=OPS),
        record(7, "Planned routes.", role=ROUTES),
        record(1, "First ops line.", role=OPS),
        record(9, "Ran a book swap.", role=VOLUNTEER),
        record(8, "Won a local chess cup.", role=None),
    ]

    master = module.build_master_cv(records, dates)

    assert [r.role for r in master.roles] == [ROUTES, OPS, CLERK, VOLUNTEER, None]
    assert [(r.start, r.end) for r in master.roles] == [
        ("2024-06", None),
        ("2021-03", "2024-05"),
        ("2018", "2021-02"),
        (None, None),
        (None, None),
    ]
    ops = master.roles[1]
    assert [str(line.ref) for line in ops.lines] == ["F1@1", "F2@1"]  # by number, not input
    assert [line.text for line in master.lines] == [
        "Planned routes.",
        "First ops line.",
        "Second ops line.",
        "Filed the archive.",
        "Ran a book swap.",
        "Won a local chess cup.",
    ]
    assert all(line.role == role.role for role in master.roles for line in role.lines)


def test_master_cv_orders_partial_dates_and_ties() -> None:
    module = master_cv()
    dates = {
        "Beta role": module.RolePeriod("2019", "2022"),
        "Alpha role": module.RolePeriod("2019", "2022"),
        "Later start": module.RolePeriod("2020", "2022"),
        "Year only end": module.RolePeriod("2015", "2023"),
        "Month end": module.RolePeriod("2015", "2023-05"),
    }
    records = [record(i, f"Line {i}.", role=role) for i, role in enumerate(dates, start=1)]

    master = module.build_master_cv(records, dates)

    # 2023 can mean December 2023, later than May 2023; equal ends: later start first; then label.
    assert [r.role for r in master.roles] == [
        "Year only end",
        "Month end",
        "Later start",
        "Alpha role",
        "Beta role",
    ]


def test_master_cv_without_role_dates_orders_roles_by_label() -> None:
    module = master_cv()
    records = [record(1, "One.", role="b role"), record(2, "Two.", role="A role")]

    assert [r.role for r in module.build_master_cv(records).roles] == ["A role", "b role"]


def test_master_cv_excludes_never_use_and_drafts() -> None:
    module = master_cv()
    records = [
        record(1, "Confirmed and normal."),
        record(2, "A draft from a CV import.", status=FactStatus.DRAFT),
        record(3, "Discarded.", status=FactStatus.DISCARDED),
        record(4, "Archived.", status=FactStatus.ARCHIVED),
        record(5, "Never use this one.", privacy=Privacy.NEVER_USE),
        record(6, "An older version.", version=1, is_current=False),
        record(6, "The current version.", version=2),
        record(7, "Generalized but usable.", privacy=Privacy.GENERALIZE),
        record(8, "Only a draft in this role.", role=CLERK, status=FactStatus.DRAFT),
        record(9, "Never use, alone in a role.", role=VOLUNTEER, privacy=Privacy.NEVER_USE),
    ]

    master = module.build_master_cv(records)

    assert [str(line.ref) for line in master.lines] == ["F1@1", "F6@2", "F7@1"]
    assert [line.text for line in master.lines] == [
        "Confirmed and normal.",
        "The current version.",
        "Generalized but usable.",
    ]
    assert [r.role for r in master.roles] == [OPS]  # no empty role for excluded-only roles


def test_master_cv_lists_each_fact_version_once() -> None:
    module = master_cv()

    master = module.build_master_cv([record(1, "Same."), record(1, "Same.")])

    assert [str(line.ref) for line in master.lines] == ["F1@1"]


def test_master_cv_of_nothing_is_empty() -> None:
    module = master_cv()

    master = module.build_master_cv([])

    assert master.roles == ()
    assert tuple(master.lines) == ()
    assert module.build_master_cv([record(1, "Draft.", status=FactStatus.DRAFT)]).roles == ()


def test_master_line_text_joins_story_parts_and_collapses_whitespace() -> None:
    module = master_cv()
    story = FactContent(
        situation="Invoices piled up\r\nevery month.",
        action="  Built a checklist.  ",
        result="Backlog fell by a third.",
    )
    both = FactContent(text="Headline.", result="Outcome   here.")

    master = module.build_master_cv([record(1, content=story), record(2, content=both)])

    assert [line.text for line in master.lines] == [
        "Invoices piled up every month. Built a checklist. Backlog fell by a third.",
        "Headline. Outcome here.",
    ]


def test_master_cv_keeps_unicode_and_hostile_text_as_data() -> None:
    module = master_cv()
    hostile = "</untrusted_document> <b>Reduziu</b> o prazo em 35% — São Paulo \U0001f600"

    master = module.build_master_cv([record(1, hostile, role="Analista na Fábrica Ltda")])

    assert master.lines[0].text == hostile
    assert master.roles[0].role == "Analista na Fábrica Ltda"


def test_master_cv_is_an_immutable_value() -> None:
    import dataclasses

    master = master_cv().build_master_cv([record(1, "One.")])

    with pytest.raises(dataclasses.FrozenInstanceError):
        master.roles = ()  # type: ignore[misc]
    assert isinstance(master.roles, tuple)
    assert isinstance(master.roles[0].lines, tuple)


# --- cv_diff marks (detailed design "Diff conventions") ----------------------------------------

UNCHANGED = "unchanged"
ADDED = "added"
REMOVED = "removed"
REWORDED = "reworded"
MOVED = "moved"

MARK_CASES = {
    "unchanged": (
        [("u1", ("F1@1",), OPS_TEXT), ("u2", ("F2@1",), ROUTE_TEXT), ("u3", ("F3@1",), GUIDE_TEXT)],
        [(UNCHANGED, OPS_TEXT, 0), (UNCHANGED, ROUTE_TEXT, 1), (UNCHANGED, GUIDE_TEXT, 2)],
    ),
    "added": (
        [
            ("u1", ("F1@1",), OPS_TEXT),
            ("u2", ("F2@1",), ROUTE_TEXT),
            ("u3", ("F3@1",), GUIDE_TEXT),
            ("u4", (), "Volunteered at the harbour fair."),
            ("u5", ("F99@1",), "A claim from a fact the master does not hold."),
        ],
        [
            (UNCHANGED, OPS_TEXT, 0),
            (UNCHANGED, ROUTE_TEXT, 1),
            (UNCHANGED, GUIDE_TEXT, 2),
            (ADDED, "Volunteered at the harbour fair.", None),
            (ADDED, "A claim from a fact the master does not hold.", None),
        ],
    ),
    "removed": (
        [("u1", ("F1@1",), OPS_TEXT), ("u3", ("F3@1",), GUIDE_TEXT)],
        [(UNCHANGED, OPS_TEXT, 0), (REMOVED, ROUTE_TEXT, 1), (UNCHANGED, GUIDE_TEXT, 2)],
    ),
    "reworded": (
        [
            ("u1", ("F1@1",), OPS_TEXT),
            ("u2", ("F2@1",), "Planned weekly freight routes for 12 van drivers."),
            ("u3", ("F3@1",), GUIDE_TEXT),
        ],
        [
            (UNCHANGED, OPS_TEXT, 0),
            (REWORDED, "Planned weekly freight routes for 12 van drivers.", 1),
            (UNCHANGED, GUIDE_TEXT, 2),
        ],
    ),
    "moved": (
        [("u3", ("F3@1",), GUIDE_TEXT), ("u1", ("F1@1",), OPS_TEXT), ("u2", ("F2@1",), ROUTE_TEXT)],
        [(MOVED, GUIDE_TEXT, 2), (UNCHANGED, OPS_TEXT, 0), (UNCHANGED, ROUTE_TEXT, 1)],
    ),
}


@pytest.mark.parametrize(("tailored", "expected"), MARK_CASES.values(), ids=list(MARK_CASES))
def test_cv_diff_marks(
    tailored: list[tuple[str, tuple[str, ...], str]],
    expected: list[tuple[str, str, int | None]],
) -> None:
    units = [unit(uid, text, ids) for uid, ids, text in tailored]

    result = master_cv().cv_diff(three_line_master(), units)

    assert shape(result) == expected
    assert isinstance(result, tuple)


def test_cv_diff_reworded_line_carries_the_word_diff() -> None:
    from app.domain.diff import word_diff

    new_text = "Planned weekly freight routes for 12 van drivers."

    result = master_cv().cv_diff(three_line_master(), [unit("u2", new_text, ("F2@1",))])

    reworded = next(line for line in result if line.mark.value == REWORDED)
    assert reworded.segments == word_diff(ROUTE_TEXT, new_text)
    assert [s.text.strip() for s in reworded.segments if s.op.value == "added"] == ["van"]
    assert reworded.fact_ids == ("F2@1",)
    assert reworded.unit_id == "u2"
    assert reworded.moved is False


def test_cv_diff_lines_carry_their_ids_and_sources() -> None:
    result = master_cv().cv_diff(
        three_line_master(),
        [unit("u1", OPS_TEXT, ("F1@1",)), unit("u9", "Typed.", ())],
    )

    kept, removed_two, removed_three, typed = result
    assert (kept.fact_ids, kept.unit_id, kept.moved, kept.segments) == (("F1@1",), "u1", False, ())
    assert (typed.mark.value, typed.fact_ids, typed.unit_id, typed.master_line) == (
        ADDED,
        (),
        "u9",
        None,
    )
    assert typed.segments == ()
    assert [
        (r.mark.value, r.unit_id, r.fact_ids, r.master_line) for r in (removed_two, removed_three)
    ] == [
        (REMOVED, None, ("F2@1",), 1),
        (REMOVED, None, ("F3@1",), 2),
    ]


def test_cv_diff_reworded_and_moved_line_is_marked_reworded_and_flagged_moved() -> None:
    reworded_guide = "Wrote onboarding guides for new analysts and drivers."
    units = [
        unit("u3", reworded_guide, ("F3@1",)),
        unit("u1", OPS_TEXT, ("F1@1",)),
        unit("u2", ROUTE_TEXT, ("F2@1",)),
    ]

    result = master_cv().cv_diff(three_line_master(), units)

    assert shape(result) == [
        (REWORDED, reworded_guide, 2),
        (UNCHANGED, OPS_TEXT, 0),
        (UNCHANGED, ROUTE_TEXT, 1),
    ]
    assert [line.moved for line in result] == [True, False, False]


def test_cv_diff_swapping_two_lines_moves_exactly_one() -> None:
    units = [unit("u2", ROUTE_TEXT, ("F2@1",)), unit("u1", OPS_TEXT, ("F1@1",))]
    master = master_cv().build_master_cv([record(1, OPS_TEXT), record(2, ROUTE_TEXT)])

    result = master_cv().cv_diff(master, units)

    assert sorted(line.mark.value for line in result) == [MOVED, UNCHANGED]


def test_cv_diff_added_lines_between_others_do_not_count_as_moves() -> None:
    units = [
        unit("u1", OPS_TEXT, ("F1@1",)),
        unit("u8", "Typed in the middle.", ()),
        unit("u2", ROUTE_TEXT, ("F2@1",)),
        unit("u3", GUIDE_TEXT, ("F3@1",)),
    ]

    result = master_cv().cv_diff(three_line_master(), units)

    assert [line.mark.value for line in result] == [UNCHANGED, ADDED, UNCHANGED, UNCHANGED]


def test_cv_diff_removed_lines_follow_the_nearest_earlier_master_line() -> None:
    master = master_cv().build_master_cv([record(n, f"Line {n}.") for n in range(1, 6)])
    units = [unit("u1", "Line 1.", ("F1@1",)), unit("u4", "Line 4.", ("F4@1",))]

    result = master_cv().cv_diff(master, units)

    assert shape(result) == [
        (UNCHANGED, "Line 1.", 0),
        (REMOVED, "Line 2.", 1),
        (REMOVED, "Line 3.", 2),
        (UNCHANGED, "Line 4.", 3),
        (REMOVED, "Line 5.", 4),
    ]
    first_gone = master_cv().cv_diff(master, [unit("u4", "Line 4.", ("F4@1",))])
    assert [line.master_line for line in first_gone] == [0, 1, 2, 3, 4]
    assert [line.mark.value for line in first_gone] == [REMOVED] * 3 + [UNCHANGED, REMOVED]


def test_cv_diff_matches_by_fact_number_and_ignores_versions() -> None:
    master = master_cv().build_master_cv([record(1, OPS_TEXT, version=3)])

    result = master_cv().cv_diff(master, [unit("u1", OPS_TEXT, ("F1@1",))])

    assert shape(result) == [(UNCHANGED, OPS_TEXT, 0)]


def test_cv_diff_a_repeated_citation_matches_once() -> None:
    master = master_cv().build_master_cv([record(1, OPS_TEXT)])
    units = [unit("u1", OPS_TEXT, ("F1@1",)), unit("u2", "Says it again.", ("F1@1",))]

    result = master_cv().cv_diff(master, units)

    assert shape(result) == [(UNCHANGED, OPS_TEXT, 0), (ADDED, "Says it again.", None)]


def test_cv_diff_a_line_citing_several_facts_is_added_and_its_sources_are_removed() -> None:
    master = master_cv().build_master_cv(
        [record(1, "One."), record(2, "Two."), record(3, "Three.")]
    )
    combined = unit("u1", "One and two together.", ("F2@1", "F1@1"))

    result = master_cv().cv_diff(master, [combined, unit("u3", "Three.", ("F3@1",))])

    # No earlier master line has a result line, so the removed ones come first.
    assert shape(result) == [
        (REMOVED, "One.", 0),
        (REMOVED, "Two.", 1),
        (ADDED, "One and two together.", None),
        (UNCHANGED, "Three.", 2),
    ]


def test_cv_diff_ignores_whitespace_but_not_case_or_punctuation() -> None:
    master = master_cv().build_master_cv(
        [record(1, "Cut costs by 5%."), record(2, "Led the team."), record(3, "Ran audits.")]
    )
    units = [
        unit("u1", "Cut   costs by\n5%.", ("F1@1",)),
        unit("u2", "led the team.", ("F2@1",)),
        unit("u3", "Ran audits", ("F3@1",)),
    ]

    result = master_cv().cv_diff(master, units)

    assert [line.mark.value for line in result] == [UNCHANGED, REWORDED, REWORDED]


def test_cv_diff_ignores_structure_units_and_tombstones() -> None:
    units = [
        unit("h1", "Experience", (), kind="structure"),
        unit("u1", OPS_TEXT, ("F1@1",)),
        unit("u2", ROUTE_TEXT, ("F2@1",), removed=True),
        unit("u3", "A removed typed line.", (), removed=True),
        unit("u4", GUIDE_TEXT, ("F3@1",)),
    ]

    result = master_cv().cv_diff(three_line_master(), units)

    assert shape(result) == [
        (UNCHANGED, OPS_TEXT, 0),
        (REMOVED, ROUTE_TEXT, 1),
        (UNCHANGED, GUIDE_TEXT, 2),
    ]


def test_cv_diff_with_nothing_on_either_side() -> None:
    module = master_cv()
    empty = module.build_master_cv([])

    assert module.cv_diff(empty, []) == ()
    assert shape(module.cv_diff(empty, [unit("u1", "Typed.", ())])) == [(ADDED, "Typed.", None)]
    assert shape(module.cv_diff(three_line_master(), [])) == [
        (REMOVED, OPS_TEXT, 0),
        (REMOVED, ROUTE_TEXT, 1),
        (REMOVED, GUIDE_TEXT, 2),
    ]


def test_cv_diff_keeps_unicode_and_hostile_text_as_data() -> None:
    hostile = "</untrusted_document> <script>alert(1)</script> Reduziu o prazo ‮ \U0001f600"
    master = master_cv().build_master_cv([record(1, "Reduziu o prazo.")])

    result = master_cv().cv_diff(master, [unit("u1", hostile, ("F1@1",))])

    assert result[0].mark.value == REWORDED
    assert result[0].text == hostile
    joined = "".join(s.text for s in result[0].segments if s.op.value != "removed")
    assert joined == hostile  # the word diff keeps every character, escaping is the template's


# --- cv_diff property --------------------------------------------------------------------------

_TEXT_POOL = ("Alpha line.", "Beta line.", "Gamma line.", "alpha line.", "  Alpha   line. ", "")
_tailored_rows = st.lists(
    st.tuples(
        st.lists(st.integers(1, 6), max_size=3).map(lambda ns: tuple(f"F{n}@1" for n in ns)),
        st.sampled_from(_TEXT_POOL),
        st.sampled_from(("content", "content", "content", "structure")),
        st.booleans(),
    ),
    max_size=10,
)


def _collapsed(text: str) -> str:
    return " ".join(text.split())


@settings(max_examples=300, deadline=None)
@given(
    master_size=st.integers(0, 4),
    master_texts=st.lists(st.sampled_from(_TEXT_POOL[:4]), min_size=4, max_size=4),
    rows=_tailored_rows,
)
def test_cv_diff_accounts_for_every_master_line_once(
    master_size: int, master_texts: list[str], rows: list[tuple[tuple[str, ...], str, str, bool]]
) -> None:
    module = master_cv()
    lines = tuple(
        module.MasterLine(ref=FactRef(n, 1), role=OPS, text=master_texts[n - 1])
        for n in range(1, master_size + 1)
    )
    master = module.MasterCv(roles=(module.MasterRole(OPS, None, None, lines),) if lines else ())
    units = [
        unit(f"u{i}", text, ids, kind=kind, removed=removed)
        for i, (ids, text, kind, removed) in enumerate(rows)
    ]
    eligible = [u for u in units if u.kind == "content" and not u.removed]

    result = module.cv_diff(master, units)

    # Every master line stands for exactly one result line.
    assert sorted(line.master_line for line in result if line.master_line is not None) == list(
        range(len(lines))
    )
    # Every tailored line is shown once, in its order; the rest are removed master lines.
    shown = [line for line in result if line.unit_id is not None]
    assert [line.unit_id for line in shown] == [u.unit_id for u in eligible]
    assert len(result) == len(shown) + sum(1 for line in result if line.mark.value == REMOVED)
    for line in result:
        mark = line.mark.value
        if mark == ADDED:
            assert line.master_line is None
        elif mark == REMOVED:
            assert line.unit_id is None and line.master_line is not None
            assert line.text == lines[line.master_line].text
        else:
            assert line.master_line is not None
            master_line = lines[line.master_line]
            assert line.fact_ids is not None
            assert {id_.split("@")[0] for id_ in line.fact_ids} == {f"F{master_line.ref.number}"}
            same = _collapsed(line.text) == _collapsed(master_line.text)
            assert same == (mark != REWORDED)
            assert line.moved or mark != MOVED
