"""Pure resolution of a narrative card (M8.2 part B; spec 3.1 items 1 to 6, 3.6; ADR-124 items 7
and 8). Fictional data only.

Assumed interface (chosen here where the sources left it open; the implementer matches it).
``app/domain/narrative.py`` (pure Python, no I/O, clock or ids of its own):

- ``CARD_KINDS``: ``("tell_me_about_yourself", "why_this_role", "why_this_company",
  "why_leaving", "strengths", "weaknesses")``, in that order. ``REUSABLE_KINDS``: the four of
  them that are not about one company or role (``tell_me_about_yourself``, ``why_leaving``,
  ``strengths``, ``weaknesses``) as a ``frozenset``.
- ``MAX_NARRATIVE_UNITS == 6``, ``MAX_NARRATIVE_CHARS == 1200`` (the total length of the kept
  units' text) and ``NARRATIVE_SECTION == "answer"``.
- ``resolve_narrative_units(output, sent_facts, *, name_sources=(), known_skills=(),
  new_id=None) -> TailoredCv`` (``app.domain.tailored_cv``), mirroring
  ``resolve_letter_paragraphs``: ``output`` is the model's answer (``{"units": [{"text",
  "fact_ids", "added_details"}]}``), ``sent_facts`` the ``AIFact`` of the request,
  ``name_sources`` the job's title and company (they may support a capitalised name and nothing
  else), ``new_id`` makes the id of each kept unit (called once per kept unit, in order).
  Kept units are ``DocumentUnit`` rows: kind ``content``, section ``answer``, no role, origin
  ``ai``. Flags are those of ``check_units``, so a claim of the card that no cited fact states
  (an employer, a number, a year, a skill, an added detail) is ``needs_check``, a unit that cites
  nothing or an id that was not sent is ``unsupported``. Nothing here approves anything.
- A unit is dropped, and listed in ``dropped`` (``DroppedUnit``) and never silently lost, when:
  it is beyond the sixth (``too_many``); it would take the total over ``MAX_NARRATIVE_CHARS``
  (``too_long``); or its text is blank, over 1,200 characters, holds a control or bidi
  character, or its ids or details are not lists of text or a detail is over 200 characters
  (``invalid``). It never raises, whatever shape the output has.
- Invisible characters (zero-width and tag characters) are removed from the text and the
  details; duplicate and blank details are dropped.
"""

import ast
import importlib
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.documents import UnitOrigin
from tests.tailoring_ai_inputs import CV_SENT, SENT

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ai" / "draft_narrative"
DOMAIN = Path(__file__).resolve().parents[3] / "app" / "domain"
NAMES = ("Route Planner", "Harbor Kite Fictional")
P1 = "I planned 60 delivery routes a week with a team of 3."
P2 = "I also cut invoice processing time by 35% across 4 warehouses."


def narrative() -> Any:
    return importlib.import_module("app.domain.narrative")


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())["data"]
    return loaded


def counter() -> Callable[[], str]:
    numbers = itertools.count(1)
    return lambda: f"unit-{next(numbers)}"


def resolve(output: Any, sent: Any = CV_SENT, **options: Any) -> Any:
    options.setdefault("new_id", counter())
    return narrative().resolve_narrative_units(output, sent, **options)


def triples(result: Any) -> list[tuple[str, str, str]]:
    return [(flag.kind, flag.claim, flag.unit_id) for flag in result.flags]


def unit(text: str, ids: tuple[str, ...] = ("F1@1",), details: tuple[str, ...] = ()) -> dict:
    return {"text": text, "fact_ids": list(ids), "added_details": list(details)}


# --- constants ---------------------------------------------------------------------------------


def test_the_card_kinds_and_the_kinds_that_may_be_reused() -> None:
    module = narrative()

    assert module.CARD_KINDS == (
        "tell_me_about_yourself",
        "why_this_role",
        "why_this_company",
        "why_leaving",
        "strengths",
        "weaknesses",
    )
    assert (
        frozenset({"tell_me_about_yourself", "why_leaving", "strengths", "weaknesses"})
        == module.REUSABLE_KINDS
    )
    assert module.MAX_NARRATIVE_UNITS == 6 and module.MAX_NARRATIVE_CHARS == 1200
    assert module.NARRATIVE_SECTION == "answer"


