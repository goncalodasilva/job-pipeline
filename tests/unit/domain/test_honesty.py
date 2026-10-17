"""The honesty check for suggested wording (M3.8; AC-M3.5; spec 3.1 items 3 and 5; ADR-089
item 5, ADR-092 item 4): ``app/domain/honesty.py``. Pure; every value is fictional.

Assumed interface (chosen here where the sources left it open):

- ``extract_quantities(text) -> tuple[Quantity, ...]``: every number, percentage, year and
  money amount written in ``text``. ``Quantity`` has ``kind`` (``"number"``, ``"percent"``,
  ``"year"`` or ``"money"``), ``value`` (a ``Decimal``: "1,200" and "1200" are both 1200, "5k"
  is 5000) and ``text`` (as written).
- ``new_claims(suggested, allowed_sources, *, known_skills=(), added_details=())
  -> tuple[Claim, ...]``: what ``suggested`` says that none of ``allowed_sources`` does (the
  fact, its role, the user's answers, the confirmed facts sent): quantities by value,
  capitalised names not found in a source whatever its case (a user types "northgate"; a
  word capitalised only because it starts a sentence is not a name),
  and names from ``known_skills`` (the controlled list, matched as whole words without
  regard to case); plus every non-blank entry of ``added_details`` (what the model itself
  says it added). ``Claim`` is a frozen dataclass with ``kind`` (the quantity kinds,
  ``"name"``, ``"skill"`` or ``"added_detail"``) and ``text`` (as written). No claim twice.

It replaces the number helper of M3.4 (``cv_checks.number_tokens``): the CV import's
``needs_check`` goes through ``extract_quantities`` and stays as strict as before.
"""

import ast
import importlib
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).resolve().parents[3]
QUANTITY_KINDS = frozenset({"number", "percent", "year", "money"})

FACT = "Cut invoice processing time by 35% across 4 warehouses."
ROLE = "Operations Analyst at Example Widgets Ltd"


def honesty() -> ModuleType:
    return importlib.import_module("app.domain.honesty")


def claims(suggested: str, sources: list[str], **options: Any) -> tuple[Any, ...]:
    found = tuple(honesty().new_claims(suggested, sources, **options))
    assert len(found) == len(set(found)), "no claim is listed twice"
    return found


def quantity_texts(found: tuple[Any, ...]) -> set[str]:
    return {claim.text for claim in found if claim.kind in QUANTITY_KINDS}


def of_kind(found: tuple[Any, ...], kind: str) -> list[str]:
    return [claim.text for claim in found if claim.kind == kind]


def values(text: str) -> set[tuple[str, Decimal]]:
    return {(q.kind, q.value) for q in honesty().extract_quantities(text)}


# --- extract_quantities ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The cases of M3.4's number_tokens test, moved here: whole numbers as written.
        ("Cut time by 35% across 4 warehouses.", {("percent", Decimal(35)), ("number", 4)}),
        ("Planned 60 routes", {("number", Decimal(60))}),
        ("1,200 orders in 3.5 days.", {("number", Decimal(1200)), ("number", Decimal("3.5"))}),
        ("(2021 - 2024)", {("year", Decimal(2021)), ("year", Decimal(2024))}),
        ("(2021-2024)", {("year", Decimal(2021)), ("year", Decimal(2024))}),
        ("no numbers here", set()),
        ("", set()),
        # The kinds.
        ("Saved €3,000 a year", {("money", Decimal(3000))}),
        ("A $1.5k budget", {("money", Decimal(1500))}),
        ("Handled 1,999 orders.", {("number", Decimal(1999))}),
        ("Joined in 2019.", {("year", Decimal(2019))}),
        ("Managed 5k people", {("number", Decimal(5000))}),
    ],
)
def test_extract_quantities_kinds_and_values(text: str, expected: set[tuple[str, Any]]) -> None:
    assert values(text) == {(kind, Decimal(value)) for kind, value in expected}


