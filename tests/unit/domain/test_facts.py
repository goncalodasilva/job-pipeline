"""Fact lifecycle, version numbers and fact references (M3.2; groundwork for AC-M3.2, AC-M3.3;
spec 3.1 item 1, 3.2; ADR-089 item 11).

``app.domain.facts`` is pure: the enumerations 0003's CHECK constraints accept, the only status
changes a fact may make (Draft to Confirmed, Draft to Discarded with undo back to Draft,
Confirmed to Archived and back), ``next_version`` (an edit that changes the content gets the
next version number; an identical edit makes no version) and ``FactRef``, the ``F12@3`` form a
fact version is cited by in AI requests and answers.

Imports are inside the tests so each one fails on its own while the module is missing. All
wording is fictional.
"""

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

STATUSES = ("draft", "confirmed", "discarded", "archived")

# "F12@3" with full-width digits, which str.isdigit() and \d accept.
FULL_WIDTH_REF = "F" + chr(0xFF11) + chr(0xFF12) + "@3"

# Everything not listed is refused, including a status to itself: editing a confirmed fact adds
# a version and is not a status change.
ALLOWED = {
    ("draft", "confirmed"),  # user confirms
    ("draft", "discarded"),  # discard, or the draft was merged into another fact
    ("discarded", "draft"),  # undo
    ("confirmed", "archived"),
    ("archived", "confirmed"),  # restore
}

TRANSITION_TABLE = [
    pytest.param(
        source,
        target,
        (source, target) in ALLOWED,
        id=f"{source}->{target}:{'allowed' if (source, target) in ALLOWED else 'refused'}",
    )
    for source in STATUSES
    for target in STATUSES
]


# --- enumerations ---------------------------------------------------------------


def test_enumerations_match_the_schema() -> None:
    from app.domain.facts import FactStatus, FactType, Privacy, RoleStatus

    # The values are the strings 0003's CHECK constraints accept, stored as plain text.
    assert {s.value for s in FactStatus} == set(STATUSES)
    assert {t.value for t in FactType} == {"fact", "story"}
    assert {p.value for p in Privacy} == {"normal", "never_use", "generalize"}
    assert {r.value for r in RoleStatus} == {"draft", "confirmed"}
    assert FactStatus("draft") == "draft"
    assert Privacy("never_use") == "never_use"
    for enumeration, unknown in (
        (FactStatus, "approved"),
        (FactType, "note"),
        (Privacy, "never use"),
        (RoleStatus, "archived"),
    ):
        with pytest.raises(ValueError):
            enumeration(unknown)


# --- lifecycle --------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target", "allowed"), TRANSITION_TABLE)
def test_lifecycle_transitions(source: str, target: str, allowed: bool) -> None:
    from app.domain.facts import FactStatus, can_transition

    assert can_transition(FactStatus(source), FactStatus(target)) is allowed