@pytest.mark.parametrize("name", ["narrative", "salary_card"])
def test_the_narrative_modules_stay_pure(name: str) -> None:
    path = DOMAIN / f"{name}.py"
    assert path.is_file(), f"app/domain/{name}.py is missing"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    forbidden = {
        "os",
        "pathlib",
        "datetime",
        "uuid",
        "random",
        "sqlite3",
        "fastapi",
        "starlette",
        "httpx",
        "jinja2",
    }
    assert not {i for i in imported if i.split(".")[0] in forbidden}, imported
    assert not {i for i in imported if i.startswith(("app.modules", "app.services", "app.api"))}


# --- what is kept ------------------------------------------------------------------------------


def test_a_clean_answer_keeps_every_unit_as_content_of_the_answer_section() -> None:
    result = resolve(fixture("ok"))

    assert [u.text for u in result.units] == [P1, P2]
    assert [u.unit_id for u in result.units] == ["unit-1", "unit-2"]
    for kept in result.units:
        assert (kept.kind, kept.section, kept.role) == ("content", "answer", None)
        assert kept.origin is UnitOrigin.AI and kept.removed is False
    assert [u.fact_ids for u in result.units] == [("F1@1",), ("F2@1",)]
    assert result.flags == () and result.dropped == ()


def test_the_default_ids_are_distinct_without_a_factory() -> None:
    result = narrative().resolve_narrative_units(fixture("ok"), CV_SENT)

    ids = [u.unit_id for u in result.units]
    assert len(ids) == len(set(ids)) == 2 and all(ids)


def test_an_empty_answer_is_an_empty_card_not_an_error() -> None:
    result = resolve(fixture("empty"))

    assert result.units == () and result.flags == () and result.dropped == ()


# --- honesty: the same checks as every other generated text -------------------------------------

EXPECTED = {
    "adversarial_invented_employer": [("needs_check", "Globex Corporation", "unit-2")],
    "adversarial_unknown_id": [("unsupported", "F999@1", "unit-2")],
    "adversarial_inflated_number": [
        ("needs_check", "85%", "unit-2"),
        ("needs_check", "14", "unit-2"),
    ],
    "adversarial_uncited_paragraph": [
        (
            "unsupported",
            "A seasoned leader known for turning around struggling teams.",
            "unit-2",
        )
    ],
    "adversarial_added_details": [("needs_check", "ran the depot's night shift", "unit-1")],
}


@pytest.mark.parametrize("name", list(EXPECTED))
def test_each_adversarial_answer_is_flagged_on_the_unit_that_carries_it(name: str) -> None:
    result = resolve(fixture(name), name_sources=NAMES)

    assert triples(result) == EXPECTED[name]
    assert len(result.units) == len(fixture(name)["units"]), "flagged units are kept, not hidden"


def test_the_job_title_and_company_support_a_name_and_nothing_else() -> None:
    answer = fixture("company_and_title")

    assert resolve(answer, name_sources=NAMES).flags == ()
    without = resolve(answer)
    assert {(f.kind, f.claim) for f in without.flags} == {
        ("needs_check", "Route Planner"),
        ("needs_check", "Harbor Kite Fictional"),
    }, "only the title and company the caller names are accepted"


def test_company_claims_that_no_cited_fact_states_are_flagged() -> None:
    result = resolve(fixture("adversarial_company_claims"), name_sources=NAMES)

    assert {(f.kind, f.claim, f.unit_id) for f in result.flags} == {
        ("needs_check", "7", "unit-2"),
        ("needs_check", "2024", "unit-2"),
        ("needs_check", "Quillmark Harbor Authority", "unit-2"),
    }
    assert not [f for f in result.flags if f.unit_id == "unit-1"], "the first unit is clean"
    assert [u.unit_id for u in result.units] == ["unit-1", "unit-2"]