def test_extract_quantities_keep_the_text_as_written() -> None:
    found = honesty().extract_quantities("Cut time by 35% and saved €3,000.")

    assert {q.text.replace(" ", "") for q in found} == {"35%", "€3,000"}
    assert all(isinstance(q.value, Decimal) for q in found)


def test_number_formats_equal() -> None:
    assert values("Handled 1,200 orders") == values("Handled 1200 orders")
    assert values("Cut time by 35%") == values("Cut time by 35 %") == values("by 35 percent")
    assert values("Saved €5,000") == values("Saved €5000") == values("Saved €5k")
    assert values("A 3.50 day backlog") == values("A 3.5 day backlog")
    # Equal formats are allowed both ways; different values never are.
    assert claims("Handled 1200 orders a month.", ["Handled 1,200 orders a month."]) == ()
    assert claims("Handled 1,200 orders a month.", ["Handled 1200 orders a month."]) == ()
    assert claims("Cut time by 35 percent.", ["Cut time by 35%."]) == ()
    assert values("3.5 days") != values("35 days")
    assert values("1,200 orders") != values("12,000 orders")
    assert values("Managed 5k people") != values("Managed 5 people")


# --- new_claims --------------------------------------------------------------------------------


def test_invented_number_flagged() -> None:
    found = claims(
        "Ran the quarterly stocktake for 12 stores, saving €3,000 a year since 2022.",
        ["Ran the quarterly stocktake."],
    )

    assert quantity_texts(found) == {"12", "€3,000", "2022"}
    kinds = {claim.kind for claim in found if claim.kind in QUANTITY_KINDS}
    assert kinds == {"number", "money", "year"}


def test_inflated_number_flagged() -> None:
    found = claims("Cut invoice processing time by 85% across 14 warehouses.", [FACT, ROLE])

    assert {text.replace(" ", "") for text in quantity_texts(found)} == {"85%", "14"}
    assert of_kind(found, "percent") and of_kind(found, "number")
    assert quantity_texts(claims("Cut it by 35.5% across 4 warehouses.", [FACT])) == {"35.5%"}


def test_invented_employer_flagged() -> None:
    found = claims(
        "Cut invoice processing time by 35% across 4 warehouses at Globex Corporation.",
        [FACT, ROLE],
    )

    names = of_kind(found, "name")
    assert any("Globex" in name for name in names), found
    assert quantity_texts(found) == set()
    # A name given in the role is not new.
    assert claims(f"{FACT[:-1]} at Example Widgets Ltd.", [FACT, ROLE]) == ()


def test_new_skill_name_flagged() -> None:
    known = ("SQL", "Excel", "Power BI", "R")
    source = "Built the monthly stock report in Excel."

    found = claims(
        "Built the monthly stock report in Excel and power bi, with sql queries.",
        [source],
        known_skills=known,
    )

    assert {text.casefold() for text in of_kind(found, "skill")} == {"power bi", "sql"}
    assert not [claim for claim in found if claim.text.casefold() == "excel"]
    # "R" is a whole word only: "report" and "reports" don't name it.
    assert claims("Built the monthly stock reports in Excel.", [source], known_skills=known) == ()


def test_number_given_in_user_answer_allowed() -> None:
    answer = "It was about 1,200 invoices a month."
    suggested = f"{FACT[:-1]}, handling about 1200 invoices a month."

    assert claims(suggested, [FACT, ROLE, answer]) == ()
    assert quantity_texts(claims(suggested, [FACT, ROLE])) == {"1200"}, "the answer allows it"


def test_name_given_in_user_answer_or_a_fact_sent_allowed() -> None:
    suggested = f"{FACT[:-1]} for the Northgate depot."

    assert any("Northgate" in name for name in of_kind(claims(suggested, [FACT]), "name"))
    assert claims(suggested, [FACT, "It was the northgate depot."]) == ()
    assert claims(suggested, [FACT, "Opened the Northgate depot in 2020."]) == ()