def test_lifecycle_table_is_exactly_the_allowed_set_and_immutable() -> None:
    from app.domain.facts import ALLOWED_TRANSITIONS

    declared = {
        (source.value, target.value)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert declared == ALLOWED
    for targets in ALLOWED_TRANSITIONS.values():
        assert isinstance(targets, frozenset)
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS["draft"] = frozenset()  # type: ignore[index]


def test_nothing_ever_becomes_confirmed_except_by_confirm_or_restore() -> None:
    # AC-M3.1 groundwork: the only ways into Confirmed are from Draft (the user confirms) and
    # from Archived (the user restores something they had confirmed before).
    from app.domain.facts import FactStatus, can_transition

    into_confirmed = {s.value for s in FactStatus if can_transition(s, FactStatus.CONFIRMED)}
    assert into_confirmed == {"draft", "archived"}
    # A discarded fact can only come back as a draft, never straight to confirmed or archived.
    out_of_discarded = {s.value for s in FactStatus if can_transition(FactStatus.DISCARDED, s)}
    assert out_of_discarded == {"draft"}


# --- versions ---------------------------------------------------------------------


def test_fact_content_is_an_immutable_value() -> None:
    from app.domain.facts import FactContent

    content = FactContent(text="Cut the invoice backlog by a third")
    assert (content.text, content.situation, content.action, content.result) == (
        "Cut the invoice backlog by a third",
        None,
        None,
        None,
    )
    assert content == FactContent(text="Cut the invoice backlog by a third")
    with pytest.raises(dataclasses.FrozenInstanceError):
        content.text = "Cut the invoice backlog by half"  # type: ignore[misc]


@pytest.mark.parametrize(
    "edited",
    [
        {"text": "Cut the invoice backlog by half"},
        {"text": "cut the invoice backlog by a third"},  # case only
        {"text": "Cut the invoice backlog by a third."},  # punctuation only
        {"text": "Cut the  invoice backlog by a third"},  # spacing inside the text
        {"text": "Cut the invoice backlog by a third", "result": "Month-end closed on time"},
        {"text": None, "situation": "Cut the invoice backlog by a third"},  # moved to a part
    ],
    ids=["wording", "case", "punctuation", "inner-space", "part-added", "part-moved"],
)
def test_edit_always_yields_next_version_number(edited: dict[str, str | None]) -> None:
    from app.domain.facts import FactContent, next_version

    current = FactContent(text="Cut the invoice backlog by a third")
    assert next_version(1, current, FactContent(**edited)) == 2
    assert next_version(7, current, FactContent(**edited)) == 8


def test_edit_of_each_story_part_yields_next_version_number() -> None:
    from app.domain.facts import FactContent, next_version

    current = FactContent(
        situation="The depot missed its lorry window twice weekly",
        action="Rewrote the loading timetable",
        result="Punctuality held for two quarters",
    )
    for part in ("text", "situation", "action", "result"):
        edited = dataclasses.replace(current, **{part: "Something else entirely"})
        assert next_version(3, current, edited) == 4, part
        removed = dataclasses.replace(current, **{part: None})
        expected = None if part == "text" else 4
        assert next_version(3, current, removed) == expected, part


@pytest.mark.parametrize(
    ("current", "edited"),
    [
        ({"text": "Trained four new cashiers"}, {"text": "Trained four new cashiers"}),
        # A form posts "" for an empty field and may add surrounding whitespace.
        (
            {"text": "Trained four new cashiers"},
            {"text": "Trained four new cashiers", "result": ""},
        ),
        ({"text": "Trained four new cashiers"}, {"text": "  Trained four new cashiers\n"}),
        (
            {"situation": "A", "action": "B", "result": "C"},
            {"text": "  ", "situation": "A ", "action": "\tB", "result": "C"},
        ),
    ],
    ids=["same", "blank-part-is-none", "outer-whitespace", "story"],
)
def test_identical_edit_makes_no_version(
    current: dict[str, str | None], edited: dict[str, str | None]
) -> None:
    from app.domain.facts import FactContent, next_version

    assert next_version(1, FactContent(**current), FactContent(**edited)) is None
    assert next_version(12, FactContent(**current), FactContent(**edited)) is None


@pytest.mark.parametrize(
    ("current", "edited"),
    [
        (
            {"text": "Trained four new cashiers\nand two supervisors"},
            {"text": "Trained four new cashiers\r\nand two supervisors"},
        ),
        (
            {"text": "Trained four new cashiers\nand two supervisors"},
            {"text": "Trained four new cashiers\rand two supervisors"},
        ),
        (
            {"situation": "A\n\nB", "action": "C\nD", "result": "E"},
            {"text": "\r\n", "situation": "A\r\n\r\nB\r\n", "action": "C\rD", "result": "E\r"},
        ),
        (
            {"text": "Trained four new cashiers\r\nand two supervisors"},
            {"text": "Trained four new cashiers\nand two supervisors"},
        ),
    ],
    ids=["crlf", "lone-cr", "story-crlf", "stored-crlf"],
)
def test_edit_that_differs_only_in_line_endings_makes_no_version(
    current: dict[str, str | None], edited: dict[str, str | None]
) -> None:
    # A browser posts a textarea's line breaks as CRLF, whatever was stored.
    from app.domain.facts import FactContent, next_version

    assert next_version(1, FactContent(**current), FactContent(**edited)) is None
    assert next_version(12, FactContent(**current), FactContent(**edited)) is None


def test_line_break_added_or_removed_is_still_an_edit() -> None:
    from app.domain.facts import FactContent, next_version

    current = FactContent(text="Trained four new cashiers\nand two supervisors")
    for edited in (
        "Trained four new cashiers and two supervisors",
        "Trained four new cashiers\r\n\r\nand two supervisors",
    ):
        assert next_version(1, current, FactContent(text=edited)) == 2, edited


@pytest.mark.parametrize(
    ("given_text", "cleaned"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (" \r\n\t", None),
        ("Trained four new cashiers", "Trained four new cashiers"),
        ("  Trained four new cashiers\r\n", "Trained four new cashiers"),
        ("One\r\nTwo\rThree\nFour", "One\nTwo\nThree\nFour"),
        ("One\r\n\r\nTwo", "One\n\nTwo"),
        ("One  two", "One  two"),
    ],
)
def test_clean_part_gives_what_is_stored(given_text: str | None, cleaned: str | None) -> None:
    # Services store parts through clean_part, so what is stored is what is compared.
    from app.domain.facts import clean_part

    assert clean_part(given_text) == cleaned


@given(text=st.none() | st.text(max_size=40))
def test_clean_part_is_idempotent_and_never_blank(text: str | None) -> None:
    from app.domain.facts import FactContent, clean_part, next_version

    once = clean_part(text)
    assert clean_part(once) == once
    assert once is None or (once == once.strip() and once != "" and "\r" not in once)
    # Cleaning a part never counts as an edit.
    assert next_version(1, FactContent(situation=text), FactContent(situation=once)) is None


@given(
    number=st.integers(min_value=1, max_value=10_000),
    parts=st.tuples(*(st.none() | st.text(max_size=30) for _ in range(4))),
)
def test_saving_the_same_content_never_makes_a_version(
    number: int, parts: tuple[str | None, str | None, str | None, str | None]
) -> None:
    from app.domain.facts import FactContent, next_version

    content = FactContent(*parts)
    assert next_version(number, content, content) is None
    assert next_version(number, content, FactContent(*parts)) is None


@pytest.mark.parametrize("current", [0, -1])
def test_next_version_refuses_a_version_number_below_one(current: int) -> None:
    from app.domain.facts import FactContent, next_version

    with pytest.raises(ValueError):
        next_version(current, FactContent(text="A"), FactContent(text="B"))


# --- fact references ----------------------------------------------------------------


def test_fact_ref_format() -> None:
    from app.domain.facts import FactRef

    # Spec 3.1 item 1: fact number 12 at version 3 is cited as F12@3.
    ref = FactRef(number=12, version=3)
    assert str(ref) == "F12@3"
    assert f"{ref}" == "F12@3"
    assert str(FactRef(1, 1)) == "F1@1"
    assert str(FactRef(1045, 27)) == "F1045@27"
    assert ref == FactRef(12, 3)
    assert ref != FactRef(12, 4)
    assert len({ref, FactRef(12, 3)}) == 1  # usable in the set of ids sent to the model
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.version = 4  # type: ignore[misc]


@pytest.mark.parametrize(("number", "version"), [(0, 1), (1, 0), (-3, 1), (2, -1)])
def test_fact_ref_refuses_numbers_below_one(number: int, version: int) -> None:
    from app.domain.facts import FactRef

    with pytest.raises(ValueError):
        FactRef(number, version)


@pytest.mark.parametrize(
    ("number", "version"),
    [
        (True, 1),
        (1, True),
        (True, True),
        (1.5, 1),
        (1, 2.0),
        (12.0, 3),
        ("12", 3),
        (12, "3"),
        (None, 1),
        (1, None),
    ],
)
def test_fact_ref_refuses_anything_but_whole_numbers(number: object, version: object) -> None:
    # F1.5@1 or FTrue@1 must never be built: a reference has exactly one spelling.
    from app.domain.facts import FactRef

    with pytest.raises(ValueError):
        FactRef(number, version)  # type: ignore[arg-type]


def test_fact_ref_parses_its_own_format() -> None:
    from app.domain.facts import FactRef

    assert FactRef.parse("F12@3") == FactRef(12, 3)
    assert FactRef.parse("F1@1") == FactRef(1, 1)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "F12",
        "12@3",
        "F@3",
        "F12@",
        "f12@3",
        "F12@3 ",
        " F12@3",
        "F12@3\n",
        "F12@3@4",
        "F12@v3",
        "F-1@2",
        "F1.5@2",
        "F0@1",
        "F1@0",
        "F012@3",
        "F12@03",
        FULL_WIDTH_REF,
        "F12 @ 3",
        "F12@3, F13@1",
    ],
)
def test_fact_ref_parse_refuses_anything_else(text: str) -> None:
    # The honesty check compares cited ids with the ids that were sent (spec 3.1 item 3), so
    # only the exact canonical form counts: a near miss is an unknown id, never a guess.
    from app.domain.facts import FactRef

    with pytest.raises(ValueError):
        FactRef.parse(text)


@given(
    number=st.integers(min_value=1, max_value=10**9),
    version=st.integers(min_value=1, max_value=10**6),
)
def test_fact_ref_round_trips(number: int, version: int) -> None:
    from app.domain.facts import FactRef

    ref = FactRef(number, version)
    assert FactRef.parse(str(ref)) == ref
    assert str(ref) == f"F{number}@{version}"
