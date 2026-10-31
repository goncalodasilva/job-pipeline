"""Fictional data and the assumed markup of the Coverage tab (M7.7; AC-M7.5). Every value is
fictional. The integration and e2e tests share these names. The application modules for the tab
are not imported here, so a missing route or template fails the test that needs it.

Assumed interface (the implementer matches it; spec 2.9, design "Coverage view", ADR-117..123).

Routes
- ``GET /facts?tab=coverage``: the Fact bank page on a fourth tab "Coverage", token-free and
  read-only like the other tabs (``HEAD`` too). With ``HX-Request`` it needs the token and
  answers the main content. The tab link is in ``nav[aria-label="Fact bank sections"]``, text
  "Coverage", ``aria-current="page"`` on the tab.
- ``GET /facts/coverage`` (token, fragment, never a page): ``#coverage`` alone. Query ``page``
  (50 rows a page, clamped). A GET writes nothing: no rows, no tasks, no provider requests.
- ``GET /facts/coverage/{cluster_id}`` (token, fragment): ``#coverage-detail`` for one Thin
  cluster. A cluster id is the first 12 hex digits of the SHA-256 of the cluster key (the safe-id
  shape). An unknown or unsafe id answers the same fixed plain-text 404 for every id, and never
  echoes the id.
- Start session posts to the existing ``POST /sessions`` (token and origin).

Markup of ``#coverage`` (a ``section``)
- Without saved or active jobs there is no table; the text "Add a few jobs and their
  requirements show up here." is shown.
- Otherwise ``table#coverage-table`` with ``tbody > tr[data-cluster-id=<id>][data-strength=
  none|thin|strong]`` per cluster, biggest gap first: None, then Thin, then Strong; inside each,
  greater weighted demand first (job weight x requirement weight, ADR-119), then label.
- Cells ``td[data-cell="label"]``, ``td[data-cell="asked"]``, ``td[data-cell="strength"]`` and
  ``td[data-cell="actions"]``. The asked cell reads "Asked by N saved jobs" and names the number
  of must-have requirements when it is not zero (for example "(2 must-have)"). The strength cell
  holds the word (None, Thin, Strong) and a symbol character, a different one for each strength.
- Every row's actions hold a form (``method="post"``, ``action="/sessions"``, ``hx-post`` the
  same) with hidden ``focus`` = ``cluster:<id>`` and hidden ``length`` = the default length of
  the Sessions setting, and a submit button whose text is "Start session". Thin rows only also
  hold a control (button or link) with the text "Strengthen" and ``hx-get`` =
  ``/facts/coverage/<id>``.
- With more than 50 clusters the section holds ``nav[aria-label="Coverage pages"]`` with links to
  the other pages (``href`` with ``tab=coverage`` and ``page=<n>``).

Markup of ``#coverage-detail`` (a ``section``)
- Heading ``h3#coverage-detail-heading`` (``tabindex="-1"``, ``data-autofocus``) holding the
  cluster's label. One ``li[data-fact-id=<id>]`` per related fact (the confirmed facts that make
  the cluster's evidence), each with a link whose text is "Refine with AI" and whose ``href`` is
  the fact's editor, ``/facts/<id>/edit#fact-refine``. Facts that are not related are not listed.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from tests.integration.test_setup_redirect import Element, outline

COVERAGE_URL = "/facts/coverage"
TAB_URL = "/facts?tab=coverage"
EMPTY_TEXT = "Add a few jobs and their requirements show up here."
PAGE_SIZE = 50

ROUTES = "Five years of route planning"
TERRAFORM = "Experience with Terraform"
ENGLISH = "Fluent English"
PYTHON = "Python scripting"
WAREHOUSE = "Warehouse safety certificate"
SAP = "Knowledge of SAP"
KOTLIN = "Kotlin services"
RUST = "Rust services"

# Biggest gap first (weighted demand in brackets): None [ROUTES 8, ENGLISH 2, WAREHOUSE 2 (label
# order), SAP 1, RUST 1 (label order; its only fact is a draft)], then Thin [TERRAFORM 6, KOTLIN 2
# (one story is Thin)], then Strong [PYTHON 1 (two stories)].
ORDER = (ROUTES, ENGLISH, WAREHOUSE, SAP, RUST, TERRAFORM, KOTLIN, PYTHON)
STRENGTH = {
    ROUTES: "none",
    ENGLISH: "none",
    WAREHOUSE: "none",
    SAP: "none",
    RUST: "none",
    TERRAFORM: "thin",
    KOTLIN: "thin",
    PYTHON: "strong",
}
WORD = {"none": "None", "thin": "Thin", "strong": "Strong"}
# label -> (saved jobs asking, of which must-have requirements)
ASKED = {
    ROUTES: (3, 2),
    TERRAFORM: (2, 2),
    ENGLISH: (1, 0),
    WAREHOUSE: (1, 0),
    SAP: (1, 0),
    RUST: (1, 0),
    KOTLIN: (1, 0),
    PYTHON: (1, 0),
}

TERRAFORM_FACT = "Wrote Terraform modules for 3 fictional depots."
UNRELATED_FACT = "Edited the monthly staff newsletter."


def cluster_id(key: str) -> str:
    """ADR-117 item 2: the first 12 hex digits of the SHA-256 of the cluster key."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def seed_coverage(seed: Any, facts: Any) -> dict[str, str]:
    """Three saved (New) jobs asking for eight clusters, and the facts behind the evidence.
    ``seed`` is a ``tests.board_pages.Seeder``; ``facts`` a ``FactService``. Returns fact ids."""
    from app.domain.facts import FactType
    from app.domain.fit import Requirement
    from app.services.facts import DraftInput, DraftOrigin, FactInput
    from tests.tailoring_pages import seed_summary

    plan = {
        "cov-job-a": (
            "Route Planner",
            [
                ("must_have", ROUTES),
                ("must_have", TERRAFORM),
                ("key", ENGLISH),
                ("nice", PYTHON),
            ],
        ),
        "cov-job-b": (
            "Depot Supervisor",
            [("must_have", ROUTES), ("must_have", TERRAFORM), ("key", WAREHOUSE)],
        ),
        "cov-job-c": (
            "Fleet Analyst",
            [("key", ROUTES), ("nice", SAP), ("key", KOTLIN), ("nice", RUST)],
        ),
    }
    for job_id, (title, requirements) in plan.items():
        seed.job(job_id, title, "Harbor Kite Fictional")
        seed_summary(
            seed.db,
            job_id,
            tuple(
                Requirement(f"{job_id}-r{n}", kind, text)
                for n, (kind, text) in enumerate(requirements, 1)
            ),
            summary_id=f"summary-{job_id}",
        )

    def story(skill: str, what: str) -> Any:
        return facts.create(
            FactInput(
                type=FactType.STORY,
                situation=f"The depot needed {what}.",
                action=f"Built {what} in {skill} for 4 teams.",
                result="Cut manual steps by half.",
                skills=(skill,),
            )
        )

    made = {
        "terraform": facts.create(FactInput(text=TERRAFORM_FACT, skills=("Terraform",))).id,
        "unrelated": facts.create(FactInput(text=UNRELATED_FACT)).id,
        "kotlin": story("Kotlin", "a route app").id,
        "python_1": story("Python", "a roster script").id,
        "python_2": story("Python", "a cleanup script").id,
    }
    origin = DraftOrigin(source="cv_import", provider="fake", model="fake-thorough")
    (draft,) = facts.create_drafts(
        origin,
        [DraftInput(text="Drafted Rust notes.", skills=("Rust",), original_answer="Rust notes")],
    )
    made["rust_draft"] = draft.id
    return made