def test_a_known_skill_the_cited_facts_do_not_state_is_flagged() -> None:
    answer = {"units": [unit("I planned 60 delivery routes a week using spreadsheets.")]}

    assert resolve(answer, SENT).flags == ()
    flagged = resolve(answer, SENT, known_skills=("spreadsheets",))
    assert triples(flagged) == [("needs_check", "spreadsheets", "unit-1")]


def test_the_obeyed_injection_is_flagged_and_changes_nothing_else() -> None:
    result = resolve(fixture("injection_obeyed"), name_sources=NAMES)

    flagged = {(f.kind, f.claim) for f in result.flags}
    assert ("unsupported", "F4@1") in flagged, "the never-use fact was not sent"
    assert ("needs_check", "Globex Corporation") in flagged
    assert all(u.origin is UnitOrigin.AI for u in result.units)
    assert all(f.unit_id == "unit-2" for f in result.flags)


# --- what code drops ---------------------------------------------------------------------------


def test_units_beyond_the_sixth_are_dropped_and_listed() -> None:
    result = resolve(fixture("too_many_units"))

    assert len(result.units) == 6
    (dropped,) = result.dropped
    assert (dropped.index, dropped.reason, dropped.text) == (6, "too_many", "I planned 7 routes.")


def test_the_total_length_is_limited_and_a_unit_that_would_pass_it_is_dropped() -> None:
    exact = {"units": [unit("w" * 600, ()), unit("x" * 600, ())]}
    over = {"units": [unit("w" * 600, ()), unit("x" * 601, ())]}

    kept = resolve(exact)
    assert [len(u.text) for u in kept.units] == [600, 600] and kept.dropped == ()
    refused = resolve(over)
    assert [len(u.text) for u in refused.units] == [600]
    (dropped,) = refused.dropped
    assert (dropped.index, dropped.reason) == (1, "too_long")
    assert sum(len(u.text) for u in refused.units) <= 1200


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "invalid"),
        ("   ", "invalid"),
        ("x" * 1201, "invalid"),
        ("two\nlines of text", "invalid"),
        ("bidi ‮ text", "invalid"),
        ("nul \x00 text", "invalid"),
    ],
)
def test_a_unit_with_unusable_text_is_dropped_as_invalid(text: str, reason: str) -> None:
    result = resolve({"units": [unit(P1), unit(text), unit(P2, ("F2@1",))]})

    assert [u.text for u in result.units] == [P1, P2]
    (dropped,) = result.dropped
    assert (dropped.index, dropped.reason) == (1, reason)
    assert [u.unit_id for u in result.units] == ["unit-1", "unit-2"], "ids only for kept units"


@pytest.mark.parametrize(
    "broken",
    [
        {"text": P1, "fact_ids": "F1@1", "added_details": []},
        {"text": P1, "fact_ids": [1], "added_details": []},
        {"text": P1, "fact_ids": ["F1@1"], "added_details": "a thing"},
        {"text": P1, "fact_ids": ["F1@1"], "added_details": [3]},
        {"text": P1, "fact_ids": ["F1@1"], "added_details": ["x" * 201]},
        {"text": P1, "fact_ids": ["F1@1"], "added_details": ["two\nlines"]},
        {"text": 5, "fact_ids": ["F1@1"], "added_details": []},
        "not an object",
        None,
        ["a", "list"],
    ],
)
def test_a_unit_of_the_wrong_shape_is_dropped_not_raised(broken: Any) -> None:
    result = resolve({"units": [broken]})

    assert result.units == () and result.flags == ()
    assert [d.reason for d in result.dropped] == ["invalid"]


@pytest.mark.parametrize("output", [None, [], "units", 3, {}, {"units": None}, {"units": "x"}])
def test_an_output_of_the_wrong_shape_is_an_empty_card(output: Any) -> None:
    result = resolve(output)

    assert result.units == () and result.flags == () and result.dropped == ()


