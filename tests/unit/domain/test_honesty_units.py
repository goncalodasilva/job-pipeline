"""The honesty check for generated units (M6.3; AC-M6.1, AC-M6.2; spec 3.1 items 2-4 and 6;
ADR-006, ADR-089 item 5, ADR-097, ADR-106 items 7 and 8): ``app/domain/honesty.py``. Pure;
every value is fictional.

Assumed interface (chosen here where the sources left it open; the implementer follows it):

- ``GeneratedUnit``: frozen dataclass, one line or paragraph of a generated document, with
  ``unit_id: str``, ``kind: "structure" | "content"``, ``section: str``, ``role: str | None``
  (the role label of the section the unit sits under, the same string as ``AIFact.role``;
  ``None`` outside a role section), ``text: str``, ``fact_ids: tuple[str, ...]`` (cited ids as
  written by the model, such as ``"F12@3"``) and ``added_details: tuple[str, ...]``.
- The facts sent are the existing ``app.domain.fact_selection.AIFact`` (ref, content, role,
  skills, generalize). A fact says its role, its skills and the parts of its wording, as
  ``fact_text`` renders them. A unit's cited fact is supported only when its ``str(ref)`` was
  sent in that request: ``F1@2`` sent does not make ``F1@1`` known.
- ``HonestyFlag``: frozen dataclass with ``kind: "unsupported" | "needs_check"``,
  ``claim: str`` and ``unit_id: str``. ``claim`` is, for an uncited or non-heading unit, its
  text; for an unknown or unsendable citation, the cited id as written; for a cross-role
  citation, the cited id; otherwise the claim as ``new_claims`` writes it (a number as
  written, a name, a skill, an ``added_details`` entry).
- ``check_units(units, sent, *, template_headings=(), known_skills=()) -> tuple[HonestyFlag,
  ...]``: flags in unit order; within a unit the citation flags first, then the claims in
  ``new_claims`` order. Each claim is flagged once per unit.
    * a cited id not among the ids sent, or a content unit without ids: ``unsupported``;
    * a structure unit whose text is not exactly one of ``template_headings`` counts as
      uncited content: ``unsupported``; an exact heading has no flags;
    * quantities, capitalised names, controlled skills and ``added_details`` absent from the
      unit's own cited, sent facts (``new_claims`` against their ``fact_text``): ``needs_check``;
      a sent fact that the unit does not cite supports nothing; the text of a ``generalize``
      fact supports quantities and skills, never a name;
    * the unit's ``role`` is set and a cited, sent fact has a different non-empty ``role``:
      one ``needs_check`` flag per such fact id.
- ``CurrentFact``: frozen dataclass ``(number: int, version: int, status: FactStatus,
  privacy: Privacy = Privacy.NORMAL)``, the current state of one fact (``version`` is its
  current version).
- ``approval_blockers(units, facts_now) -> tuple[HonestyFlag, ...]``: for each cited id of
  each unit (once per unit, in citation order), one flag at most: ``unsupported`` when the
  fact is now archived or discarded, or ``never_use``, or the id isn't a fact reference or
  names a fact that does not exist; else ``needs_check`` when the cited version is older than
  the current one. The claim is the cited id. It doesn't rerun ``check_units``; structure
  units are ignored.
"""

import importlib
from types import ModuleType
from typing import Any

import pytest

from app.domain.fact_selection import AIFact
from app.domain.facts import FactContent, FactRef, FactStatus, Privacy

ROLE_A = "Operations Analyst at Example Widgets Ltd"
ROLE_B = "Route Planner at Sample Freight Co"
OPS_FACT = "Cut invoice processing time by 35% across 4 warehouses."


def honesty() -> ModuleType:
    return importlib.import_module("app.domain.honesty")


def fact(
    number: int,
    text: str,
    *,
    version: int = 1,
    role: str | None = None,
    skills: tuple[str, ...] = (),
    generalize: bool = False,
) -> AIFact:
    return AIFact(FactRef(number, version), FactContent(text=text), role, skills, generalize)


def unit(
    unit_id: str,
    text: str,
    fact_ids: tuple[str, ...] = (),
    *,
    kind: str = "content",
    section: str = "Experience",
    role: str | None = None,
    added_details: tuple[str, ...] = (),
) -> Any:
    return honesty().GeneratedUnit(
        unit_id=unit_id,
        kind=kind,
        section=section,
        role=role,
        text=text,
        fact_ids=fact_ids,
        added_details=added_details,
    )


def check(units: list[Any], sent: list[AIFact], **options: Any) -> tuple[Any, ...]:
    return tuple(honesty().check_units(units, sent, **options))