# --- reading answers ------------------------------------------------------------------------


def rows_of(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.tag == "tr" and "data-cluster-id" in e.attrs]


def cell(elements: list[Element], row: Element, name: str) -> Element:
    found = [
        e for e in elements if e.tag == "td" and e.attrs.get("data-cell") == name and e.within(row)
    ]
    assert len(found) == 1, f'expected one "{name}" cell in a row, found {len(found)}'
    return found[0]


def label_of_row(elements: list[Element], row: Element) -> str:
    return cell(elements, row, "label").text


def row_for(elements: list[Element], label: str) -> Element:
    found = [r for r in rows_of(elements) if label_of_row(elements, r) == label]
    assert len(found) == 1, f'expected one row labelled "{label}", found {len(found)}'
    return found[0]


def labels_in_order(html: str) -> list[str]:
    elements = outline(html)
    return [label_of_row(elements, row) for row in rows_of(elements)]


def inside_row(elements: list[Element], row: Element) -> list[Element]:
    return [e for e in elements if e.within(row)]


def start_form(elements: list[Element], row: Element) -> Element:
    forms = [e for e in inside_row(elements, row) if e.tag == "form"]
    assert len(forms) == 1, f"expected one form in the row, found {len(forms)}"
    return forms[0]


def strengthen_controls(elements: list[Element], row: Element) -> list[Element]:
    return [
        e for e in inside_row(elements, row) if e.tag in ("a", "button") and e.text == "Strengthen"
    ]


def first_row_where(
    elements: list[Element], predicate: Callable[[Element], bool]
) -> Element | None:
    return next((r for r in rows_of(elements) if predicate(r)), None)