def test_pure_rephrase_not_flagged() -> None:
    sources = [FACT, ROLE]
    for rephrased in (
        "Across 4 warehouses, cut invoice processing time by 35%.",
        "Reduced invoice processing time by 35% across 4 warehouses at Example Widgets Ltd.",
        "Invoice processing time: cut by 35 percent, across 4 warehouses.",
        FACT,
    ):
        assert claims(rephrased, sources, known_skills=("SQL", "Excel")) == (), rephrased


def test_added_details_are_always_flagged() -> None:
    found = claims(FACT, [FACT], added_details=["saving the finance team a week", "  ", ""])

    assert [(claim.kind, claim.text) for claim in found] == [
        ("added_detail", "saving the finance team a week")
    ]


def test_no_sources_flags_every_claim() -> None:
    found = claims("Cut costs by 35% at Globex Corporation.", [])

    assert {text.replace(" ", "") for text in quantity_texts(found)} == {"35%"}
    assert any("Globex" in name for name in of_kind(found, "name"))


_texts = st.one_of(
    st.text(max_size=120),
    st.lists(
        st.sampled_from(
            [
                "Cut",
                "invoice",
                "time",
                "by",
                "35%",
                "35 percent",
                "1,200",
                "1200",
                "€3,000",
                "$1.5k",
                "5k",
                "2019",
                "(2021-2024)",
                "Globex Corporation",
                "Example Widgets Ltd",
                "sql",
                "Power BI",
                ".",
                ",",
                "\n",
                "3.50",
            ]
        ),
        max_size=30,
    ).map(" ".join),
)


@settings(max_examples=300, deadline=None)
@given(text=_texts, skills=st.lists(st.text(min_size=1, max_size=12), max_size=5))
def test_text_checked_against_itself_has_no_new_claims(text: str, skills: list[str]) -> None:
    assert tuple(honesty().new_claims(text, [text], known_skills=[*skills, "SQL"])) == ()


# --- it replaces the M3.4 number helper --------------------------------------------------------


def _imported_names(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return found


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_cv_import_number_check_goes_through_honesty() -> None:
    """ADR-089 item 5, ADR-092 item 4: one quantity check for imports and refine."""
    cv_checks = REPO_ROOT / "app" / "domain" / "cv_checks.py"

    assert "extract_quantities" in _imported_names(cv_checks).get("app.domain.honesty", set())
    assert "number_tokens" not in _defined_functions(cv_checks), "the M3.4 helper is replaced"


def _unit(text: str, quote: str) -> dict[str, Any]:
    return {
        "type": "fact",
        "role_key": None,
        "text": text,
        "situation": None,
        "action": None,
        "result": None,
        "skills": [],
        "source_quote": quote,
    }


@pytest.mark.parametrize(
    ("wording", "flagged"),
    [
        ("Handled 1,200 orders and a 3.5 day backlog.", False),
        ("Handled 1,300 orders and a 3.5 day backlog.", True),
        ("Handled 1,200 orders and a 5.3 day backlog.", True),
        # Still flagged after M3.8: the wording's words must be its quote's (ADR-092 item 4).
        ("Handled 1200 orders and a 3.5 day backlog.", True),
        ("Handled 1,200 orders and a 3.5 day backlog in 2019.", True),
    ],
)
def test_cv_import_needs_check_keeps_working_through_honesty(wording: str, flagged: bool) -> None:
    from app.services.cv_import import drafts_from

    quote = "Handled 1,200 orders and a 3.5 day backlog."
    cv = f"Example Widgets Ltd - Operations Analyst (2021 - 2024)\n- {quote}\n"

    _roles, (draft,) = drafts_from({"roles": [], "units": [_unit(wording, quote)]}, cv)

    assert draft.needs_check is flagged