def flag_set(found: tuple[Any, ...], kind: str) -> set[tuple[str, str]]:
    return {(flag.claim, flag.unit_id) for flag in found if flag.kind == kind}


# --- Citations (spec 3.1 item 3, first two bullets; AC-M6.1) ----------------------------------


def test_ac_m6_1_unknown_fact_id_is_unsupported() -> None:
    found = check(
        [unit("u1", "Cut invoice processing time.", ("F99@1",))],
        [fact(1, OPS_FACT)],
    )

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("unsupported", "F99@1", "u1")]


def test_ac_m6_1_unknown_fact_id_in_any_shape_is_unsupported() -> None:
    found = check(
        [unit("u1", "Cut invoice processing time.", ("bogus", "F1@1", "f1@1"))],
        [fact(1, OPS_FACT)],
    )

    assert flag_set(found, "unsupported") == {("bogus", "u1"), ("f1@1", "u1")}
    assert flag_set(found, "needs_check") == set()


def test_ac_m6_1_id_not_sent_in_request_is_unsupported() -> None:
    # F1@2 was sent; the model cites the older F1@1, and F2@1 exists in the bank but was
    # not part of this request.
    sent = [fact(1, OPS_FACT, version=2)]

    found = check([unit("u1", "Cut invoice processing time.", ("F1@1", "F2@1"))], sent)

    assert flag_set(found, "unsupported") == {("F1@1", "u1"), ("F2@1", "u1")}


def test_ac_m6_1_uncited_paragraph_is_unsupported() -> None:
    found = check(
        [unit("u1", "Cut invoice processing time across the warehouses.", ())],
        [fact(1, OPS_FACT)],
    )

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("unsupported", "Cut invoice processing time across the warehouses.", "u1")
    ]


def test_ac_m6_1_a_known_id_among_unknown_ones_still_supports_its_wording() -> None:
    found = check(
        [unit("u1", "Cut invoice processing time by 35%.", ("F1@1", "F77@4"))],
        [fact(1, OPS_FACT)],
    )

    assert [(f.kind, f.claim) for f in found] == [("unsupported", "F77@4")]


# --- Claims absent from the unit's own cited facts (spec 3.1 item 3, third bullet) -------------


@pytest.mark.parametrize(
    ("source", "wording", "claim"),
    [
        pytest.param(
            "Managed a team of 4 analysts.", "Managed a team of 14 analysts.", "14", id="digits"
        ),
        pytest.param(
            "Mentored four junior analysts.",
            "Mentored fourteen junior analysts.",
            "fourteen",
            id="words",
        ),
        pytest.param(
            "Cut invoice processing time by 35%.",
            "Cut invoice processing time by 85%.",
            "85%",
            id="percent",
        ),
        pytest.param(
            "Saved €5,000 a year in supplier costs.",
            "Saved €50,000 a year in supplier costs.",
            "€50,000",
            id="money",
        ),
        pytest.param(
            "Led the stock system migration in 2021.",
            "Led the stock system migration in 2019.",
            "2019",
            id="year",
        ),
        pytest.param(
            "Doubled the weekly report throughput.",
            "Tripled the weekly report throughput.",
            "Tripled",
            id="multiplier",
        ),
    ],
)
def test_ac_m6_2_number_absent_from_cited_facts_needs_check(
    source: str, wording: str, claim: str
) -> None:
    found = check([unit("u1", wording, ("F1@1",))], [fact(1, source)])

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("needs_check", claim, "u1")]


def test_ac_m6_2_number_from_an_uncited_sent_fact_still_needs_check() -> None:
    sent = [
        fact(1, "Cut invoice processing time by 35%."),
        fact(2, "Cut stock count time by 60%."),
    ]

    found = check([unit("u1", "Cut stock count time by 60%.", ("F1@1",))], sent)

    assert flag_set(found, "needs_check") == {("60%", "u1")}
    assert flag_set(found, "unsupported") == set()


def test_ac_m6_2_number_supported_by_one_of_several_cited_facts_is_not_flagged() -> None:
    sent = [
        fact(1, "Cut invoice processing time by 35%."),
        fact(2, "Trained 12 new starters."),
    ]

    found = check(
        [
            unit(
                "u1",
                "Cut invoice processing time by 35% and trained 12 starters.",
                ("F1@1", "F2@1"),
            )
        ],
        sent,
    )

    assert found == ()


def test_invented_employer_needs_check() -> None:
    sent = [fact(1, "Built the weekly stock report at Example Widgets Ltd.")]

    found = check(
        [unit("u1", "Built the weekly stock report at Globex Corporation.", ("F1@1",))], sent
    )

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("needs_check", "Globex Corporation", "u1")
    ]