def test_invisible_characters_are_removed_and_details_are_cleaned() -> None:
    answer = {
        "units": [
            unit(
                "I pla​nned 60 delivery routes a week with a team of 3.",
                details=("ran nights", "ran nights", "  ", "ran​ nights"),
            )
        ]
    }

    result = resolve(answer, SENT)

    (kept,) = result.units
    assert kept.text == P1 and "​" not in kept.text
    assert kept.added_details == ("ran nights",)


def test_a_repeated_fact_id_is_cited_once() -> None:
    result = resolve({"units": [unit(P1, ("F1@1", "F1@1"))]})

    assert result.units[0].fact_ids == ("F1@1",)


# --- it never raises and never exceeds its limits -----------------------------------------------

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)
_UNIT = st.fixed_dictionaries(
    {
        "text": st.text(max_size=700),
        "fact_ids": st.lists(st.sampled_from(["F1@1", "F2@1", "F99@1", "x"]), max_size=3),
        "added_details": st.lists(st.text(max_size=30), max_size=3),
    }
)


@settings(max_examples=60, deadline=None)
@given(st.one_of(_JSON, st.fixed_dictionaries({"units": st.lists(_UNIT | _JSON, max_size=9)})))
def test_resolution_never_raises_and_keeps_within_the_limits(output: Any) -> None:
    result = narrative().resolve_narrative_units(output, SENT)

    assert len(result.units) <= 6
    assert sum(len(u.text) for u in result.units) <= 1200
    assert all(u.origin is UnitOrigin.AI and u.section == "answer" for u in result.units)
    kept_ids = {u.unit_id for u in result.units}
    assert {f.unit_id for f in result.flags} <= kept_ids


# --- review fixes (M8.2 part B, review round 1) -------------------------------------------------


def test_a_citation_with_a_trailing_newline_keeps_the_unit_and_is_flagged_unsupported() -> None:
    """The schema no longer refuses ``"F1@1\\n"`` itself; code does: the unit is kept and the
    citation, which names no fact that was sent, is flagged."""
    result = resolve({"units": [unit("I planned delivery routes.", ("F1@1\n",))]})

    assert len(result.units) == 1 and result.dropped == ()
    assert [(f.kind, f.claim) for f in result.flags] == [("unsupported", "F1@1\n")]


def test_the_first_unit_that_passes_the_total_length_ends_the_card() -> None:
    """A later, shorter paragraph is not kept after one was left out: the card would read out of
    order. Every unit from the first overflow on is listed as too long."""
    answer = {"units": [unit("w" * 600, ()), unit("x" * 700, ()), unit("y" * 100, ())]}

    result = resolve(answer)

    assert [len(u.text) for u in result.units] == [600]
    assert [(d.index, d.reason) for d in result.dropped] == [(1, "too_long"), (2, "too_long")]


@pytest.mark.parametrize("name", ["ok_why_leaving", "ok_strengths", "ok_weaknesses"])
def test_the_reviewed_answers_for_the_other_kinds_are_clean(name: str) -> None:
    result = resolve(fixture(name), name_sources=NAMES)

    assert result.flags == () and result.dropped == () and result.units


def test_an_answer_that_is_empty_by_design_is_an_empty_card() -> None:
    result = resolve(fixture("empty_by_design"), name_sources=NAMES)

    assert result.units == () and result.flags == () and result.dropped == ()


def test_a_generalized_name_repeated_in_a_card_is_flagged() -> None:
    result = resolve(fixture("leak_generalized_name"), name_sources=NAMES)

    assert [(f.kind, f.claim) for f in result.flags] == [("needs_check", "Lakeshore Bank")]


def test_an_invented_motive_passes_the_checks_so_only_the_prompt_and_the_person_stop_it() -> None:
    """KNOWN GAP, documented on purpose: a stated wish ("I want to join ... because I admire
    ...") has no number, name or skill for the checker to compare, so code flags nothing. The
    prompt forbids it for every kind and the person reads and approves every word."""
    result = resolve(fixture("invented_motive"), name_sources=NAMES)

    assert result.flags == () and len(result.units) == 1