def test_skill_not_in_cited_facts_needs_check() -> None:
    sent = [fact(1, "Built weekly stock dashboards in Excel.")]

    found = check(
        [unit("u1", "Built weekly stock dashboards in Power BI.", ("F1@1",))],
        sent,
        known_skills=("Power BI", "SQL"),
    )

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("needs_check", "Power BI", "u1")]


def test_generalized_fact_name_never_supports_a_name() -> None:
    text = "Cut reporting time by 35% for Northgate Bank."
    wording = [unit("u1", "Cut reporting time by 35% for Northgate Bank.", ("F1@1",))]

    named = check(wording, [fact(1, text, generalize=True)])
    open_ = check(wording, [fact(1, text)])

    # The numbers the generalized fact states still count; its names do not.
    assert [(f.kind, f.claim, f.unit_id) for f in named] == [
        ("needs_check", "Northgate Bank", "u1")
    ]
    assert open_ == ()


def test_structure_unit_with_claim_is_unsupported() -> None:
    heading = unit("h1", "Experience", kind="structure", section="Experience")
    smuggled = unit(
        "h2", "Led 40 engineers at Globex Corporation", kind="structure", section="Experience"
    )

    found = check([heading, smuggled], [fact(1, OPS_FACT)], template_headings=("Experience",))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("unsupported", "Led 40 engineers at Globex Corporation", "h2")
    ]


def test_unit_citing_another_roles_fact_needs_check() -> None:
    sent = [
        fact(1, "Built the weekly stock report.", role=ROLE_A),
        fact(2, "Planned the weekly freight routes.", role=ROLE_B),
    ]
    mixed = unit(
        "u1",
        "Built the weekly stock report and planned the weekly freight routes.",
        ("F1@1", "F2@1"),
        role=ROLE_A,
    )

    found = check([mixed], sent)

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("needs_check", "F2@1", "u1")]


def test_model_added_details_flagged() -> None:
    found = check(
        [
            unit(
                "u1",
                "Cut invoice processing time by 35%.",
                ("F1@1",),
                added_details=("a team of about ten", "   "),
            )
        ],
        [fact(1, OPS_FACT)],
    )

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("needs_check", "a team of about ten", "u1")
    ]


def test_faithful_rephrase_has_no_flags() -> None:
    sent = [
        fact(1, OPS_FACT, role=ROLE_A, skills=("SQL",)),
        fact(
            2,
            "Weekly stock counts were slow.",
            role=ROLE_A,
        ),
    ]
    units = [
        unit("h1", "Experience", kind="structure"),
        unit(
            "u1",
            "Reduced invoice processing time by 35% across four warehouses at Example Widgets "
            "Ltd, using SQL.",
            ("F1@1",),
            role=ROLE_A,
        ),
        unit("u2", "Found that weekly stock counts were slow.", ("F2@1",), role=ROLE_A),
    ]

    found = check(units, sent, template_headings=("Experience",), known_skills=("SQL", "Power BI"))

    assert found == ()


# --- Approval (spec 3.1 item 4; ADR-106 item 8) -------------------------------------------------


def test_approval_blockers_never_use_archived_and_superseded_citations() -> None:
    cf = honesty().CurrentFact
    facts_now = [
        cf(1, 1, FactStatus.CONFIRMED),
        cf(2, 1, FactStatus.CONFIRMED, Privacy.NEVER_USE),
        cf(3, 1, FactStatus.ARCHIVED),
        cf(4, 1, FactStatus.DISCARDED),
        cf(5, 3, FactStatus.CONFIRMED),
        cf(6, 2, FactStatus.ARCHIVED),
        cf(7, 1, FactStatus.CONFIRMED, Privacy.GENERALIZE),
    ]
    units = [
        unit("ok", "Fine.", ("F1@1", "F7@1")),
        unit("never", "Wording.", ("F2@1",)),
        unit("archived", "Wording.", ("F3@1",)),
        unit("discarded", "Wording.", ("F4@1",)),
        unit("old", "Wording.", ("F5@2",)),
        unit("current", "Wording.", ("F5@3",)),
        unit("both", "Wording.", ("F6@1",)),
        unit("two", "Wording.", ("F3@1", "F5@1")),
    ]

    found = tuple(honesty().approval_blockers(units, facts_now))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("unsupported", "F2@1", "never"),
        ("unsupported", "F3@1", "archived"),
        ("unsupported", "F4@1", "discarded"),
        ("needs_check", "F5@2", "old"),
        ("unsupported", "F6@1", "both"),
        ("unsupported", "F3@1", "two"),
        ("needs_check", "F5@1", "two"),
    ]
